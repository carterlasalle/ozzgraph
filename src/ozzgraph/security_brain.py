"""V06 security-brain — opportunity-driven planning for OzzGraph.

Implements milestone 6 of docs/CHANGES_v2.md: the deterministic
round-robin planner call in the runner's investigate loop is replaced
by a security-brain pipeline:

    OpportunityGenerator -> decide -> TaskBuilder -> execute
                              |
                              +-> StrategicPlanner (LLM, invoked ONLY
                                  when > 1 viable path exists)

Components:

- :class:`OpportunityGenerator` derives scored, ranked candidate
  opportunities from the authoritative graph state plus the routed
  phase — evidenced hypotheses and uncharacterized services, the same
  strategic-path vocabulary the deterministic planner reads. A
  ``characterize_service`` opportunity carries a fully deterministic
  bounded action; a ``test_hypothesis`` opportunity carries none
  (choosing a probe is judgment, not derivation).

- :class:`StrategicPlanner` is the LLM-driven planner. It is invoked
  ONLY when more than one viable path exists; otherwise it is never
  called, so the single-path runner makes no LLM round-trip for it.
  It derives the deterministic binding plan (executor parity — the
  executor independently derives the same plan from the same graph)
  and the bounded strategic context the runner presents to the model.

- :class:`TaskBuilder` converts a chosen opportunity or plan into a
  bounded :class:`BoundedTask` (one command, one skill, plan binding)
  that the executor consumes through its strict one-action-per-turn
  contract (AGENTS.md rule #4).

- :class:`HypothesisManager` owns the hypothesis lifecycle — create,
  attach evidence, promote (resolved/confirmed), abandon — reusing the
  :class:`~ozzgraph.planner.Hypothesis` model and the graph's
  ``hypothesis`` entity plus ``EVIDENCE SUPPORTS/CONTRADICTS
  HYPOTHESIS`` edges. Lifecycle state is a ``status`` payload field
  (``open`` / ``promoted`` / ``abandoned``); promoted and abandoned
  hypotheses no longer generate opportunities, so dead or finished
  paths never resurface (AGENTS.md Forbidden Shortcuts: no retrying
  forever).

- :class:`ProgressEvaluator` evaluates progress toward the objectives
  and decides continue / pivot / finish from deterministic graph
  predicates (AGENTS.md rule #8: predicates, not counts).

The public :class:`~ozzgraph.planner.Planner` API is untouched — the
executor and evaluator still consume it, and the strategic planner
reuses it for the deterministic binding plan.

Runner wiring (docs/CHANGES_v2.md milestone 6): ``decide`` returns a
typed :class:`BrainDecision`:

- :class:`DeterministicActionDecision` — exactly one obvious action
  (a single uncharacterized service): the runner executes the task
  with ZERO LLM calls.
- :class:`StrategicDecision` — more than one viable path: the runner
  calls the model (the StrategicPlanner) with the ranked
  opportunities in context and executes its chosen action.
- :class:`FallbackDecision` — zero or one non-obvious path: the
  runner keeps the standard model-propose path unchanged (a lone
  hypothesis needs judgment to test, and an empty graph needs a
  model-chosen direction).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.adapters import ParsedAction
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_UPDATED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    GraphEntityUpdated,
    graph_event,
)
from ozzgraph.executor import ENTITY_ACTION, FailedAction
from ozzgraph.phases import Phase
from ozzgraph.planner import (
    EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS,
    InvalidGraphStateError,
    Plan,
    Planner,
    PlanStep,
)
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    ENTITY_HYPOTHESIS,
    ENTITY_OBJECTIVE,
    ENTITY_SERVICE,
    FIELD_CHARACTERIZED,
    FIELD_COMPLETED,
    FIELD_EXPLOITABLE,
    PhaseRoute,
)
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Producer name on security-brain graph/run events.
BRAIN_PRODUCER = "security_brain"

#: Run-log event for a deterministic single-obvious action execution.
BRAIN_DETERMINISTIC_ACTION = "brain.deterministic_action"

#: Run-log event for a progress evaluation that decided pivot.
BRAIN_PROGRESS_EVALUATED = "brain.progress_evaluated"

#: Run-log events for hypothesis lifecycle transitions.
BRAIN_HYPOTHESIS_PROMOTED = "brain.hypothesis_promoted"
BRAIN_HYPOTHESIS_ABANDONED = "brain.hypothesis_abandoned"

#: Deterministic cap on derived opportunities per turn.
MAX_OPPORTUNITIES = 8

#: Hypothesis lifecycle payload field and values. The field is
#: additive to the payload the runner stamps (the planner and
#: evaluator never read it); a missing status means the hypothesis is
#: open.
FIELD_STATUS = "status"
STATUS_OPEN = "open"
STATUS_PROMOTED = "promoted"
STATUS_ABANDONED = "abandoned"

#: Hypothesis payload field the generator reads for the opportunity
#: objective (same convention as :mod:`ozzgraph.planner`).
FIELD_OBJECTIVE = "objective"

#: Entity type the generator reads for the executed-command exclusion.
ENTITY_EVIDENCE = "evidence"


class BrainError(RuntimeError):
    """Base error for the security-brain layer (AGENTS.md rule #9)."""


class OpportunityKind(str, Enum):
    """The typed kinds of opportunities the generator derives."""

    TEST_HYPOTHESIS = "test_hypothesis"
    CHARACTERIZE_SERVICE = "characterize_service"


class Opportunity(BaseModel):
    """One scored candidate next action derived from the graph + route.

    Attributes:
        id: Deterministic opportunity id (``opportunity-<kind>-<entity id>``).
        kind: The typed opportunity kind.
        entity_id: The ``hypothesis`` or ``service`` entity id the
            opportunity acts on.
        objective: Bounded statement of what the opportunity accomplishes.
        score: Deterministic rank score — hypotheses (1000 +
            confidence * 100 + net evidence weight) always outrank
            uncharacterized services (100), mirroring the planner's
            hypotheses-first ordering.
        rationale: Bounded justification for the opportunity.
        hypothesis_id: The hypothesis the opportunity tests, when kind
            is ``test_hypothesis``.
        action: The fully deterministic bounded command, when the
            opportunity is an obvious action (``characterize_service``);
            ``None`` for judgment opportunities.
        skill_id: The skill bound to a deterministic action (the routed
            phase's first advertised skill in registry order).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: OpportunityKind
    entity_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    score: float
    rationale: str = Field(min_length=1)
    hypothesis_id: str | None = None
    action: str | None = None
    skill_id: str | None = None


class BoundedTask(BaseModel):
    """One bounded task the executor can consume in a single turn.

    Attributes:
        command: The single bounded command line (never a multi-command
            plan, AGENTS.md rule #4).
        skill_id: The skill bound to the task — authoritative when the
            task serves a plan step, phase-advertised otherwise, exactly
            as the executor resolves it.
        plan_id: The plan the task serves, when planned.
        plan_step_id: The plan step the task implements, when planned.
        hypothesis_id: The hypothesis the step tests, when planned.
        objective: Bounded statement of what the task accomplishes.
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    plan_id: str | None = None
    plan_step_id: str | None = None
    hypothesis_id: str | None = None
    objective: str = Field(min_length=1)


class BrainDecision(BaseModel):
    """Base of the typed decisions the security brain returns.

    Attributes:
        phase: The routed phase the decision serves.
        reason: Bounded justification, recorded on runner events.
    """

    model_config = ConfigDict(extra="forbid")

    phase: Phase
    reason: str = Field(min_length=1)


class FallbackDecision(BrainDecision):
    """Zero or one non-obvious viable path: keep the model-propose path.

    The runner calls the model exactly as before (no strategic
    context): a fresh graph needs a model-chosen direction, and a lone
    hypothesis needs judgment to test.
    """


class DeterministicActionDecision(BrainDecision):
    """Exactly one obvious action exists: execute it with no LLM call.

    Attributes:
        opportunity: The single obvious opportunity.
        task: The bounded task derived from it (deterministic command).
    """

    opportunity: Opportunity
    task: BoundedTask


class StrategicDecision(BrainDecision):
    """More than one viable path: the StrategicPlanner (LLM) is invoked.

    Attributes:
        opportunities: The ranked viable paths the runner presents to
            the model (the strategic context).
        plan: The deterministic binding plan for executor parity
            (:class:`~ozzgraph.planner.Plan`), or ``None`` for a
            mixed-path graph the deterministic planner does not branch
            on (one hypothesis + one service).
        strategy_prompt: The bounded strategic context (the ranked
            opportunities) appended to the model prompt.
    """

    opportunities: tuple[Opportunity, ...]
    plan: Plan | None = None
    strategy_prompt: str = Field(min_length=1)


class StrategicPlan(BaseModel):
    """The typed strategic plan for a branching graph.

    Attributes:
        opportunities: The ranked viable paths.
        plan: The deterministic binding plan, or ``None`` for a
            mixed-path graph.
        strategy_prompt: The bounded strategic context for the model.
    """

    model_config = ConfigDict(extra="forbid")

    opportunities: tuple[Opportunity, ...]
    plan: Plan | None = None
    strategy_prompt: str = Field(min_length=1)


class ProgressVerdict(str, Enum):
    """The typed progress decision toward the objectives.

    Attributes:
        CONTINUE: No completion or pivot predicate holds.
        PIVOT: Every hypothesis is resolved (promoted or abandoned)
            and the objectives are not complete — every strategic path
            is dead or done, so the run needs a new direction.
        FINISH: Every objective is completed (the generic DONE path).
    """

    CONTINUE = "continue"
    PIVOT = "pivot"
    FINISH = "finish"


class ProgressEvaluation(BaseModel):
    """The typed progress decision with the stats behind it.

    Attributes:
        verdict: The typed :class:`ProgressVerdict`.
        reason: Bounded deterministic justification.
        open_hypotheses: Hypotheses with no terminal status.
        promoted_hypotheses: Hypotheses resolved as confirmed.
        abandoned_hypotheses: Hypotheses abandoned as refuted.
        evidence_count: ``evidence`` entities in the graph.
        completed_objectives / total_objectives: Objective completion.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: ProgressVerdict
    reason: str = Field(min_length=1)
    open_hypotheses: int = Field(ge=0)
    promoted_hypotheses: int = Field(ge=0)
    abandoned_hypotheses: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    completed_objectives: int = Field(ge=0)
    total_objectives: int = Field(ge=0)


class OpportunityGenerator:
    """Derives scored, ranked candidate opportunities from graph + route.

    Deterministic: the same graph state always yields the same
    opportunities. Only OPEN hypotheses (no ``status`` payload, or a
    status other than ``promoted``/``abandoned``) with at least one
    evidence ref generate ``test_hypothesis`` opportunities, and only
    uncharacterized services whose deterministic probe was not already
    executed generate ``characterize_service`` opportunities — dead,
    finished, or already-attempted paths never resurface.

    Args:
        max_opportunities: Cap on derived opportunities per turn.
    """

    def __init__(self, *, max_opportunities: int = MAX_OPPORTUNITIES) -> None:
        if max_opportunities < 1:
            raise ValueError("max_opportunities must be >= 1")
        self._max_opportunities = max_opportunities

    async def generate(self, graph: StateGraph, route: PhaseRoute) -> tuple[Opportunity, ...]:
        """The ranked opportunities for ``graph`` under ``route``.

        Hypothesis opportunities always outrank service opportunities
        (the planner's hypotheses-first ordering). Within hypotheses,
        the score encodes confidence descending, then net evidence
        weight descending; ties break on entity id — deterministic, no
        randomness, no model calls.

        Raises:
            InvalidGraphStateError: If a payload field the generator
                reads is present but wrong-typed (a strict-boolean
                ``characterized``/``completed`` or an out-of-range
                ``confidence``).
        """
        candidates = [
            *await self._hypothesis_opportunities(graph),
            *await self._service_opportunities(graph, route),
        ]
        candidates.sort(key=lambda item: (-item.score, item.entity_id))
        return tuple(candidates[: self._max_opportunities])

    async def _hypothesis_opportunities(self, graph: StateGraph) -> list[Opportunity]:
        """One scored opportunity per open, evidenced hypothesis."""
        opportunities: list[Opportunity] = []
        for record in await graph.list_entities(ENTITY_HYPOTHESIS):
            if _payload_status(record) in (STATUS_PROMOTED, STATUS_ABANDONED):
                continue
            supporting = await _incoming_evidence_ids(
                graph, record.id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS
            )
            contradicting = await _incoming_evidence_ids(
                graph, record.id, EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
            )
            if not supporting and not contradicting:
                continue  # a bare hypothesis is not a strategic path
            confidence = _payload_confidence(record)
            net_weight = len(supporting) - len(contradicting)
            opportunities.append(
                Opportunity(
                    id=f"opportunity-test_hypothesis-{record.id}",
                    kind=OpportunityKind.TEST_HYPOTHESIS,
                    entity_id=record.id,
                    objective=_payload_optional_str(record, FIELD_OBJECTIVE)
                    or f"resolve hypothesis {record.id}",
                    score=round(1000.0 + confidence * 100.0 + net_weight, 4),
                    rationale=(
                        f"hypothesis {record.id} has {len(supporting)} supporting and "
                        f"{len(contradicting)} contradicting evidence"
                    ),
                    hypothesis_id=record.id,
                )
            )
        return opportunities

    async def _service_opportunities(
        self, graph: StateGraph, route: PhaseRoute
    ) -> list[Opportunity]:
        """One scored, deterministic opportunity per uncharacterized service."""
        if not route.skills:
            return []
        executed = await _executed_commands(graph)
        opportunities: list[Opportunity] = []
        for record in await graph.list_entities(ENTITY_SERVICE):
            if _payload_bool(record, FIELD_CHARACTERIZED):
                continue
            command = _service_characterize_command(record)
            if command in executed:
                continue  # already attempted; never loop the same probe
            opportunities.append(
                Opportunity(
                    id=f"opportunity-characterize_service-{record.id}",
                    kind=OpportunityKind.CHARACTERIZE_SERVICE,
                    entity_id=record.id,
                    objective=f"characterize service {record.id}",
                    score=100.0,
                    rationale=f"service {record.id} is not characterized yet",
                    action=command,
                    skill_id=route.skills[0].skill_id,
                )
            )
        return opportunities


class StrategicPlanner:
    """LLM-driven planner invoked ONLY when >1 viable path exists.

    The planner itself never calls the model — the runner performs the
    completion with the :attr:`StrategicPlan.strategy_prompt` in
    context (the same one-action contract as the propose path). This
    class derives the deterministic binding plan and the strategic
    context; :meth:`plan` is never called for a single-path graph, so
    the single-obvious path makes no LLM round-trip.

    Args:
        planner: The deterministic planner used for the binding plan
            (executor parity — the executor independently derives the
            same plan from the same graph); defaults to a fresh
            :class:`~ozzgraph.planner.Planner`.
    """

    def __init__(self, planner: Planner | None = None) -> None:
        self._planner = planner if planner is not None else Planner()

    async def plan(
        self,
        graph: StateGraph,
        route: PhaseRoute,
        opportunities: tuple[Opportunity, ...],
    ) -> StrategicPlan:
        """The strategic plan for a branching graph.

        The binding plan is the deterministic planner's plan when the
        graph branches on it (two or more evidenced hypotheses or
        uncharacterized services); a mixed-path graph (one hypothesis
        plus one service) yields ``plan=None`` and the runner then
        binds no plan step, mirroring the executor.
        """
        decision = await self._planner.plan(graph, route)
        plan = decision if isinstance(decision, Plan) else None
        return StrategicPlan(
            opportunities=opportunities,
            plan=plan,
            strategy_prompt=_strategic_prompt(opportunities),
        )


class TaskBuilder:
    """Converts a chosen opportunity/plan into a bounded task.

    The binding mirrors the executor's step-selection rules: with a
    plan, the first step with no failed attempt binds the task (its
    skill is authoritative); without a plan, the first advertised
    phase skill binds.
    """

    async def build_deterministic(self, route: PhaseRoute, opportunity: Opportunity) -> BoundedTask:
        """The bounded task for one obvious deterministic opportunity.

        Raises:
            BrainError: If the opportunity carries no deterministic
                action (only ``characterize_service`` opportunities
                do; the generator guarantees it).
        """
        if opportunity.action is None or opportunity.skill_id is None:
            raise BrainError(
                f"opportunity {opportunity.id!r} carries no deterministic action; "
                "only characterize_service opportunities are obvious"
            )
        return BoundedTask(
            command=opportunity.action,
            skill_id=opportunity.skill_id,
            hypothesis_id=opportunity.hypothesis_id,
            objective=opportunity.objective,
        )

    async def build_strategic(
        self,
        route: PhaseRoute,
        plan: Plan | None,
        parsed: ParsedAction,
        failed_actions: Sequence[FailedAction],
    ) -> BoundedTask | None:
        """The bounded task for the model-chosen strategic action.

        Returns ``None`` when no skill can bind the task — every plan
        step has a failed attempt (re-plan, never loop) or the routed
        phase advertises no skills.

        Args:
            route: The routed phase (fallback skill source).
            plan: The deterministic binding plan, or ``None`` for a
                mixed-path graph.
            parsed: The model's parsed action (the strategic choice).
            failed_actions: Previously failed actions; their steps are
                skipped exactly as the executor skips them.
        """
        command = parsed.payload or ""
        if plan is not None:
            step = _first_unfailed_step(plan, failed_actions)
            if step is None:
                return None
            return BoundedTask(
                command=command,
                skill_id=step.skill_id,
                plan_id=plan.id,
                plan_step_id=step.id,
                hypothesis_id=step.hypothesis_id,
                objective=step.objective,
            )
        if not route.skills:
            return None
        return BoundedTask(
            command=command,
            skill_id=route.skills[0].skill_id,
            objective="strategic action under a mixed-path graph",
        )


class HypothesisManager:
    """Owns the hypothesis lifecycle on the authoritative graph.

    Lifecycle: :meth:`create` (form from an observation, with the
    deterministic confidence the runner computed) ->
    :meth:`attach_evidence` (link new evidence, supporting or
    contradicting) -> :meth:`promote` (resolved/confirmed: terminal, a
    finding backs it) or :meth:`abandon` (refuted: terminal, never
    re-opportunized). Promoted and abandoned hypotheses carry a
    ``status`` payload field the generator reads; the planner and
    evaluator never read it (additive payload state).

    Every mutation mirrors a ``graph.*`` event, so replaying the run
    log reconstructs the identical graph hash (AGENTS.md rule #1).

    Args:
        event_log: Optional append-only log mutations and lifecycle
            events are mirrored into.
        run_id: Run identifier recorded on every event.
    """

    def __init__(self, *, event_log: EventLog | None = None, run_id: str = "") -> None:
        self._event_log = event_log
        self._run_id = run_id

    async def create(
        self,
        graph: StateGraph,
        *,
        hypothesis_id: str,
        objective: str,
        exploitation_direction: str,
        confidence: float,
        evidence_id: str,
        cwe: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Create one hypothesis entity and link its first evidence.

        Idempotent: the entity id derives from the producing action's
        fingerprint, so re-persistence writes nothing new.

        Args:
            graph: The authoritative state graph.
            hypothesis_id: The deterministic hypothesis entity id.
            objective: Bounded statement of the claim.
            exploitation_direction: The bounded action that produced
                the supporting observation.
            confidence: The deterministic confidence (runner-computed).
            evidence_id: The first evidence entity backing the claim.
            cwe: Optional CWE classification (the sensitive-data
                signal), added to the payload when set.
            at: Mutation timestamp; defaults to now.
        """
        timestamp = at if at is not None else datetime.now(UTC)
        if await graph.get_entity(hypothesis_id) is None:
            payload: dict[str, object] = {
                "confidence": confidence,
                "objective": objective,
                "exploitation_direction": exploitation_direction,
                FIELD_STATUS: STATUS_OPEN,
            }
            if cwe is not None:
                payload["cwe"] = cwe
            await self._create_entity(graph, hypothesis_id, ENTITY_HYPOTHESIS, payload, timestamp)
        await self.attach_evidence(
            graph,
            hypothesis_id=hypothesis_id,
            evidence_id=evidence_id,
            supports=True,
            at=timestamp,
        )

    async def attach_evidence(
        self,
        graph: StateGraph,
        *,
        hypothesis_id: str,
        evidence_id: str,
        supports: bool,
        at: datetime | None = None,
    ) -> None:
        """Link ``evidence_id`` to the hypothesis, supporting or contradicting.

        Idempotent: the edge id derives from the evidence and
        hypothesis ids, so re-linking writes nothing new.
        """
        timestamp = at if at is not None else datetime.now(UTC)
        edge_type = (
            EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS if supports else EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
        )
        direction = "supports" if supports else "contradicts"
        edge_id = f"{evidence_id}-{direction}-{hypothesis_id}"
        if await graph.get_edge(edge_id) is None:
            await self._create_edge(
                graph, edge_id, edge_type, evidence_id, hypothesis_id, timestamp
            )
        if supports:
            # LOCAL-PHASE-GAP fix: a hypothesis with supporting evidence
            # AND a real vulnerability signal (a CWE classification —
            # stamped when the evidence matched the flag pattern)
            # becomes EXPLOITABLE: the router's
            # `has_supported_exploitable_hypothesis` predicate
            # (Phase.EXPLOITATION) matches only hypotheses stamped
            # `exploitable: true` with an incoming support edge.
            # Benign recon observations (e.g. a 200 on `/` with
            # headers) carry no CWE and must NOT route the run into
            # EXPLOITATION — the policy gate there blocks recon-family
            # probes, which would prevent the run from ever fetching
            # the flag (docs/CHANGES_v2.md, LOCAL-PHASE-GAP).
            record = await graph.get_entity(hypothesis_id)
            if record is None or _payload_bool(record, FIELD_EXPLOITABLE):
                return
            if not record.data.get("cwe"):
                return
            payload = dict(record.data)
            payload[FIELD_EXPLOITABLE] = True
            await self._update_entity(graph, hypothesis_id, payload, timestamp)

    async def promote(
        self,
        graph: StateGraph,
        *,
        hypothesis_id: str,
        at: datetime | None = None,
    ) -> None:
        """Resolve/promote a hypothesis: mark it confirmed (terminal).

        Promotion overrides any prior status — a confirmed hypothesis
        becomes a finding, and evidence outranks a dead-end marking.
        Idempotent.
        """
        timestamp = at if at is not None else datetime.now(UTC)
        record = await graph.get_entity(hypothesis_id)
        if record is None:
            return  # defensive: never fail the loop on a missing entity
        payload = dict(record.data)
        payload[FIELD_STATUS] = STATUS_PROMOTED
        payload["promoted_at"] = timestamp.isoformat()
        await self._update_entity(graph, hypothesis_id, payload, timestamp)
        self._append(BRAIN_HYPOTHESIS_PROMOTED, {"hypothesis_id": hypothesis_id})

    async def abandon(
        self,
        graph: StateGraph,
        *,
        hypothesis_id: str,
        at: datetime | None = None,
    ) -> None:
        """Abandon a hypothesis: mark it refuted (terminal, never re-run).

        A promoted hypothesis is never abandoned — promotion is
        terminal. Idempotent.
        """
        timestamp = at if at is not None else datetime.now(UTC)
        record = await graph.get_entity(hypothesis_id)
        if record is None:
            return  # defensive: never fail the loop on a missing entity
        payload = dict(record.data)
        if payload.get(FIELD_STATUS) == STATUS_PROMOTED:
            return
        payload[FIELD_STATUS] = STATUS_ABANDONED
        payload["abandoned_at"] = timestamp.isoformat()
        await self._update_entity(graph, hypothesis_id, payload, timestamp)
        self._append(BRAIN_HYPOTHESIS_ABANDONED, {"hypothesis_id": hypothesis_id})

    # ------------------------------------------------------------------
    # event mirroring (AGENTS.md rule #1: replay reconstructs the hash)
    # ------------------------------------------------------------------

    async def _create_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        entity_type: str,
        data: dict[str, object],
        at: datetime,
    ) -> None:
        await graph.create_entity(entity_id, entity_type, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    self._run_id,
                    BRAIN_PRODUCER,
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
        at: datetime,
    ) -> None:
        await graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    self._run_id,
                    BRAIN_PRODUCER,
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=edge_type,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=at,
                    ),
                )
            )

    async def _update_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        data: dict[str, object],
        at: datetime,
    ) -> None:
        await graph.update_entity(entity_id, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_UPDATED,
                    self._run_id,
                    BRAIN_PRODUCER,
                    GraphEntityUpdated(entity_id=entity_id, data=data, at=at),
                )
            )

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=BRAIN_PRODUCER,
                    payload=payload,
                )
            )


class ProgressEvaluator:
    """Deterministic progress evaluation toward the objectives.

    Verdicts (AGENTS.md rule #8: predicates, not counts):

    - FINISH: every seeded objective entity is completed (the generic
      DONE path, docs/adr/0008); a graph with no objectives is never
      finish.
    - PIVOT: objectives are not all complete and every hypothesis is
      resolved (promoted or abandoned) — every strategic path is dead
      or done, so the run needs a new direction.
    - CONTINUE: otherwise.
    """

    async def evaluate(self, graph: StateGraph) -> ProgressEvaluation:
        """The typed progress decision for ``graph``.

        Raises:
            InvalidGraphStateError: If a payload field the evaluator
                reads is present but wrong-typed (``completed`` must be
                a strict bool).
        """
        objectives = await graph.list_entities(ENTITY_OBJECTIVE)
        completed = sum(1 for record in objectives if _payload_bool(record, FIELD_COMPLETED))
        open_hypotheses = 0
        promoted = 0
        abandoned = 0
        for record in await graph.list_entities(ENTITY_HYPOTHESIS):
            status = _payload_status(record)
            if status == STATUS_PROMOTED:
                promoted += 1
            elif status == STATUS_ABANDONED:
                abandoned += 1
            else:
                open_hypotheses += 1
        evidence_count = len(await graph.list_entities(ENTITY_EVIDENCE))
        total = len(objectives)

        if objectives and completed == total:
            return ProgressEvaluation(
                verdict=ProgressVerdict.FINISH,
                reason=f"all objectives completed ({completed}/{total})",
                open_hypotheses=open_hypotheses,
                promoted_hypotheses=promoted,
                abandoned_hypotheses=abandoned,
                evidence_count=evidence_count,
                completed_objectives=completed,
                total_objectives=total,
            )
        if open_hypotheses == 0 and (promoted + abandoned) > 0:
            return ProgressEvaluation(
                verdict=ProgressVerdict.PIVOT,
                reason=(
                    f"every hypothesis is resolved ({promoted} promoted, {abandoned} "
                    "abandoned) and the objectives are not complete; a new direction "
                    "is needed"
                ),
                open_hypotheses=open_hypotheses,
                promoted_hypotheses=promoted,
                abandoned_hypotheses=abandoned,
                evidence_count=evidence_count,
                completed_objectives=completed,
                total_objectives=total,
            )
        return ProgressEvaluation(
            verdict=ProgressVerdict.CONTINUE,
            reason="no completion or pivot predicate holds",
            open_hypotheses=open_hypotheses,
            promoted_hypotheses=promoted,
            abandoned_hypotheses=abandoned,
            evidence_count=evidence_count,
            completed_objectives=completed,
            total_objectives=total,
        )


class SecurityBrain:
    """The V06 security-brain facade the runner consults each turn.

    Args:
        generator: Opportunity generator; defaults to a fresh
            :class:`OpportunityGenerator`.
        strategic: Strategic planner; defaults to a fresh
            :class:`StrategicPlanner` over ``planner``.
        tasks: Task builder; defaults to a fresh :class:`TaskBuilder`.
        hypotheses: Hypothesis manager; defaults to a fresh
            :class:`HypothesisManager` over ``event_log``/``run_id``.
        progress: Progress evaluator; defaults to a fresh
            :class:`ProgressEvaluator`.
        planner: The deterministic planner shared with the executor
            (binding-plan parity); defaults to a fresh
            :class:`~ozzgraph.planner.Planner`.
        event_log: Optional append-only log the hypothesis manager
            mirrors mutations into.
        run_id: Run identifier recorded on manager events.
    """

    def __init__(
        self,
        *,
        generator: OpportunityGenerator | None = None,
        strategic: StrategicPlanner | None = None,
        tasks: TaskBuilder | None = None,
        hypotheses: HypothesisManager | None = None,
        progress: ProgressEvaluator | None = None,
        planner: Planner | None = None,
        event_log: EventLog | None = None,
        run_id: str = "",
    ) -> None:
        self._planner = planner if planner is not None else Planner()
        self._generator = generator if generator is not None else OpportunityGenerator()
        self._strategic = strategic if strategic is not None else StrategicPlanner(self._planner)
        self._tasks = tasks if tasks is not None else TaskBuilder()
        self._hypotheses = (
            hypotheses
            if hypotheses is not None
            else HypothesisManager(event_log=event_log, run_id=run_id)
        )
        self._progress = progress if progress is not None else ProgressEvaluator()

    @property
    def generator(self) -> OpportunityGenerator:
        """The opportunity generator."""
        return self._generator

    @property
    def strategic(self) -> StrategicPlanner:
        """The strategic (LLM-driven) planner."""
        return self._strategic

    @property
    def tasks(self) -> TaskBuilder:
        """The task builder."""
        return self._tasks

    @property
    def hypotheses(self) -> HypothesisManager:
        """The hypothesis lifecycle manager."""
        return self._hypotheses

    @property
    def progress(self) -> ProgressEvaluator:
        """The progress evaluator."""
        return self._progress

    async def decide(
        self,
        graph: StateGraph,
        route: PhaseRoute,
        *,
        failed_actions: Sequence[FailedAction] = (),
    ) -> BrainDecision:
        """Decide how to act this turn (no LLM call happens here).

        Decision rules:

        - Exactly one opportunity carrying a deterministic action
          (a single uncharacterized service): a
          :class:`DeterministicActionDecision` — the runner executes
          it with ZERO LLM calls.
        - More than one viable path: a :class:`StrategicDecision` —
          the runner invokes the StrategicPlanner (LLM) with the
          ranked opportunities in context.
        - Otherwise (zero paths, or a lone non-obvious hypothesis): a
          :class:`FallbackDecision` — the runner keeps the standard
          model-propose path.

        Args:
            graph: The authoritative SQLite state graph.
            route: The graph-driven phase route.
            failed_actions: Previously failed actions (skipped steps,
                per the executor's rules).

        Raises:
            InvalidGraphStateError: If a payload field the generator
                reads is wrong-typed (fail loud, AGENTS.md rule #9).
        """
        opportunities = await self._generator.generate(graph, route)
        if not opportunities:
            return FallbackDecision(
                phase=route.phase,
                reason=(
                    "no viable opportunities derived from the graph; "
                    "the model proposes the next action"
                ),
            )
        if len(opportunities) == 1:
            only = opportunities[0]
            if only.action is not None:
                task = await self._tasks.build_deterministic(route, only)
                return DeterministicActionDecision(
                    phase=route.phase,
                    reason=(
                        f"exactly one obvious action ({only.id}); "
                        "executed deterministically with no LLM call"
                    ),
                    opportunity=only,
                    task=task,
                )
            return FallbackDecision(
                phase=route.phase,
                reason=(
                    f"one viable path ({only.id}) that is not an obvious "
                    "deterministic action; the model proposes the next action"
                ),
            )
        strategic = await self._strategic.plan(graph, route, opportunities)
        return StrategicDecision(
            phase=route.phase,
            reason=(
                f"{len(opportunities)} viable paths; the StrategicPlanner (LLM) "
                "chooses the next action"
            ),
            opportunities=strategic.opportunities,
            plan=strategic.plan,
            strategy_prompt=strategic.strategy_prompt,
        )


def _strategic_prompt(opportunities: Sequence[Opportunity]) -> str:
    """The bounded strategic context presenting the ranked opportunities."""
    lines = ["STRATEGIC OPPORTUNITIES (ranked viable paths):"]
    for index, opportunity in enumerate(opportunities, start=1):
        lines.append(
            f"{index}. {opportunity.id} [{opportunity.kind.value}] "
            f"score={opportunity.score:.2f}: {_bounded(opportunity.objective, 160)}"
        )
    return "\n".join(lines)


def _service_characterize_command(record: EntityRecord) -> str:
    """The deterministic bounded probe for one uncharacterized service.

    Uses the service's stored address when present (the local-mode seed
    writes the target address onto the service), falling back to a
    probe keyed by the service's canonical entity id — a pure function
    of graph state, so the same graph always derives the same probe
    (and the same fingerprint).
    """
    address = str(record.data.get("address", "")).strip()
    if address:
        return f"curl -sS -m 5 -i {address}"
    return f"nmap -sV --top-ports 1000 {record.id}"


async def _executed_commands(graph: StateGraph) -> set[str]:
    """Command payloads of every recorded ``action`` entity.

    The authoritative already-attempted set (AGENTS.md rule #1): a
    deterministic probe whose fingerprint already executed is never
    derived again.
    """
    commands: set[str] = set()
    for record in await graph.list_entities(ENTITY_ACTION):
        command = record.data.get("command")
        if isinstance(command, str) and command:
            commands.add(command)
    return commands


async def _incoming_evidence_ids(
    graph: StateGraph, entity_id: str, edge_type: str
) -> tuple[str, ...]:
    """Evidence entity ids with an incoming ``edge_type`` edge.

    Ordered by edge id (``StateGraph.neighbors`` orders
    deterministically).
    """
    neighbors = await graph.neighbors(entity_id, edge_type)
    return tuple(edge.src_id for edge in neighbors.incoming)


def _first_unfailed_step(plan: Plan, failed_actions: Sequence[FailedAction]) -> PlanStep | None:
    """The first plan step with no failed attempt (executor parity)."""
    failed_step_ids = frozenset(
        failed.plan_step_id for failed in failed_actions if failed.plan_step_id is not None
    )
    for step in plan.steps:
        if step.id not in failed_step_ids:
            return step
    return None


def _payload_bool(record: EntityRecord, key: str) -> bool:
    """Read a strict-boolean payload field, defaulting to False.

    Mirrors :func:`ozzgraph.planner._payload_bool`: a present non-bool
    value is invalid graph state and fails loudly (AGENTS.md rule #9).

    Raises:
        InvalidGraphStateError: If ``key`` is present and not a bool.
    """
    value = record.data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {key!r} must be a bool, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def _payload_confidence(record: EntityRecord) -> float:
    """Read the hypothesis ``confidence`` payload field, defaulting to 0.0.

    Mirrors :func:`ozzgraph.planner._payload_confidence`.

    Raises:
        InvalidGraphStateError: If ``confidence`` is present and not a
            number in [0.0, 1.0].
    """
    value = record.data.get("confidence")
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field 'confidence' must be a "
            f"number in [0.0, 1.0], got {type(value).__name__} ({value!r})"
        )
    if not 0.0 <= value <= 1.0:
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field 'confidence' must be in [0.0, 1.0], got {value!r}"
        )
    return float(value)


def _payload_optional_str(record: EntityRecord, key: str) -> str | None:
    """Read an optional non-empty string payload field.

    Raises:
        InvalidGraphStateError: If ``key`` is present and is not a
            non-empty string.
    """
    value = record.data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {key!r} must be a non-empty "
            f"string, got {type(value).__name__} ({value!r})"
        )
    return value


def _payload_status(record: EntityRecord) -> str:
    """The hypothesis lifecycle status; a missing field means open."""
    value = record.data.get(FIELD_STATUS)
    if value is None:
        return STATUS_OPEN
    if not isinstance(value, str) or not value:
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {FIELD_STATUS!r} must be a "
            f"non-empty string, got {type(value).__name__} ({value!r})"
        )
    return value


def _bounded(text: str, limit: int) -> str:
    """Deterministic truncation for summaries and error messages."""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
