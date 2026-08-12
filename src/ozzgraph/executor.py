"""Bounded one-action-per-turn executor loop for OzzGraph (PR20).

Implements the EXECUTOR layer (docs/ARCHITECTURE.md, "Executor"; PR step
20 of docs/IMPLEMENTATION_PLAN.md): the deterministic loop between the
graph-driven :class:`~ozzgraph.router.PhaseRouter` (PR18) and the
:class:`~ozzgraph.planner.Planner` (PR19). One :meth:`Executor.turn`
routes the graph, plans under the routed phase, validates the model's
untrusted action proposal against a strict output contract, bounds the
approved action, records the attempt before execution, and returns
exactly ONE typed :class:`ActionRequest` — never a list.

Design rules:

- One bounded action per turn (AGENTS.md rule #4): every approved
  action carries a skill default timeout, an output limit, and a
  normalized fingerprint from the policy gate. The model proposes only
  ``action`` text plus a ``skill_id``; the executor validates that
  proposal against the strict :class:`ModelAction` contract and rejects
  anything malformed loudly (:class:`MalformedOutputError`). Multi-
  command plans disguised as one action are governed by the skill
  cards' bounded scripts, the action-length bound, and the policy gate
  — never by the executor silently unbinding an action.

- Graph-driven, deterministic (AGENTS.md rule #8): the executor routes
  the graph through :class:`PhaseRouter` and plans through
  :class:`Planner` on every turn — graph-state predicates, never
  action counts. Plans are persisted as graph entities (``plan``,
  ``plan_step``, ``PLANSTEP TESTS HYPOTHESIS``) the first time a plan
  id is seen — a plan id already in the graph is never rewritten —
  with every mutation mirrored to the append-only event log as a
  ``graph.*`` event (the PR7/PR8 pattern), so replay reconstructs the
  same graph hash. Plan ids derive from the graph hash (PR19), and the
  executor's own persistence is part of that state, so as the graph
  evolves across turns the persisted plan entities form the run's plan
  timeline; the evaluator (PR21) interprets them.

- Attempts are recorded before execution (AGENTS.md Security
  Boundaries step 10; Data Invariant "Every Observation references an
  Action"): every approved action is persisted as an ``action`` graph
  entity keyed by its fingerprint (``action-<fingerprint>``) alongside
  an ``executor.action_attempted`` run-log event, before the turn
  returns. The tool plane attaches observations to that entity later.

- Budgets (AGENTS.md rule #9): the executor checks every bounded
  budget dimension (runtime, tokens, model calls, tool calls) before
  each turn and raises :class:`~ozzgraph.budgets.BudgetExceeded`
  loudly when any is exhausted. An approved turn consumes exactly one
  model call (the call that produced the proposal) and one tool call
  (the action this turn will execute); the resulting accounting is
  carried on every :class:`ExecutorTurn`.

- Failed actions never retry (AGENTS.md Forbidden Shortcuts): the
  executor receives failed-action history, skips plan steps with a
  failed attempt, and rejects any proposal whose fingerprint was
  already attempted or already recorded (:class:`DuplicateFingerprintError`).
  A plan whose every step has failed raises
  :class:`PlanExhaustedError` — no suppressing timeouts, no retrying
  forever.

- No raw MCP (AGENTS.md rule #5): the executor only ever produces
  action TEXT — a command line or a ``halctl`` invocation, which the
  tool plane runs through the :mod:`ozzgraph.policy` gate and
  :mod:`ozzgraph.shell` runner. It never constructs an MCP client or
  calls an MCP method; ``halctl`` remains the only adapter surface.

- Small kernel (AGENTS.md rule #10): the executor owns only the turn
  loop; budgets, skills, policy, state, and events are injected.
  Nothing is wired into the supervisor yet.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
from ozzgraph.phases import Phase
from ozzgraph.planner import Plan, Planner, PlanStep
from ozzgraph.policy import DuplicateActionError, FingerprintStore, PolicyDecision, ScopePolicy
from ozzgraph.router import PhaseRoute, PhaseRouter
from ozzgraph.skills import SkillRegistry
from ozzgraph.state_graph import StateGraph

if TYPE_CHECKING:
    from ozzgraph.evaluator import Evaluator, PlanEvaluation

#: Hard bound on a single action's text length, in characters. Mirrors
#: the policy gate's default command-length ceiling so the model
#: contract and the gate agree on what a bounded action is.
MAX_ACTION_LENGTH = 4096

#: Default per-stream output cap, in characters, attached to every
#: bounded action; the tool plane passes it to the shell runner as the
#: stdout/stderr limits.
DEFAULT_OUTPUT_LIMIT = 65536

#: Producer name on every executor event.
EXECUTOR_PRODUCER = "executor"

#: Run-log event emitted for every approved, recorded action attempt.
EXECUTOR_ACTION_ATTEMPTED = "executor.action_attempted"

#: Run-log event emitted when a plan is first persisted as entities.
EXECUTOR_PLAN_PERSISTED = "executor.plan_persisted"

#: Entity types the executor writes (docs/DATA_STRATEGY.md, lowercase
#: by convention).
ENTITY_ACTION = "action"
ENTITY_PLAN = "plan"
ENTITY_PLAN_STEP = "plan_step"

#: Edge type linking a plan step to the hypothesis it tests
#: (docs/DATA_STRATEGY.md, uppercase by convention).
EDGE_PLANSTEP_TESTS_HYPOTHESIS = "PLANSTEP TESTS HYPOTHESIS"

#: sha256 hex digest shape for fingerprints (same shape the policy
#: gate produces and the fingerprint store validates).
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutorError(RuntimeError):
    """Base error for the executor layer (AGENTS.md rule #9)."""


class MalformedOutputError(ExecutorError):
    """The model's output violates the strict action contract.

    Raised when the raw model output is not a JSON object string or a
    mapping, or when it fails :class:`ModelAction` validation — missing
    or extra fields, wrong-typed values, empty or over-long action
    text. Model output is untrusted (AGENTS.md Security Boundaries) and
    is never coerced or repaired here.
    """


class InvalidSkillError(ExecutorError):
    """The model selected a skill the turn context does not allow.

    Raised when the proposed ``skill_id`` is unknown, does not cover
    the routed phase, or — when a plan step is bound — is not the
    step's assigned skill. The plan and the phase skills are
    authoritative; the model cannot dodge them.
    """


class DuplicateFingerprintError(ExecutorError):
    """The action's fingerprint was already attempted or recorded.

    Raised when the policy gate's fingerprint matches a failed action
    in the turn's history, or when the fingerprint store rejects it as
    a duplicate (AGENTS.md Security Boundaries step 8). A fingerprint
    is never executed twice — a repeat of a command that timed out or
    errored is still blocked (no infinite retry).
    """


class PlanExhaustedError(ExecutorError):
    """Every plan step has a failed attempt, so the plan cannot advance.

    Raised instead of retrying any step's fingerprint: a plan whose
    every step failed must be re-planned (the evaluator, PR21) rather
    than looped (AGENTS.md Forbidden Shortcuts).
    """


def _validate_fingerprint(value: str) -> str:
    """Validate a fingerprint as a 64-char sha256 hex digest.

    Raises:
        ValueError: If ``value`` is not a 64-char sha256 hex digest.
    """
    if not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError("fingerprint must be a 64-char sha256 hex digest")
    return value


class ModelAction(BaseModel):
    """The strict output contract for one model action proposal.

    The model proposes exactly two fields: the bounded action text (a
    command line or a ``halctl`` invocation — never a raw MCP call)
    and the skill it selected from the advertised summaries. Anything
    else — missing fields, extra fields, wrong types, over-long text —
    is malformed output and fails loudly (AGENTS.md rule #9).
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=MAX_ACTION_LENGTH)
    skill_id: str = Field(min_length=1, max_length=64)


class FailedAction(BaseModel):
    """One previously attempted action that failed.

    Fed back into the loop so a plan step with a failed attempt is
    skipped and a failed fingerprint is never retried (AGENTS.md
    Forbidden Shortcuts: no suppressing timeouts, no retrying
    forever).

    Attributes:
        fingerprint: sha256 fingerprint of the failed action's command.
        reason: Why it failed (e.g. ``timeout``, ``output_limit``,
            ``error``).
        plan_step_id: The plan step the failed action belonged to,
            when the action served a plan step.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    reason: str = Field(min_length=1)
    plan_step_id: str | None = None

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint_must_be_sha256(cls, value: str) -> str:
        return _validate_fingerprint(value)


class BudgetAccounting(BaseModel):
    """Snapshot of the budget state carried on every turn.

    Mirrors the tracker's cumulative dimensions: tokens, model calls,
    and tool calls used so far plus the remaining allowance (``None``
    when the dimension is unbounded).
    """

    model_config = ConfigDict(extra="forbid")

    tokens_used: int = Field(ge=0)
    model_calls_used: int = Field(ge=0)
    tool_calls_used: int = Field(ge=0)
    remaining_tokens: int | None = Field(default=None, ge=0)
    remaining_model_calls: int | None = Field(default=None, ge=0)
    remaining_tool_calls: int | None = Field(default=None, ge=0)


class ActionRequest(BaseModel):
    """One bounded action for the tool plane — never a list.

    The executor's strict output contract (AGENTS.md rule #4): every
    action carries a timeout, an output limit, and a fingerprint, plus
    the plan binding (``plan_id`` / ``plan_step_id`` / ``hypothesis_id``)
    when the turn served a plan step.

    Attributes:
        action: The bounded action text — a command line or a
            ``halctl`` invocation, never a raw MCP call.
        skill_id: The skill that bounds and guides the action.
        timeout_seconds: The skill's default action timeout.
        output_limit: Per-stream output cap in characters.
        fingerprint: sha256 fingerprint of the action's canonical form,
            from the policy gate.
        phase: The routed phase the action serves.
        plan_id: The plan the action serves, when one was produced.
        plan_step_id: The plan step the action implements, when planned.
        hypothesis_id: The hypothesis the step tests, when planned.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=MAX_ACTION_LENGTH)
    skill_id: str = Field(min_length=1, max_length=64)
    timeout_seconds: int = Field(ge=1)
    output_limit: int = Field(ge=1)
    fingerprint: str
    phase: Phase
    plan_id: str | None = None
    plan_step_id: str | None = None
    hypothesis_id: str | None = None

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint_must_be_sha256(cls, value: str) -> str:
        return _validate_fingerprint(value)


class ExecutorTurn(BaseModel):
    """The executor's typed return: exactly one bounded action per turn.

    Attributes:
        phase: The routed phase this turn served.
        predicate: The transition predicate that matched the graph state.
        action: The single bounded action for the tool plane.
        budget: Budget accounting after this turn's consumption.
    """

    model_config = ConfigDict(extra="forbid")

    phase: Phase
    predicate: str = Field(min_length=1)
    action: ActionRequest
    budget: BudgetAccounting


class Executor:
    """Bounded one-action-per-turn executor loop.

    Args:
        budgets: The budget tracker every turn checks and consumes
            against (one model call + one tool call per approved turn).
        run_id: Run identifier recorded on every event.
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``executor.*`` run events; when ``None`` no
            events are emitted.
        registry: Skill registry used to resolve the model's selected
            skill and its default timeout; defaults to a fresh
            :class:`~ozzgraph.skills.SkillRegistry` snapshot of the
            module-level :data:`ozzgraph.skills.SKILLS`.
        router: Graph-driven phase router; defaults to a
            :class:`PhaseRouter` built over ``registry``.
        planner: Deterministic planner; defaults to a :class:`Planner`
            built over ``registry``.
        policy: Scope policy gate (AGENTS.md Security Boundaries steps
            3-7); defaults to a fail-closed :class:`ScopePolicy`.
        store: Fingerprint store rejecting duplicate actions (step 8);
            defaults to an in-memory :class:`FingerprintStore`.
        evaluator: Optional PR21 evaluator the loop can consult between
            turns via :meth:`consult_evaluator`; the turn loop itself
            never calls it, so existing executor behavior is unchanged.
    """

    def __init__(
        self,
        *,
        budgets: Budgets,
        run_id: str,
        event_log: EventLog | None = None,
        registry: SkillRegistry | None = None,
        router: PhaseRouter | None = None,
        planner: Planner | None = None,
        policy: ScopePolicy | None = None,
        store: FingerprintStore | None = None,
        evaluator: Evaluator | None = None,
    ) -> None:
        self._budgets = budgets
        self._run_id = run_id
        self._event_log = event_log
        self._registry = registry if registry is not None else SkillRegistry()
        self._router = router if router is not None else PhaseRouter(self._registry)
        self._planner = planner if planner is not None else Planner(self._registry)
        self._policy = policy if policy is not None else ScopePolicy()
        self._store = store if store is not None else FingerprintStore()
        self._evaluator = evaluator

    async def turn(
        self,
        graph: StateGraph,
        model_output: object,
        *,
        failed_actions: Sequence[FailedAction] = (),
        count_model_call: bool = True,
    ) -> ExecutorTurn:
        """Produce exactly one bounded action for one turn.

        Flow: budget check -> model-call consumption -> route -> plan
        -> step selection -> model output validation -> skill
        validation -> policy gate (fingerprint + duplicate rejection)
        -> tool-call consumption -> plan persistence -> attempt
        recording -> typed turn.

        Args:
            graph: The authoritative SQLite state graph to route and
                plan on.
            model_output: The model's raw, untrusted output: a JSON
                object string or a mapping with ``action`` and
                ``skill_id`` fields (the strict output contract).
            failed_actions: History of previously attempted actions
                that failed; their fingerprints are never retried, and
                plan steps with a failed attempt are skipped.

        Raises:
            BudgetExceeded: If any bounded budget dimension is
                exhausted before the turn starts.
            MalformedOutputError: If the model output violates the
                strict action contract.
            InvalidSkillError: If the model selected a skill that is
                unknown, does not cover the routed phase, or is not
                the plan step's assigned skill.
            ScopeViolationError: If the proposed action text fails the
                policy gate (allowlist, platform/public-internet
                blocks, family or phase permissions).
            DuplicateFingerprintError: If the action's fingerprint was
                already attempted (in ``failed_actions``) or already
                recorded (the duplicate store).
            PlanExhaustedError: If a plan exists and every one of its
                steps has a failed attempt.
            SkillRegistryError: If the resolved skill is not registered
                in the executor's registry (a wiring error).
        """
        self._check_budget_exhausted()
        if count_model_call:
            self._budgets.consume_model_call()

        route = await self._router.route(graph)
        decision = await self._planner.plan(graph, route)
        plan: Plan | None = None
        step: PlanStep | None = None
        if isinstance(decision, Plan):
            plan = decision
            step = self._select_step(plan, failed_actions)

        proposed = self._validate_output(model_output)
        skill_id = self._resolve_skill(route, step, proposed.skill_id)

        approved = self._policy.check(proposed.action, phase=route.phase.value)
        self._reject_retry(approved, failed_actions)
        timeout = self._registry.timeout_for(skill_id)

        self._budgets.consume_tool_call()
        if plan is not None:
            await self._persist_plan(graph, plan)
        request = ActionRequest(
            action=proposed.action,
            skill_id=skill_id,
            timeout_seconds=timeout,
            output_limit=DEFAULT_OUTPUT_LIMIT,
            fingerprint=approved.fingerprint,
            phase=route.phase,
            plan_id=plan.id if plan is not None else None,
            plan_step_id=step.id if step is not None else None,
            hypothesis_id=step.hypothesis_id if step is not None else None,
        )
        await self._record_attempt(graph, request)
        return ExecutorTurn(
            phase=route.phase,
            predicate=route.predicate,
            action=request,
            budget=self._accounting(),
        )

    async def consult_evaluator(self, graph: StateGraph) -> PlanEvaluation | None:
        """Consult the configured evaluator for a plan-level decision.

        The minimal PR21 integration surface: the turn loop above never
        calls the evaluator (its behavior is unchanged); a supervisor
        calls this between turns to learn the evaluator's typed verdict
        (continue/complete/abandon/replan), which the evaluator persists
        as graph entities and ``graph.*`` events.

        Args:
            graph: The authoritative SQLite state graph to evaluate on.

        Returns:
            The evaluator's typed :class:`PlanEvaluation`, or ``None``
            when no evaluator is configured.

        Raises:
            ozzgraph.evaluator.NoPlanError: If the graph holds no plan
                entity.
            ozzgraph.evaluator.InvalidPlanStateError: If the persisted
                plan entities are invalid.
            ozzgraph.evaluator.MalformedEvaluatorOutputError: If a model
                fallback completion violates the verdict contract.
        """
        if self._evaluator is None:
            return None
        return await self._evaluator.decide_plan(graph)

    def _check_budget_exhausted(self) -> None:
        """Raise :class:`BudgetExceeded` for the first exhausted dimension.

        Checks runtime, then tokens, model calls, and tool calls,
        mirroring :meth:`Budgets.is_exhausted`. The check runs before
        anything else in a turn, so an exhausted run fails loudly
        instead of producing another action.

        Raises:
            BudgetExceeded: If any bounded budget dimension is
                exhausted.
        """
        budgets = self._budgets
        if budgets.is_runtime_exhausted():
            # Budgets does not expose the raw runtime cap through its
            # public API, so the error reports the elapsed time at the
            # exhaustion point; the kind is what callers key on.
            elapsed = budgets.elapsed()
            raise BudgetExceeded(BudgetKind.RUNTIME, elapsed, elapsed)
        remaining = budgets.remaining_tokens()
        if remaining is not None and remaining <= 0:
            used = budgets.tokens_used()
            raise BudgetExceeded(BudgetKind.TOKENS, used, used + 1)
        remaining = budgets.remaining_model_calls()
        if remaining is not None and remaining <= 0:
            used = budgets.model_calls_used()
            raise BudgetExceeded(BudgetKind.MODEL_CALLS, used, used + 1)
        remaining = budgets.remaining_tool_calls()
        if remaining is not None and remaining <= 0:
            used = budgets.tool_calls_used()
            raise BudgetExceeded(BudgetKind.TOOL_CALLS, used, used + 1)

    def _validate_output(self, raw: object) -> ModelAction:
        """Parse and validate the model's untrusted output.

        Accepts a JSON object string or a mapping and validates it
        against the strict :class:`ModelAction` contract. Anything
        else — invalid JSON, a non-object document, a non-str/non-map
        value, or a contract violation — is rejected loudly.

        Raises:
            MalformedOutputError: If the output cannot be parsed or
                fails the contract.
        """
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MalformedOutputError(f"model output is not valid JSON: {exc}") from exc
        elif isinstance(raw, Mapping):
            parsed = dict(raw)
        else:
            raise MalformedOutputError(
                f"model output must be a JSON object string or a mapping, got {type(raw).__name__}"
            )
        try:
            return ModelAction.model_validate(parsed)
        except ValidationError as exc:
            raise MalformedOutputError(f"model output violates the action contract: {exc}") from exc

    def _resolve_skill(self, route: PhaseRoute, step: PlanStep | None, proposed: str) -> str:
        """Resolve the model's proposed skill against the turn context.

        With a bound plan step, the step's assigned skill is
        authoritative and the proposal must equal it. Without a plan,
        the proposal must be one of the routed phase's advertised
        skill summaries.

        Raises:
            InvalidSkillError: If the proposal is not valid for the
                turn context.
        """
        if step is not None:
            if proposed != step.skill_id:
                raise InvalidSkillError(
                    f"model selected skill {proposed!r} but plan step {step.id!r} "
                    f"requires its assigned skill {step.skill_id!r}"
                )
            return step.skill_id
        advertised = {summary.skill_id for summary in route.skills}
        if proposed not in advertised:
            raise InvalidSkillError(
                f"skill {proposed!r} does not cover phase {route.phase.value}; "
                f"advertised: {sorted(advertised)}"
            )
        return proposed

    def _reject_retry(
        self, decision: PolicyDecision, failed_actions: Sequence[FailedAction]
    ) -> None:
        """Reject a fingerprint that was failed or already recorded.

        Raises:
            DuplicateFingerprintError: If ``decision.fingerprint``
                matches a failed action or is already in the store.
        """
        for failed in failed_actions:
            if failed.fingerprint == decision.fingerprint:
                raise DuplicateFingerprintError(
                    f"fingerprint {decision.fingerprint} was already attempted and failed "
                    f"({failed.reason}); refusing to retry the same action"
                )
        try:
            self._store.record(decision.fingerprint, canonical=decision.canonical)
        except DuplicateActionError as exc:
            raise DuplicateFingerprintError(str(exc)) from exc

    def _select_step(self, plan: Plan, failed_actions: Sequence[FailedAction]) -> PlanStep:
        """The next plan step with no failed attempt.

        Steps whose id appears in the failed-action history are
        skipped; the first remaining step is the turn's binding.

        Raises:
            PlanExhaustedError: If every plan step has a failed
                attempt.
        """
        failed_step_ids = frozenset(
            failed.plan_step_id for failed in failed_actions if failed.plan_step_id is not None
        )
        for step in plan.steps:
            if step.id not in failed_step_ids:
                return step
        raise PlanExhaustedError(
            f"every step of plan {plan.id!r} has a failed attempt; refusing to "
            "retry a plan that cannot make progress"
        )

    async def _persist_plan(self, graph: StateGraph, plan: Plan) -> None:
        """Persist ``plan`` as graph entities, idempotently.

        The first time a plan id is seen, one ``plan`` entity, one
        ``plan_step`` entity per step, and a ``PLANSTEP TESTS
        HYPOTHESIS`` edge per hypothesis-testing step are created, and
        every mutation is mirrored to the event log as a ``graph.*``
        event with the same timestamp, so replay reconstructs the
        identical graph hash. A plan already persisted (the same graph
        state yields the same plan id) is left untouched.
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
        self._append(
            EXECUTOR_PLAN_PERSISTED,
            {"plan_id": plan.id, "phase": plan.phase.value, "step_count": len(plan.steps)},
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

    async def _record_attempt(self, graph: StateGraph, request: ActionRequest) -> None:
        """Record the approved action before it executes (step 10).

        Persists an ``action`` entity keyed by the action's fingerprint
        (``action-<fingerprint>`` — the entity future observations
        reference via ``ACTION PRODUCED OBSERVATION``) and appends the
        ``executor.action_attempted`` run event with the full bounded
        action payload.
        """
        payload: dict[str, object] = {
            "command": request.action,
            "skill_id": request.skill_id,
            "timeout_seconds": request.timeout_seconds,
            "output_limit": request.output_limit,
            "fingerprint": request.fingerprint,
            "phase": request.phase.value,
            "plan_id": request.plan_id,
            "plan_step_id": request.plan_step_id,
            "hypothesis_id": request.hypothesis_id,
        }
        await self._create_entity(graph, f"action-{request.fingerprint}", ENTITY_ACTION, payload)
        self._append(EXECUTOR_ACTION_ATTEMPTED, payload)

    async def _create_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        entity_type: str,
        data: dict[str, object],
    ) -> None:
        """Create one entity and mirror the mutation to the event log.

        The entity and its ``graph.entity_created`` event share one
        timestamp, so replaying the log reproduces ``created_at``
        exactly and the graph hash is stable.
        """
        at = datetime.now(UTC)
        await graph.create_entity(entity_id, entity_type, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    self._run_id,
                    EXECUTOR_PRODUCER,
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
                    EXECUTOR_PRODUCER,
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
        """Append one executor run event when an event log is configured."""
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=EXECUTOR_PRODUCER,
                    payload=payload,
                )
            )

    def _accounting(self) -> BudgetAccounting:
        """Snapshot the budget state after this turn's consumption."""
        budgets = self._budgets
        return BudgetAccounting(
            tokens_used=budgets.tokens_used(),
            model_calls_used=budgets.model_calls_used(),
            tool_calls_used=budgets.tool_calls_used(),
            remaining_tokens=budgets.remaining_tokens(),
            remaining_model_calls=budgets.remaining_model_calls(),
            remaining_tool_calls=budgets.remaining_tool_calls(),
        )
