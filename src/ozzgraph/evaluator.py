"""Deterministic evaluator with model fallback and replanning for OzzGraph (PR21).

Implements the EVALUATOR layer (docs/ARCHITECTURE.md, \"Evaluator\"; PR
step 21 of docs/IMPLEMENTATION_PLAN.md, the closing slice of Phase 7
Planner-Executor-Evaluator): the deterministic interpreter of the plan
timeline the executor (PR20) persists. :meth:`Evaluator.decide_plan`
reads the LATEST persisted ``plan`` entity — never re-deriving a plan id
from the graph hash, because the executor's own persistence mutates the
graph so a plan id is never re-derived (the persisted plan entities form
the run's plan timeline) — plus its persisted ``plan_step`` entities and
the current hypothesis/evidence/action state, and returns one typed
:class:`PlanEvaluation` verdict backed by evidence and entity ids.

Design rules:

- Latest plan, never re-derived (PR20 contract): the latest plan entity
  is the one with the greatest ``(created_at, id)`` among persisted
  ``plan`` entities. ``created_at`` is part of the authoritative graph
  state and replay reconstructs it exactly, so the selection is
  deterministic. The evaluator reconstructs the :class:`Plan` model from
  the persisted entities (plan payload, per-step entities, current
  hypothesis evidence) and evaluates the CURRENT graph state against the
  plan's typed conditions.

- Deterministic decisions first (AGENTS.md \"prefer deterministic code
  over extra model calls\"): every step, hypothesis, and plan decision is
  a pure function of the graph. Step completion/abandon interpret the
  planner's condition templates structurally: a hypothesis-testing step
  completes when the hypothesis gained NEW supporting evidence (an
  ``EVIDENCE SUPPORTS HYPOTHESIS`` edge created after the plan entity)
  and is abandoned when it gained NEW contradicting evidence or burned
  its attempt budget; a service step completes when its service is
  characterized and is abandoned when the service is absent or its
  attempt budget is exhausted. Plan completion follows
  :data:`~ozzgraph.planner.PLAN_COMPLETION_CONDITIONS` (every step
  completed, or a ranked hypothesis confirmed with new supporting
  evidence and no new contradictions), and plan abandonment follows
  :data:`~ozzgraph.planner.PLAN_ABANDONMENT_CONDITIONS` (every ranked
  hypothesis refuted, or the graph no longer routes to the plan's
  phase) plus the plan budgets below.

- Plan budgets (Phase 7 \"plan budgets\"): :data:`MAX_ATTEMPTS_PER_STEP`
  bounds attempts per step (loop recovery — a step with that many
  attempted actions and no completion is abandoned, never retried
  forever) and :data:`MAX_MODEL_CALLS_PER_PLAN` bounds the action
  entities bound to a plan (each approved executor turn consumes exactly
  one model call and records one action entity). A plan that exhausts a
  budget is abandoned and re-planned, never looped. The step cap
  :data:`~ozzgraph.planner.MAX_PLAN_STEPS` is enforced defensively.

- Replanning + loop recovery (Phase 7 exit criterion: \"wrong hypothesis
  is abandoned and replaced autonomously\"): when the plan is abandoned,
  the evaluator builds a new plan via :class:`~ozzgraph.planner.Planner`
  for the CURRENT graph state and routed phase, persists it with the
  executor's persistence pattern (plan/step entities, ``PLANSTEP TESTS
  HYPOTHESIS`` edges, mirrored ``graph.*`` events), and records the
  supersession with a ``PLAN SUPERSEDES PLAN`` edge from the new plan to
  the old. If replanning would rebuild the IDENTICAL plan (the graph is
  unchanged) or no replacement plan is derivable (non-branching graph, or
  the routed phase has no skill packs), the plan is abandoned with no
  successor — the evaluator never loops on a failing plan.

- Model fallback, only when inconclusive (Phase 7 \"model evaluator
  fallback\"): a hypothesis with no deterministic signal
  (:class:`HypothesisVerdict.UNDETERMINED`) may be handed to a model
  client (injected; the executor's :class:`~ozzgraph.model_client.ModelService`
  satisfies the :class:`ModelFallbackClient` contract) under a strict
  typed output contract — a single JSON object with ``verdict``
  (one of the typed values) and ``reason`` — parsed with the adapters'
  JSON repair strategy (fence strip, then first balanced object). The
  fallback is invoked ONLY for undetermined hypotheses, its output is
  constrained to the same typed schemas, and it respects the model-call
  budget (:class:`~ozzgraph.budgets.Budgets` check + consume, mirroring
  the executor's accounting; an exhausted budget fails loudly). A
  malformed fallback output raises :class:`MalformedEvaluatorOutputError`.

- Replay compatibility (AGENTS.md data invariants): every graph mutation
  the evaluator makes — the evaluation decision entity, the replanned
  plan entities, and the supersedes edge — shares one timestamp with its
  ``graph.*`` event, so replaying the log reconstructs the identical
  graph hash. Evaluation decisions are persisted as ``evaluation``
  entities (``eval-<plan id>-<seq>``) plus ``evaluator.plan_evaluated``
  run events; replans additionally emit ``evaluator.plan_replanned``.

- Small kernel (AGENTS.md rule #10): the evaluator owns only the
  interpretation; budgets, router, planner, and the model client are
  injected. The executor (PR20) exposes :meth:`~ozzgraph.executor.Executor.consult_evaluator`
  as the minimal integration surface; nothing is wired into the
  supervisor yet.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ozzgraph.adapters import _first_balanced_object, _strip_code_fence
from ozzgraph.budgets import BudgetExceeded, BudgetKind, Budgets
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.executor import (
    EDGE_PLANSTEP_TESTS_HYPOTHESIS,
    ENTITY_ACTION,
    ENTITY_PLAN,
    ENTITY_PLAN_STEP,
)
from ozzgraph.model_client import ModelMessage, ModelRequest, ModelResponse
from ozzgraph.phases import Phase
from ozzgraph.planner import (
    EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS,
    MAX_PLAN_STEPS,
    AbandonCondition,
    Hypothesis,
    Plan,
    Planner,
    PlannerSkillUnavailableError,
    PlanStep,
)
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    ENTITY_HYPOTHESIS,
    FIELD_CHARACTERIZED,
    PhaseRoute,
    PhaseRouter,
)
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Producer name on every evaluator event.
EVALUATOR_PRODUCER = "evaluator"

#: Run-log event emitted for every persisted evaluation decision.
EVALUATOR_PLAN_EVALUATED = "evaluator.plan_evaluated"

#: Run-log event emitted when an abandoned plan is replaced by a new plan.
EVALUATOR_PLAN_REPLANNED = "evaluator.plan_replanned"

#: Entity type the evaluator writes for one evaluation decision
#: (docs/DATA_STRATEGY.md, lowercase by convention).
ENTITY_EVALUATION = "evaluation"

#: Entity type the fallback reads for observation summaries.
ENTITY_OBSERVATION = "observation"

#: Edge type recording that a new plan replaced an abandoned one (new -> old).
EDGE_PLAN_SUPERSEDES_PLAN = "PLAN SUPERSEDES PLAN"

#: Edge type linking evidence to the observation it was extracted from
#: (docs/DATA_STRATEGY.md; read defensively for fallback context).
EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION = "EVIDENCE EXTRACTED_FROM OBSERVATION"

#: Loop-recovery threshold: a step with this many attempted actions and no
#: completion is abandoned (never retried forever).
MAX_ATTEMPTS_PER_STEP = 3

#: Plan budget: this many action entities bound to a plan (one per approved
#: executor turn, each consuming one model call) exhausts the plan.
MAX_MODEL_CALLS_PER_PLAN = 10

#: Output token cap for a model-fallback completion.
FALLBACK_MAX_TOKENS = 256

#: Character cap for the fallback prompt's evidence context.
FALLBACK_CONTEXT_LIMIT = 600

#: Character cap for one observation summary embedded in fallback context.
FALLBACK_SUMMARY_LIMIT = 200

#: Maximum observation summaries embedded in fallback context.
FALLBACK_MAX_SUMMARIES = 8

#: Service-step completion-condition template (planner emits exactly this
#: shape, with the service id embedded); the evaluator extracts the id.
_SERVICE_CHARACTERIZED_RE = re.compile(r"^service (.+) is characterized$")


class EvaluatorError(RuntimeError):
    """Base error for the evaluator layer (AGENTS.md rule #9)."""


class NoPlanError(EvaluatorError):
    """The graph holds no ``plan`` entity, so there is nothing to evaluate.

    Raised by :meth:`Evaluator.decide_plan` when no plan has been
    persisted yet (e.g. a fresh run before the first executor turn).
    """


class InvalidPlanStateError(EvaluatorError):
    """Persisted plan entities or graph payloads the evaluator reads are invalid.

    Raised when a persisted plan/step entity is missing, wrong-typed, or
    unrecognized (e.g. a step id the plan payload does not cover, a
    service step whose conditions do not match the planner's template, a
    hypothesis without a graph entity), or when a payload field the
    evaluator reads violates its contract. The evaluator never coerces or
    silently skips such state — it fails loudly.
    """


class MalformedEvaluatorOutputError(EvaluatorError):
    """The model fallback's output violates the strict verdict contract.

    Raised when the fallback completion is empty, is not a JSON object,
    or fails :class:`_ModelVerdict` validation — including after the
    adapters' JSON repair strategy (fence strip, balanced-object
    extraction) was applied. Model output is untrusted (AGENTS.md
    Security Boundaries) and is never coerced.
    """


class StepOutcome(str, Enum):
    """The typed outcome of one plan step.

    Attributes:
        PENDING: The step is in progress: no completion or abandon
            condition holds yet and its attempt budget is not spent.
        COMPLETED: The step's completion condition holds (new supporting
            evidence for a hypothesis step; the service characterized).
        FAILED: The step's abandon condition holds, or its attempt budget
            is exhausted (loop recovery); the step is never retried.
        BLOCKED: The step is rendered moot by the plan-level decision
            (early completion or abandonment) and will not run.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class HypothesisVerdict(str, Enum):
    """The typed verdict for one ranked hypothesis.

    Attributes:
        CONFIRMED: The hypothesis gained NEW supporting evidence with no
            NEW contradicting evidence (deterministically), or the model
            fallback confirmed it.
        REFUTED: The hypothesis gained NEW contradicting evidence, or its
            step exhausted its attempt budget; it is abandoned.
        UNDETERMINED: No deterministic signal; the model fallback either
            was not configured or also could not decide.
    """

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNDETERMINED = "undetermined"


class PlanVerdict(str, Enum):
    """The typed plan-level decision.

    Attributes:
        CONTINUE: No completion or abandonment condition holds; keep
            executing the plan.
        COMPLETE: Every step reached its completion condition, or a
            ranked hypothesis was confirmed.
        ABANDON: The plan cannot continue (budget exhausted, every
            hypothesis refuted, or the graph no longer routes to its
            phase) and no replacement plan was derived.
        REPLAN: The plan is abandoned and was replaced by a new plan,
            persisted as the new latest plan entity.
    """

    CONTINUE = "continue"
    COMPLETE = "complete"
    ABANDON = "abandon"
    REPLAN = "replan"


class StepEvaluation(BaseModel):
    """The typed outcome of one step, with the evidence backing it.

    Attributes:
        step_id: The ``plan_step`` entity id evaluated.
        outcome: The typed :class:`StepOutcome`.
        attempts: Action entities bound to this step so far (each
            approved executor turn records exactly one).
        supporting_evidence: Evidence entity ids currently supporting the
            step's hypothesis (empty for service steps).
        contradicting_evidence: Evidence entity ids currently
            contradicting the step's hypothesis (empty for service
            steps).
        reason: The deterministic (or fallback) justification.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    outcome: StepOutcome
    attempts: int = Field(ge=0)
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


class HypothesisEvaluation(BaseModel):
    """The typed verdict for one ranked hypothesis, with evidence ids.

    Attributes:
        hypothesis_id: The ``hypothesis`` entity id evaluated.
        verdict: The typed :class:`HypothesisVerdict`.
        supporting_evidence: Evidence entity ids supporting the
            hypothesis (ordered by edge id).
        contradicting_evidence: Evidence entity ids contradicting it.
        reason: The deterministic (or fallback) justification.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1)
    verdict: HypothesisVerdict
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


class PlanEvaluation(BaseModel):
    """The typed plan-level decision for one evaluation pass.

    Attributes:
        plan_id: The evaluated plan's entity id.
        verdict: The typed :class:`PlanVerdict`.
        step_outcomes: One :class:`StepEvaluation` per plan step, in plan
            order.
        hypothesis_outcomes: One :class:`HypothesisEvaluation` per ranked
            hypothesis, in plan order.
        superseded_by: The new plan's entity id when ``verdict`` is
            :attr:`PlanVerdict.REPLAN`, else ``None``.
        reason: The deterministic justification.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    verdict: PlanVerdict
    step_outcomes: tuple[StepEvaluation, ...] = ()
    hypothesis_outcomes: tuple[HypothesisEvaluation, ...] = ()
    superseded_by: str | None = None
    reason: str = Field(min_length=1)


class _ModelVerdict(BaseModel):
    """The strict output contract for one model-fallback completion."""

    model_config = ConfigDict(extra="forbid")

    verdict: HypothesisVerdict
    reason: str = Field(min_length=1, max_length=500)


class ModelFallbackClient(Protocol):
    """The async ``complete`` contract the model fallback needs.

    :class:`~ozzgraph.model_client.ModelService` satisfies this protocol
    structurally; tests inject lightweight fakes.
    """

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class Evaluator:
    """Deterministic plan evaluator with model fallback and replanning.

    Args:
        run_id: Run identifier recorded on every event.
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``evaluator.*`` run events; when ``None`` no
            events are emitted.
        budgets: Optional budget tracker the model fallback checks and
            consumes against (one model call per fallback completion);
            when ``None`` the fallback is unbudgeted.
        router: Graph-driven phase router used for the routed-phase
            abandonment condition and replanning; defaults to a
            :class:`PhaseRouter`.
        planner: Deterministic planner used to build replacement plans;
            defaults to a :class:`Planner`.
        model_client: Optional model client for the fallback path; the
            fallback is invoked ONLY for hypotheses the deterministic
            rules leave undetermined.
        model_name: Model identifier used for fallback completions;
            required when ``model_client`` is configured.
        max_attempts_per_step: Loop-recovery threshold (default
            :data:`MAX_ATTEMPTS_PER_STEP`).
        max_model_calls_per_plan: Plan budget (default
            :data:`MAX_MODEL_CALLS_PER_PLAN`).
    """

    def __init__(
        self,
        *,
        run_id: str = "evaluator",
        event_log: EventLog | None = None,
        budgets: Budgets | None = None,
        router: PhaseRouter | None = None,
        planner: Planner | None = None,
        model_client: ModelFallbackClient | None = None,
        model_name: str | None = None,
        max_attempts_per_step: int = MAX_ATTEMPTS_PER_STEP,
        max_model_calls_per_plan: int = MAX_MODEL_CALLS_PER_PLAN,
    ) -> None:
        if model_client is not None and model_name is None:
            raise ValueError("model_name is required when a model_client is configured")
        if max_attempts_per_step < 1:
            raise ValueError("max_attempts_per_step must be >= 1")
        if max_model_calls_per_plan < 1:
            raise ValueError("max_model_calls_per_plan must be >= 1")
        self._run_id = run_id
        self._event_log = event_log
        self._budgets = budgets
        self._router = router if router is not None else PhaseRouter()
        self._planner = planner if planner is not None else Planner()
        self._model_client = model_client
        self._model_name = model_name
        self._max_attempts_per_step = max_attempts_per_step
        self._max_model_calls_per_plan = max_model_calls_per_plan

    async def decide_plan(
        self,
        graph: StateGraph,
        *,
        use_model_fallback: bool = True,
    ) -> PlanEvaluation:
        """Evaluate the latest persisted plan and decide, persisting the decision.

        Flow: load the latest plan entity -> route the graph -> evaluate
        every step and ranked hypothesis (deterministically, with the
        model fallback for undetermined hypotheses when enabled) ->
        decide the plan verdict -> persist a replacement plan when
        replanning -> persist the typed decision as an ``evaluation``
        entity plus ``graph.*`` and ``evaluator.*`` events -> return the
        typed :class:`PlanEvaluation`.

        Args:
            graph: The authoritative SQLite state graph to evaluate on.
            use_model_fallback: Whether undetermined hypotheses may be
                handed to the configured model client.

        Raises:
            NoPlanError: If the graph holds no ``plan`` entity.
            InvalidPlanStateError: If a persisted plan/step entity or a
                payload field the evaluator reads is invalid.
            MalformedEvaluatorOutputError: If a fallback completion
                violates the strict verdict contract.
            BudgetExceeded: If the fallback is invoked with an exhausted
                model-call budget (fail loudly, AGENTS.md rule #9).
        """
        plan, plan_created_at = await self._load_latest_plan(graph)
        route = await self._router.route(graph)

        snapshots: dict[
            str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]
        ] = {}
        step_evals: list[StepEvaluation] = []
        for step in plan.steps:
            if step.hypothesis_id is not None:
                snapshot = await self._evidence_snapshot(graph, step.hypothesis_id, plan_created_at)
                snapshots[step.hypothesis_id] = snapshot
            else:
                snapshot = None
            step_evals.append(await self._evaluate_step(graph, step, snapshot))

        step_eval_by_id = {evaluation.step_id: evaluation for evaluation in step_evals}
        step_by_hypothesis = {
            step.hypothesis_id: step for step in plan.steps if step.hypothesis_id is not None
        }
        hyp_evals: list[HypothesisEvaluation] = []
        for hypothesis in plan.hypotheses:
            hypothesis_step = step_by_hypothesis.get(hypothesis.id)
            attempts = (
                step_eval_by_id[hypothesis_step.id].attempts if hypothesis_step is not None else 0
            )
            snapshot = snapshots.get(hypothesis.id, ((), (), (), ()))
            hyp_evals.append(
                await self._evaluate_hypothesis(
                    graph, hypothesis, snapshot, attempts, use_model_fallback
                )
            )

        abandoned_reason = await self._abandonment_reason(graph, plan, step_evals, hyp_evals, route)
        verdict: PlanVerdict
        superseded_by: str | None = None
        reason: str
        if abandoned_reason is not None:
            verdict, superseded_by, reason = await self._decide_abandoned(
                graph, plan, route, abandoned_reason
            )
        elif all(evaluation.outcome == StepOutcome.COMPLETED for evaluation in step_evals):
            verdict = PlanVerdict.COMPLETE
            reason = "every plan step reached its completion condition"
        elif any(evaluation.verdict == HypothesisVerdict.CONFIRMED for evaluation in hyp_evals):
            verdict = PlanVerdict.COMPLETE
            reason = "a ranked hypothesis gained new supporting evidence with no contradictions"
        else:
            verdict = PlanVerdict.CONTINUE
            reason = "no completion or abandonment condition holds"

        if verdict is not PlanVerdict.CONTINUE:
            step_evals = [
                evaluation
                if evaluation.outcome is not StepOutcome.PENDING
                else evaluation.model_copy(
                    update={
                        "outcome": StepOutcome.BLOCKED,
                        "reason": "rendered moot by the plan-level decision",
                    }
                )
                for evaluation in step_evals
            ]

        evaluation = PlanEvaluation(
            plan_id=plan.id,
            verdict=verdict,
            step_outcomes=tuple(step_evals),
            hypothesis_outcomes=tuple(hyp_evals),
            superseded_by=superseded_by,
            reason=reason,
        )
        await self._persist_evaluation(graph, evaluation)
        return evaluation

    async def _load_latest_plan(self, graph: StateGraph) -> tuple[Plan, datetime]:
        """The latest persisted plan entity and its creation time.

        \"Latest\" is the greatest ``(created_at, id)`` among ``plan``
        entities — the executor's persisted plans form the run's plan
        timeline, and a plan id is never re-derived from the graph hash
        (PR20 contract).
        """
        plans = await graph.list_entities(ENTITY_PLAN)
        if not plans:
            raise NoPlanError("the graph holds no plan entity; nothing to evaluate")
        latest = max(plans, key=lambda record: (record.created_at, record.id))
        return await self._load_plan(graph, latest), latest.created_at

    async def _load_plan(self, graph: StateGraph, record: EntityRecord) -> Plan:
        """Reconstruct a :class:`Plan` from the persisted plan entities.

        The plan entity payload carries the phase, step count, ranked
        hypothesis ids, and the completion/abandonment conditions; each
        step is a ``plan_step`` entity. Hypotheses are re-read from the
        CURRENT graph (rank from the persisted order, evidence from the
        current edges), so the evaluator always judges the current state
        against the plan's conditions.
        """
        try:
            phase = Phase(record.data["phase"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidPlanStateError(
                f"plan entity {record.id!r} has an invalid 'phase' payload: {exc}"
            ) from exc
        hypothesis_ids = record.data.get("hypotheses", [])
        if not isinstance(hypothesis_ids, list) or not all(
            isinstance(hypothesis_id, str) for hypothesis_id in hypothesis_ids
        ):
            raise InvalidPlanStateError(
                f"plan entity {record.id!r} payload field 'hypotheses' must be a list of strings"
            )
        raw_step_count = record.data.get("step_count", 0)
        if (
            not isinstance(raw_step_count, int)
            or isinstance(raw_step_count, bool)
            or raw_step_count < 0
        ):
            raise InvalidPlanStateError(
                f"plan entity {record.id!r} payload field 'step_count' must be an int >= 0"
            )
        step_count = raw_step_count
        raw_completions = record.data.get("completion_conditions", [])
        if not isinstance(raw_completions, list) or not all(
            isinstance(condition, str) for condition in raw_completions
        ):
            raise InvalidPlanStateError(
                f"plan entity {record.id!r} payload field 'completion_conditions' "
                "must be a list of strings"
            )
        completion_conditions = tuple(raw_completions)
        raw_abandonments = record.data.get("abandonment_conditions", [])
        if not isinstance(raw_abandonments, list):
            raise InvalidPlanStateError(
                f"plan entity {record.id!r} payload field 'abandonment_conditions' must be a list"
            )
        try:
            abandonment_conditions = tuple(
                AbandonCondition.model_validate(condition) for condition in raw_abandonments
            )
        except ValidationError as exc:
            raise InvalidPlanStateError(
                f"plan entity {record.id!r} has an invalid abandonment condition: {exc}"
            ) from exc
        hypotheses = await self._load_hypotheses(graph, phase, hypothesis_ids)
        steps = await self._load_steps(graph, record.id, step_count)
        return Plan(
            id=record.id,
            phase=phase,
            hypotheses=hypotheses,
            steps=steps,
            completion_conditions=completion_conditions,
            abandonment_conditions=abandonment_conditions,
            skills=(),
        )

    async def _load_steps(
        self, graph: StateGraph, plan_id: str, step_count: int
    ) -> tuple[PlanStep, ...]:
        """Load the persisted ``plan_step`` entities of ``plan_id``, in order."""
        steps: list[PlanStep] = []
        for index in range(1, step_count + 1):
            step_id = f"{plan_id}-step-{index}"
            record = await graph.get_entity(step_id)
            if record is None or record.type != ENTITY_PLAN_STEP:
                raise InvalidPlanStateError(
                    f"plan {plan_id!r} is missing its persisted step entity {step_id!r}"
                )
            try:
                # The step's id lives in the ENTITY id, not the payload (the
                # executor writes ``{hypothesis_id, objective, skill_id, ...}``),
                # so it is injected before validating the PlanStep schema.
                steps.append(PlanStep.model_validate({**record.data, "id": record.id}))
            except ValidationError as exc:
                raise InvalidPlanStateError(
                    f"plan_step entity {step_id!r} has an invalid payload: {exc}"
                ) from exc
        return tuple(steps)

    async def _load_hypotheses(
        self, graph: StateGraph, phase: Phase, hypothesis_ids: list[str]
    ) -> tuple[Hypothesis, ...]:
        """Re-read each ranked hypothesis from the current graph state."""
        hypotheses: list[Hypothesis] = []
        for rank, hypothesis_id in enumerate(hypothesis_ids, start=1):
            record = await graph.get_entity(hypothesis_id)
            if record is None or record.type != ENTITY_HYPOTHESIS:
                raise InvalidPlanStateError(
                    f"plan references hypothesis entity {hypothesis_id!r} but the graph "
                    f"holds no such {ENTITY_HYPOTHESIS!r} entity"
                )
            supporting = await _incoming_evidence_ids(
                graph, hypothesis_id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS
            )
            contradicting = await _incoming_evidence_ids(
                graph, hypothesis_id, EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
            )
            hypotheses.append(
                Hypothesis(
                    id=hypothesis_id,
                    phase=phase,
                    objective=_payload_optional_str(record, "objective")
                    or f"resolve hypothesis {hypothesis_id}",
                    rank=rank,
                    confidence=_payload_confidence(record),
                    supporting_evidence=supporting,
                    contradicting_evidence=contradicting,
                    exploitation_direction=_payload_optional_str(record, "exploitation_direction"),
                )
            )
        return tuple(hypotheses)

    async def _evidence_snapshot(
        self,
        graph: StateGraph,
        hypothesis_id: str,
        plan_created_at: datetime,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Current evidence sets for ``hypothesis_id``, plus the NEW sets.

        Returns ``(supporting, contradicting, new_supporting,
        new_contradicting)`` where \"new\" means the edge was created
        after the plan entity (the planner's conditions say \"gains new
        ... evidence\", and plan-time evidence is the ranking context,
        not a step outcome).
        """
        supporting = await _incoming_evidence_ids(
            graph, hypothesis_id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS
        )
        contradicting = await _incoming_evidence_ids(
            graph, hypothesis_id, EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
        )
        new_supporting = await _new_incoming_evidence_ids(
            graph, hypothesis_id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, plan_created_at
        )
        new_contradicting = await _new_incoming_evidence_ids(
            graph, hypothesis_id, EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS, plan_created_at
        )
        return supporting, contradicting, new_supporting, new_contradicting

    async def _evaluate_step(
        self,
        graph: StateGraph,
        step: PlanStep,
        snapshot: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None,
    ) -> StepEvaluation:
        """One step's typed outcome from the current graph state."""
        attempts = await self._attempts_for_step(graph, step.id)
        if step.hypothesis_id is None:
            return await self._evaluate_service_step(graph, step, attempts)
        supporting, contradicting, new_supporting, new_contradicting = snapshot or ((), (), (), ())
        if new_contradicting:
            outcome = StepOutcome.FAILED
            reason = f"hypothesis {step.hypothesis_id} gained new contradicting evidence"
        elif new_supporting:
            outcome = StepOutcome.COMPLETED
            reason = f"hypothesis {step.hypothesis_id} gained new supporting evidence"
        elif attempts >= self._max_attempts_per_step:
            outcome = StepOutcome.FAILED
            reason = (
                f"{attempts} attempts exceed the step budget of "
                f"{self._max_attempts_per_step}; abandoning the step"
            )
        else:
            outcome = StepOutcome.PENDING
            reason = "no completion or abandon condition holds yet"
        return StepEvaluation(
            step_id=step.id,
            outcome=outcome,
            attempts=attempts,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            reason=reason,
        )

    async def _evaluate_service_step(
        self, graph: StateGraph, step: PlanStep, attempts: int
    ) -> StepEvaluation:
        """A service step: characterized completes, absent or budget-spent fails."""
        match = _SERVICE_CHARACTERIZED_RE.match(step.completion_condition)
        if match is None:
            raise InvalidPlanStateError(
                f"step {step.id!r} has no hypothesis and an unrecognized completion condition "
                f"{step.completion_condition!r}; expected the planner's 'service <id> is characterized'"
            )
        service_id = match.group(1)
        record = await graph.get_entity(service_id)
        if record is None:
            outcome = StepOutcome.FAILED
            reason = f"service {service_id} is unreachable or absent"
        elif _payload_bool(record, FIELD_CHARACTERIZED):
            outcome = StepOutcome.COMPLETED
            reason = f"service {service_id} is characterized"
        elif attempts >= self._max_attempts_per_step:
            outcome = StepOutcome.FAILED
            reason = (
                f"{attempts} attempts exceed the step budget of "
                f"{self._max_attempts_per_step}; abandoning the step"
            )
        else:
            outcome = StepOutcome.PENDING
            reason = f"service {service_id} is not characterized yet"
        return StepEvaluation(
            step_id=step.id,
            outcome=outcome,
            attempts=attempts,
            supporting_evidence=(),
            contradicting_evidence=(),
            reason=reason,
        )

    async def _evaluate_hypothesis(
        self,
        graph: StateGraph,
        hypothesis: Hypothesis,
        snapshot: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
        attempts: int,
        use_model_fallback: bool,
    ) -> HypothesisEvaluation:
        """One ranked hypothesis's typed verdict.

        Deterministic rules first: NEW contradicting evidence refutes
        (contradiction outranks support — conservative), an exhausted
        step attempt budget refutes (loop recovery), NEW supporting
        evidence with no NEW contradiction confirms. Only a hypothesis
        the deterministic rules leave undetermined may reach the model
        fallback.
        """
        supporting, contradicting, new_supporting, new_contradicting = snapshot
        if new_contradicting:
            verdict = HypothesisVerdict.REFUTED
            reason = "gained new contradicting evidence"
        elif attempts >= self._max_attempts_per_step:
            verdict = HypothesisVerdict.REFUTED
            reason = f"step attempt budget exhausted ({attempts} attempts)"
        elif new_supporting:
            verdict = HypothesisVerdict.CONFIRMED
            reason = "gained new supporting evidence with no contradictions"
        else:
            verdict = HypothesisVerdict.UNDETERMINED
            reason = "no deterministic signal"
            if use_model_fallback:
                verdict, model_reason = await self._fallback_verdict(graph, hypothesis)
                reason = f"model fallback: {model_reason}"
        return HypothesisEvaluation(
            hypothesis_id=hypothesis.id,
            verdict=verdict,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            reason=reason,
        )

    async def _abandonment_reason(
        self,
        graph: StateGraph,
        plan: Plan,
        step_evals: list[StepEvaluation],
        hyp_evals: list[HypothesisEvaluation],
        route: PhaseRoute,
    ) -> str | None:
        """The first plan-level abandonment reason, or None.

        Mirrors :data:`~ozzgraph.planner.PLAN_ABANDONMENT_CONDITIONS`
        (every ranked hypothesis refuted; the graph no longer routes to
        the plan's phase) plus the plan budgets (every step of a
        hypothesis-less plan failed; the plan's model-call budget is
        exhausted) — a plan that exhausts its budget is abandoned and
        re-planned, never looped.
        """
        if route.phase is not plan.phase:
            return "the graph no longer routes to the plan's phase"
        if plan.hypotheses and all(
            evaluation.verdict is HypothesisVerdict.REFUTED for evaluation in hyp_evals
        ):
            return "every ranked hypothesis is refuted"
        if (
            not plan.hypotheses
            and plan.steps
            and all(evaluation.outcome is StepOutcome.FAILED for evaluation in step_evals)
        ):
            return "every plan step failed"
        if len(plan.steps) > MAX_PLAN_STEPS:
            return f"the plan carries more steps than the cap of {MAX_PLAN_STEPS}"
        if await self._model_calls_for_plan(graph, plan.id) >= self._max_model_calls_per_plan:
            return f"the plan's model-call budget of {self._max_model_calls_per_plan} is exhausted"
        return None

    async def _decide_abandoned(
        self,
        graph: StateGraph,
        plan: Plan,
        route: PhaseRoute,
        abandoned_reason: str,
    ) -> tuple[PlanVerdict, str | None, str]:
        """Decide the abandoned plan's fate: replan or abandon with no successor.

        The replacement plan is built for the CURRENT graph state (before
        any of the evaluator's own persistence mutates the graph), and is
        persisted with the executor's persistence pattern plus a ``PLAN
        SUPERSEDES PLAN`` edge from the new plan to the old. If
        replanning would rebuild the identical plan, or no replacement is
        derivable (non-branching graph; the routed phase has no skill
        packs), the plan is abandoned with no successor — the evaluator
        never loops on a failing plan.
        """
        decision = await self._try_replan(graph, route)
        if decision is None:
            return (
                PlanVerdict.ABANDON,
                None,
                f"{abandoned_reason}; no replacement plan derivable for the current graph state",
            )
        if decision.id == plan.id:
            return (
                PlanVerdict.ABANDON,
                None,
                (
                    f"{abandoned_reason}; replanning would rebuild the identical plan "
                    f"{plan.id!r}, refusing to loop"
                ),
            )
        await self._persist_plan(graph, decision)
        await self._create_edge(
            graph,
            f"{decision.id}-supersedes-{plan.id}",
            EDGE_PLAN_SUPERSEDES_PLAN,
            decision.id,
            plan.id,
        )
        self._append(
            EVALUATOR_PLAN_REPLANNED,
            {
                "old_plan_id": plan.id,
                "new_plan_id": decision.id,
                "phase": plan.phase.value,
                "reason": abandoned_reason,
            },
        )
        return (
            PlanVerdict.REPLAN,
            decision.id,
            f"{abandoned_reason}; replanned as {decision.id}",
        )

    async def _try_replan(self, graph: StateGraph, route: PhaseRoute) -> Plan | None:
        """A replacement plan for the current graph state, or None.

        A non-branching graph yields a :class:`NoPlanDecision` (no
        replacement), and a routed phase with no skill packs raises the
        planner's typed error — both are expected abandonment outcomes,
        not failures. Real graph corruption (wrong-typed payloads,
        hypotheses without evidence refs) still propagates loudly.
        """
        try:
            decision = await self._planner.plan(graph, route)
        except PlannerSkillUnavailableError:
            return None
        if isinstance(decision, Plan):
            return decision
        return None

    async def _fallback_verdict(
        self, graph: StateGraph, hypothesis: Hypothesis
    ) -> tuple[HypothesisVerdict, str]:
        """Ask the model client for one hypothesis verdict, under budget.

        Invoked ONLY when the deterministic rules left the hypothesis
        undetermined. The completion must be exactly one JSON object
        (``verdict`` + ``reason``); the adapters' JSON repair strategy
        (fence strip, then first balanced object) is applied before
        giving up loudly. One model call is checked against and consumed
        from the injected budget, mirroring the executor's accounting.
        """
        if self._model_client is None or self._model_name is None:
            return HypothesisVerdict.UNDETERMINED, "no model fallback configured"
        self._check_fallback_budget()
        context = await self._fallback_context(graph, hypothesis)
        request = ModelRequest(
            model=self._model_name,
            messages=[
                ModelMessage(role="system", content=_FALLBACK_SYSTEM_PROMPT),
                ModelMessage(
                    role="user",
                    content=(
                        f"{context}\n\nDecide the hypothesis verdict and respond with "
                        "exactly the JSON object described above."
                    ),
                ),
            ],
            temperature=0.0,
            max_tokens=FALLBACK_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        response = await self._model_client.complete(request)
        if not response.choices:
            raise MalformedEvaluatorOutputError("model fallback returned no completion choices")
        content = response.choices[0].message.content
        if content is None or not content.strip():
            raise MalformedEvaluatorOutputError("model fallback returned an empty completion")
        parsed = _parse_fallback_json(content)
        try:
            verdict = _ModelVerdict.model_validate(parsed)
        except ValidationError as exc:
            raise MalformedEvaluatorOutputError(
                f"model fallback output violates the verdict contract: {exc}"
            ) from exc
        return verdict.verdict, verdict.reason

    def _check_fallback_budget(self) -> None:
        """Check and consume one model call for a fallback completion.

        Mirrors the executor's budget accounting (check before consume;
        an exhausted budget fails loudly with :class:`BudgetExceeded`).
        """
        if self._budgets is None:
            return
        remaining = self._budgets.remaining_model_calls()
        if remaining is not None and remaining <= 0:
            used = self._budgets.model_calls_used()
            raise BudgetExceeded(BudgetKind.MODEL_CALLS, used, used + 1)
        self._budgets.consume_model_call()

    async def _fallback_context(self, graph: StateGraph, hypothesis: Hypothesis) -> str:
        """Bounded evidence context for one fallback completion.

        Hypothesis objective plus the supporting/contradicting evidence
        ids and (best-effort) the summaries of observations the evidence
        was extracted from, capped at :data:`FALLBACK_CONTEXT_LIMIT`.
        """
        lines = [f"hypothesis: {hypothesis.objective}"]
        if hypothesis.supporting_evidence:
            lines.append("supporting evidence: " + ", ".join(hypothesis.supporting_evidence))
        if hypothesis.contradicting_evidence:
            lines.append("contradicting evidence: " + ", ".join(hypothesis.contradicting_evidence))
        for summary in await self._observation_summaries(
            graph, hypothesis.supporting_evidence + hypothesis.contradicting_evidence
        ):
            lines.append(f"observation: {summary}")
        return "\n".join(lines)[:FALLBACK_CONTEXT_LIMIT]

    async def _observation_summaries(
        self, graph: StateGraph, evidence_ids: tuple[str, ...]
    ) -> list[str]:
        """Bounded observation summaries attached to the evidence entities.

        Read defensively: the ``EVIDENCE EXTRACTED_FROM OBSERVATION``
        edge direction is resolved from either endpoint, and only
        observation-typed entities with a string ``summary`` payload
        contribute. This is fallback CONTEXT only — never a decision
        input for the deterministic rules.
        """
        summaries: list[str] = []
        for evidence_id in evidence_ids:
            neighbors = await graph.neighbors(evidence_id, EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION)
            for edge in (*neighbors.incoming, *neighbors.outgoing):
                observation_id = edge.dst_id if edge.dst_id != evidence_id else edge.src_id
                record = await graph.get_entity(observation_id)
                if record is None or record.type != ENTITY_OBSERVATION:
                    continue
                summary = record.data.get("summary")
                if isinstance(summary, str) and summary:
                    summaries.append(f"{observation_id}: {summary[:FALLBACK_SUMMARY_LIMIT]}")
                    if len(summaries) >= FALLBACK_MAX_SUMMARIES:
                        return summaries
        return summaries

    async def _attempts_for_step(self, graph: StateGraph, step_id: str) -> int:
        """Action entities bound to ``step_id`` (one per approved turn)."""
        actions = await graph.list_entities(ENTITY_ACTION)
        return sum(1 for record in actions if record.data.get("plan_step_id") == step_id)

    async def _model_calls_for_plan(self, graph: StateGraph, plan_id: str) -> int:
        """Action entities bound to ``plan_id`` — the plan's model-call spend.

        Each approved executor turn consumes exactly one model call and
        records exactly one action entity bound to the served plan, so
        this is the deterministic, replay-consistent proxy for the
        plan's model-call budget.
        """
        actions = await graph.list_entities(ENTITY_ACTION)
        return sum(1 for record in actions if record.data.get("plan_id") == plan_id)

    async def _persist_evaluation(self, graph: StateGraph, evaluation: PlanEvaluation) -> None:
        """Persist one evaluation decision as an ``evaluation`` entity.

        The entity id is ``eval-<plan id>-<seq>`` where ``seq`` follows
        the existing evaluation entities of that plan, so identical
        graphs yield identical ids. The mutation is mirrored to the event
        log as a ``graph.*`` event with the same timestamp, and the
        decision is also recorded as an ``evaluator.plan_evaluated`` run
        event.
        """
        prefix = f"eval-{evaluation.plan_id}-"
        existing = await graph.list_entities(ENTITY_EVALUATION)
        sequence = sum(1 for record in existing if record.id.startswith(prefix)) + 1
        payload = evaluation.model_dump(mode="json")
        await self._create_entity(graph, f"{prefix}{sequence}", ENTITY_EVALUATION, payload)
        self._append(EVALUATOR_PLAN_EVALUATED, payload)

    async def _persist_plan(self, graph: StateGraph, plan: Plan) -> None:
        """Persist ``plan`` as graph entities, idempotently (executor pattern).

        Mirrors the executor's persistence: one ``plan`` entity, one
        ``plan_step`` entity per step, and a ``PLANSTEP TESTS HYPOTHESIS``
        edge per hypothesis-testing step, each mutation mirrored to the
        event log as a ``graph.*`` event with the same timestamp. A plan
        id already present is never rewritten.
        """
        if await graph.get_entity(plan.id) is not None:
            return
        await self._create_entity(
            graph,
            plan.id,
            ENTITY_PLAN,
            {
                "phase": plan.phase.value,
                "step_count": len(plan.steps),
                "hypotheses": [hypothesis.id for hypothesis in plan.hypotheses],
                "completion_conditions": list(plan.completion_conditions),
                "abandonment_conditions": [
                    condition.model_dump(mode="json") for condition in plan.abandonment_conditions
                ],
            },
        )
        for step in plan.steps:
            await self._create_entity(
                graph,
                step.id,
                ENTITY_PLAN_STEP,
                {
                    "hypothesis_id": step.hypothesis_id,
                    "objective": step.objective,
                    "skill_id": step.skill_id,
                    "completion_condition": step.completion_condition,
                    "abandon_condition": step.abandon_condition.model_dump(mode="json"),
                },
            )
            if step.hypothesis_id is not None:
                await self._create_edge(
                    graph,
                    f"{step.id}-tests-{step.hypothesis_id}",
                    EDGE_PLANSTEP_TESTS_HYPOTHESIS,
                    step.id,
                    step.hypothesis_id,
                )

    async def _create_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        entity_type: str,
        data: dict[str, object],
    ) -> None:
        """Create one entity and mirror the mutation to the event log."""
        at = datetime.now(UTC)
        await graph.create_entity(entity_id, entity_type, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    self._run_id,
                    EVALUATOR_PRODUCER,
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        data=data,
                        at=at,
                    ),
                )
            )

    async def _create_edge(
        self,
        graph: StateGraph,
        edge_id: str,
        edge_type: str,
        src_id: str,
        dst_id: str,
    ) -> None:
        """Create one edge and mirror the mutation to the event log."""
        at = datetime.now(UTC)
        await graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    self._run_id,
                    EVALUATOR_PRODUCER,
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=edge_type,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=at,
                    ),
                )
            )

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one evaluator run event when an event log is configured."""
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=EVALUATOR_PRODUCER,
                    payload=payload,
                )
            )


async def _new_incoming_evidence_ids(
    graph: StateGraph,
    entity_id: str,
    edge_type: str,
    after: datetime,
) -> tuple[str, ...]:
    """Evidence ids with an incoming ``edge_type`` edge created after ``after``.

    Ordered by edge id (``StateGraph.neighbors`` orders deterministically),
    so the tuple is stable for a given graph state.
    """
    neighbors = await graph.neighbors(entity_id, edge_type)
    return tuple(edge.src_id for edge in neighbors.incoming if edge.created_at > after)


async def _incoming_evidence_ids(
    graph: StateGraph, entity_id: str, edge_type: str
) -> tuple[str, ...]:
    """Evidence entity ids with an incoming ``edge_type`` edge to ``entity_id``."""
    neighbors = await graph.neighbors(entity_id, edge_type)
    return tuple(edge.src_id for edge in neighbors.incoming)


def _payload_bool(record: EntityRecord, key: str) -> bool:
    """Read a strict-boolean payload field, defaulting to False.

    Raises:
        InvalidPlanStateError: If ``key`` is present on the record's
            payload and is not a bool (fail loudly, never coerced).
    """
    value = record.data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InvalidPlanStateError(
            f"entity {record.id!r} payload field {key!r} must be a bool, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def _payload_confidence(record: EntityRecord) -> float:
    """Read the hypothesis ``confidence`` payload field, defaulting to 0.0.

    Raises:
        InvalidPlanStateError: If ``confidence`` is present and not a
            number in [0.0, 1.0].
    """
    value = record.data.get("confidence")
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPlanStateError(
            f"entity {record.id!r} payload field 'confidence' must be a "
            f"number in [0.0, 1.0], got {type(value).__name__} ({value!r})"
        )
    if not 0.0 <= value <= 1.0:
        raise InvalidPlanStateError(
            f"entity {record.id!r} payload field 'confidence' must be in [0.0, 1.0], got {value!r}"
        )
    return float(value)


def _payload_optional_str(record: EntityRecord, key: str) -> str | None:
    """Read an optional non-empty string payload field.

    Raises:
        InvalidPlanStateError: If ``key`` is present and is not a
            non-empty string.
    """
    value = record.data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidPlanStateError(
            f"entity {record.id!r} payload field {key!r} must be a non-empty "
            f"string, got {type(value).__name__} ({value!r})"
        )
    return value


def _parse_fallback_json(text: str) -> dict[str, object]:
    """Parse the fallback completion into a JSON object, repairing if needed.

    Tries the raw text, then the adapters' repair strategy — markdown
    fence strip, then the first balanced ``{...}`` object — and raises
    :class:`MalformedEvaluatorOutputError` when no candidate parses as a
    JSON object.

    Raises:
        MalformedEvaluatorOutputError: If no candidate parses as a JSON
            object.
    """
    candidates = [text]
    stripped = _strip_code_fence(text)
    if stripped is not None:
        candidates.append(stripped)
    balanced = _first_balanced_object(text)
    if balanced is not None:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise MalformedEvaluatorOutputError(
        "model fallback output is not a JSON object: "
        + (text[:200] if text else "empty completion")
    )


#: The strict system prompt for model-fallback completions.
_FALLBACK_SYSTEM_PROMPT = (
    "You are the model fallback of a deterministic evaluator in an autonomous "
    "CTF harness. Given a hypothesis and its evidence, decide whether the "
    "hypothesis is confirmed, refuted, or undetermined. Base your decision ONLY "
    "on the provided evidence. Respond with exactly one JSON object and nothing "
    'else: {"verdict": "confirmed" | "refuted" | "undetermined", "reason": '
    '"<bounded explanation>"}.'
)
