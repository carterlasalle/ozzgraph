"""Deterministic paid-hint policy gate and supervisor-only coordinator (PR23).

Implements the paid-hint policy slice of Phase 8 (docs/
IMPLEMENTATION_PLAN.md, PR step 23; docs/TECHNICAL_REQUIREMENTS.md,
"Hint Policy"): hint zero is free and automatic (bootstrap owns it),
paid hints are supervisor-only, and a paid hint is purchased only when a
deterministic gate over the authoritative graph state passes. The gate
(:class:`HintPolicy`) never touches the wire; the coordinator
(:class:`HintCoordinator`) is the ONLY kernel caller of
``request_hint`` for ``index > 0`` (AGENTS.md invariant 5), mirroring
how :class:`~ozzgraph.submissions.SubmissionCoordinator` owns
``submit_flag``.

Design rules:

- Free hint zero is never gated (docs/TECHNICAL_REQUIREMENTS.md: hint
  zero may be automatic): :meth:`HintPolicy.evaluate` approves
  ``index == 0`` unconditionally (rules ``{"free_hint": true}``), and
  the coordinator passes it straight to the client — no privilege
  requirement, no budget consumption, no purchase entity (bootstrap
  records its own ``bootstrap.hint_requested`` event). Only ``index >
  0`` is a paid hint.

- Supervisor-only (AGENTS.md rule #5): the coordinator refuses to call
  the wire for a paid hint unless the injected client is privileged
  (:class:`HintPrivilegeError`), before the gate even runs. HalClient
  itself double-guards ``request_hint`` for ``index > 0``, so a
  non-privileged client can never reach the platform from any path.

- Budget invariant (AGENTS.md data invariant: "paid hint count never
  exceeds the configured maximum"): the paid-hint count is the number
  of persisted ``hint_purchase`` entities (``hint-purchase-<seq>``,
  deterministic — the sequence follows the existing entities, so
  identical graph states yield identical ids, mirroring
  ``submission-<seq>``). The gate denies when the count reaches
  ``max_hints`` (config default 1 — one paid hint per detonation), and
  the coordinator re-checks inside its serialization lock, so the
  count can never exceed the maximum.

- Fail-closed gate: every rule below is a pure, deterministic predicate
  over graph entities and their authoritative ``created_at``
  timestamps (replay reconstructs them exactly). Any unrepresentable or
  unknown state — no plan entity, a corrupt ``step_count`` payload, an
  inconsistent step set, no evaluation/purchase anchor — DENIES the
  paid hint with a documented reason. The gate never coerces state.

- Paid hint requires no recent information gain: the anchor is the
  later of the latest ``hint_purchase`` entity's ``created_at`` and the
  latest ``evaluation`` entity's ``created_at`` (the most recent moment
  the run either decided the state was worth paying for or assessed
  it). The rule passes iff NO ``fact``, ``evidence``, or ``observation``
  entity has ``created_at`` strictly after the anchor. With no purchase
  and no evaluation the rule is False (fail-closed: without an anchor
  there is no evidence the current state was ever assessed).

- Paid hint requires exhausted low-cost actions: the latest ``plan``
  entity is the one with the greatest ``(created_at, id)`` (the
  evaluator's PR21 selection rule; a plan id is never re-derived). Its
  ``plan_step`` entities are the modeled cheap action candidates
  (``<plan id>-step-<n>``). The rule passes iff every step has at least
  one attempted ``action`` entity bound via its ``plan_step_id``
  payload (each approved executor turn records exactly one action). A
  missing plan, a non-integer ``step_count``, or a step set whose size
  mismatches ``step_count`` is unrepresentable and denies.

- Paid hint requires two evaluator recommendations: the evaluator
  (PR21) persists ``evaluation`` entities but emits no
  hint-recommendation signal, so this layer owns the minimal
  deterministic recommendation record — a ``hint_recommendation``
  entity (``hint-rec-<sha256(evaluation_id)>``, idempotent per
  evaluation: the same evaluation can never recommend twice, so two
  records mean two DISTINCT evaluations). :meth:`HintPolicy.record_evaluator_recommendation`
  persists one; the gate requires at least :data:`REQUIRED_RECOMMENDATIONS`
  (2) of them.

- Paid hint requires sufficient expected-value improvement: the gain is
  the deterministic formula ``(1 - progress) * min(1, attempts /
  EV_STALL_FLOOR)`` where ``progress`` is the fraction of the latest
  plan's steps completed in the latest evaluation's ``step_outcomes``
  (0.0 when no evaluation exists or its payload is not a list — a
  missing or non-list payload contributes zero completed steps) and
  ``attempts`` is the number of ``action`` entities bound to the
  latest plan via their ``plan_id`` payload. The rule passes iff the
  gain is at least :data:`MIN_EV_GAIN` (0.5): the run is at most half
  progressed AND has stalled at least halfway to the exhaustion floor.

- Paid hints are always serialized (AGENTS.md rule #7): the coordinator
  owns an :class:`asyncio.Lock` around the entire
  check-then-request-then-persist sequence, so concurrent gate
  evaluations cannot double-purchase. The budget is re-read from the
  graph inside the lock, so the count can never exceed ``max_hints``.

- Replay compatibility (AGENTS.md data invariants): every entity the
  policy/coordinator persists shares one timestamp with its ``graph.*``
  event (the PR20 executor pattern), so replaying the log reconstructs
  the identical graph hash. Run events (producer ``hints``):
  ``hint.policy_denied`` (with the rule breakdown and reasons),
  ``hint.policy_approved``, ``hint.purchase_attempted`` BEFORE the wire
  call (the executor's "record the attempt before execution" boundary),
  then ``hint.purchase_succeeded`` or ``hint.purchase_failed``, plus
  ``hint.recommendation_recorded`` when a recommendation is persisted.

Payload contracts (docs/DATA_STRATEGY.md):

- ``hint_purchase`` entity (``hint-purchase-<seq>``): payload
  ``challenge_id``, ``index``, ``paid`` (strict ``True`` — the
  platform's verdict is validated, never assumed), ``hint`` (the
  platform's hint text). Purchases are entity-only (no edge): the count
  of entities IS the paid-hint count, so no challenge entity is
  required.
- ``hint_recommendation`` entity (``hint-rec-<sha256(evaluation_id)>``):
  payload ``evaluation_id`` (must name a real ``evaluation`` entity —
  fail loudly otherwise), ``reason``.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.config import DEFAULT_MAX_HINTS
from ozzgraph.evaluator import ENTITY_EVALUATION
from ozzgraph.events import (
    GRAPH_ENTITY_CREATED,
    HINT_POLICY_APPROVED,
    HINT_POLICY_DENIED,
    HINT_PURCHASE_ATTEMPTED,
    HINT_PURCHASE_FAILED,
    HINT_PURCHASE_SUCCEEDED,
    HINT_RECOMMENDATION_RECORDED,
    Event,
    EventLog,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.executor import ENTITY_ACTION, ENTITY_PLAN, ENTITY_PLAN_STEP
from ozzgraph.hal_client import HalServiceError, HintResult
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Producer name on every hint-policy and coordinator event.
HINTS_PRODUCER = "hints"

#: Entity types the policy writes and reads (docs/DATA_STRATEGY.md,
#: lowercase by convention). ``hint_purchase`` entities ARE the paid-hint
#: count; ``hint_recommendation`` entities are the evaluator
#: recommendation records.
ENTITY_HINT_PURCHASE = "hint_purchase"
ENTITY_HINT_RECOMMENDATION = "hint_recommendation"

#: Free hint zero is never gated (docs/TECHNICAL_REQUIREMENTS.md: hint
#: zero may be automatic); only positive indices are paid hints.
FREE_HINT_INDEX = 0

#: Rule names on :attr:`PaidHintDecision.rules` (documented in
#: docs/API_AND_INTEGRATIONS.md, "Hint Policy").
RULE_FREE_HINT = "free_hint"
RULE_BUDGET = "budget_available"
RULE_NO_RECENT_INFORMATION_GAIN = "no_recent_information_gain"
RULE_LOW_COST_EXHAUSTED = "low_cost_actions_exhausted"
RULE_TWO_RECOMMENDATIONS = "two_evaluator_recommendations"
RULE_SUFFICIENT_EV = "sufficient_expected_value"

#: Minimum expected-value gain that justifies a paid hint
#: (docs/TECHNICAL_REQUIREMENTS.md: sufficient expected-value
#: improvement). 0.5 reads as "the run is at most half progressed and
#: has stalled at least halfway to the exhaustion floor".
MIN_EV_GAIN = 0.5

#: Stall floor in the expected-value formula: the stall factor
#: ``min(1, attempts / EV_STALL_FLOOR)`` saturates once the latest plan
#: has this many attempted actions. Mirrors the plan budget scale
#: (the evaluator abandons a step after 3 attempts, a plan after 10
#: model calls).
EV_STALL_FLOOR = 6

#: Evaluator recommendations required before a paid hint is allowed
#: (docs/TECHNICAL_REQUIREMENTS.md: two evaluator recommendations).
REQUIRED_RECOMMENDATIONS = 2

#: Entity types whose creation after the anchor counts as "recent
#: information gain" (docs/TECHNICAL_REQUIREMENTS.md: no recent
#: information gain). Facts, evidence, and observations are the
#: information-bearing graph entities.
INFORMATION_GAIN_ENTITY_TYPES = ("fact", "evidence", "observation")


class HintError(RuntimeError):
    """Base error for the hint-policy layer (AGENTS.md rule #9)."""


class HintPrivilegeError(HintError):
    """The injected client is not privileged, so paid hints are refused.

    Only the supervisor may buy paid hints (AGENTS.md invariant 5,
    docs/TECHNICAL_REQUIREMENTS.md); a non-privileged client must never
    reach the wire. Hint zero stays open to any caller (free, not
    privileged), mirroring HalClient's own privilege model.
    """


class HintPolicyDeniedError(HintError):
    """The paid-hint gate denied the purchase.

    Raised by :meth:`HintCoordinator.check_then_request` after the
    ``hint.policy_denied`` event is recorded. The full decision (rule
    breakdown, reasons, expected-value gain) rides on the error so the
    caller (the supervisor) can decide the next move.

    Attributes:
        decision: The :class:`PaidHintDecision` that denied.
    """

    def __init__(self, decision: PaidHintDecision) -> None:
        super().__init__(f"paid hint denied: {'; '.join(decision.reasons)}")
        self.decision = decision


class HintStateError(HintError):
    """A payload field or platform verdict the coordinator reads is invalid.

    The coordinator never coerces corrupt state (fail loudly, AGENTS.md
    rule #9): a wrong-typed ``step_count`` denies through the gate, and
    a paid hint request that the platform answers with ``paid: false``
    raises this error — a purchase is only ever persisted for a genuine
    paid hint.
    """


class PaidHintRequest(BaseModel):
    """One paid-hint gate request.

    Attributes:
        challenge_id: The challenge the hint belongs to.
        index: The hint index. ``0`` (free hint) is approved
            unconditionally; only ``index > 0`` is gated as a paid hint.
    """

    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=1)
    index: int = Field(ge=0)


class PaidHintDecision(BaseModel):
    """The deterministic gate verdict for one paid-hint request.

    Attributes:
        approved: True when every gate rule passed (or the request was
            the free hint zero); False otherwise — the gate is
            fail-closed, so any unrepresentable state lands here.
        index: The requested hint index.
        rules: One boolean per gate rule, keyed by the ``RULE_*``
            constants — the auditable rule breakdown carried into
            ``hint.policy_denied`` / ``hint.policy_approved`` events.
        reasons: Human-readable denial reasons; empty when approved.
        expected_value_gain: The deterministic expected-value gain
            (``(1 - progress) * min(1, attempts / EV_STALL_FLOOR)``),
            or None when the free-hint shortcut applied.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    index: int = Field(ge=0)
    rules: dict[str, bool] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    expected_value_gain: float | None = None


class HintClient(Protocol):
    """The privileged hint surface the coordinator needs.

    :class:`~ozzgraph.hal_client.HalClient` satisfies this protocol
    structurally; tests inject lightweight fakes. The coordinator checks
    ``privileged`` before calling ``request_hint`` for a paid hint, so
    the supervisor-only boundary holds for every implementer.
    """

    @property
    def privileged(self) -> bool: ...

    async def request_hint(self, challenge_id: str, index: int) -> HintResult: ...

    async def aclose(self) -> None: ...


def hint_recommendation_id(evaluation_id: str) -> str:
    """The deterministic entity id for one evaluation's recommendation.

    ``hint-rec-<sha256(evaluation_id)>``: the same evaluation can never
    recommend twice (recording is idempotent), so :data:`REQUIRED_RECOMMENDATIONS`
    records always mean that many DISTINCT evaluations recommended a
    hint.
    """
    digest = hashlib.sha256(evaluation_id.encode("utf-8")).hexdigest()
    return f"hint-rec-{digest}"


class HintPolicy:
    """Deterministic paid-hint gate over the authoritative graph state.

    The policy is a pure decision function: :meth:`evaluate` reads the
    graph, evaluates every gate rule (fail-closed), and returns a typed
    :class:`PaidHintDecision` — it never mutates the graph, never emits
    events, and never touches the wire. :meth:`record_evaluator_recommendation`
    is the only writer: it persists the minimal recommendation record
    the "two evaluator recommendations" rule counts.

    Args:
        run_id: Run identifier recorded on every event (used by the
            recommendation recorder).
        event_log: Optional append-only log for the ``graph.*`` mutation
            event and the ``hint.recommendation_recorded`` run event;
            when ``None`` no events are emitted.
        max_hints: Paid-hint budget — the persisted ``hint_purchase``
            count must stay below it (default
            :data:`~ozzgraph.config.DEFAULT_MAX_HINTS`, one paid hint
            per detonation).
        min_ev_gain: Expected-value threshold (default
            :data:`MIN_EV_GAIN`).
        ev_stall_floor: Stall denominator in the expected-value formula
            (default :data:`EV_STALL_FLOOR`).
        required_recommendations: Distinct evaluator recommendations
            required (default :data:`REQUIRED_RECOMMENDATIONS`).
        information_gain_types: Entity types whose creation after the
            anchor counts as recent information gain (default
            :data:`INFORMATION_GAIN_ENTITY_TYPES`).

    Raises:
        ValueError: If a budget or threshold argument is invalid.
    """

    def __init__(
        self,
        *,
        run_id: str = "hints",
        event_log: EventLog | None = None,
        max_hints: int = DEFAULT_MAX_HINTS,
        min_ev_gain: float = MIN_EV_GAIN,
        ev_stall_floor: int = EV_STALL_FLOOR,
        required_recommendations: int = REQUIRED_RECOMMENDATIONS,
        information_gain_types: tuple[str, ...] = INFORMATION_GAIN_ENTITY_TYPES,
    ) -> None:
        if max_hints < 1:
            raise ValueError(f"max_hints must be >= 1, got {max_hints}")
        if min_ev_gain < 0:
            raise ValueError(f"min_ev_gain must be >= 0, got {min_ev_gain}")
        if ev_stall_floor < 1:
            raise ValueError(f"ev_stall_floor must be >= 1, got {ev_stall_floor}")
        if required_recommendations < 1:
            raise ValueError(
                f"required_recommendations must be >= 1, got {required_recommendations}"
            )
        if not information_gain_types:
            raise ValueError("information_gain_types must not be empty")
        self._run_id = run_id
        self._event_log = event_log
        self._max_hints = max_hints
        self._min_ev_gain = min_ev_gain
        self._ev_stall_floor = ev_stall_floor
        self._required_recommendations = required_recommendations
        self._information_gain_types = tuple(information_gain_types)

    async def evaluate(self, graph: StateGraph, request: PaidHintRequest) -> PaidHintDecision:
        """Evaluate the paid-hint gate for ``request`` without side effects.

        Free hint zero (``request.index == 0``) is approved
        unconditionally — hint zero may be automatic
        (docs/TECHNICAL_REQUIREMENTS.md) and is never gated. Every paid
        hint is evaluated against all five gate rules, each a pure,
        deterministic, fail-closed predicate over the graph; the
        decision carries the full rule breakdown and every denial
        reason.

        Args:
            graph: The authoritative SQLite state graph to evaluate on.
            request: The hint request to gate.

        Returns:
            The typed :class:`PaidHintDecision`. ``approved`` is True
            only when every rule passed; any unrepresentable state
            denies with a documented reason.
        """
        if request.index == FREE_HINT_INDEX:
            return PaidHintDecision(
                approved=True,
                index=request.index,
                rules={RULE_FREE_HINT: True},
                reasons=(),
            )

        budget_ok, budget_reason = await self._budget_available(graph)
        info_ok, info_reason = await self._no_recent_information_gain(graph)
        actions_ok, actions_reason = await self._low_cost_actions_exhausted(graph)
        recommendations_ok, recommendations_reason = await self._two_evaluator_recommendations(
            graph
        )
        ev_ok, ev_gain, ev_reason = await self._sufficient_expected_value(graph)

        rules = {
            RULE_BUDGET: budget_ok,
            RULE_NO_RECENT_INFORMATION_GAIN: info_ok,
            RULE_LOW_COST_EXHAUSTED: actions_ok,
            RULE_TWO_RECOMMENDATIONS: recommendations_ok,
            RULE_SUFFICIENT_EV: ev_ok,
        }
        reasons = tuple(
            reason
            for reason in (
                budget_reason,
                info_reason,
                actions_reason,
                recommendations_reason,
                ev_reason,
            )
            if reason is not None
        )
        return PaidHintDecision(
            approved=not reasons,
            index=request.index,
            rules=rules,
            reasons=reasons,
            expected_value_gain=ev_gain,
        )

    async def record_evaluator_recommendation(
        self,
        graph: StateGraph,
        evaluation_id: str,
        reason: str = "evaluator recommends a paid hint",
    ) -> str:
        """Persist one hint recommendation, idempotent per evaluation.

        The recommendation record is ``hint_rec-<sha256(evaluation_id)>``,
        so the same evaluation can never recommend twice — two records
        always mean two distinct evaluations, which is exactly what the
        "two evaluator recommendations" rule counts.

        Args:
            graph: The authoritative SQLite state graph to persist in.
            evaluation_id: The ``evaluation`` entity id that recommends
                the hint; must exist in the graph and be typed
                ``evaluation`` (fail loudly otherwise).
            reason: Why the evaluator recommends a hint.

        Raises:
            HintStateError: If ``evaluation_id`` does not name an
                existing ``evaluation`` entity.

        Returns:
            The recommendation entity id (the existing one when the
            evaluation already recommended).
        """
        evaluation = await graph.get_entity(evaluation_id)
        if evaluation is None or evaluation.type != ENTITY_EVALUATION:
            raise HintStateError(
                f"recommendation references unknown evaluation entity {evaluation_id!r}"
            )
        recommendation_id = hint_recommendation_id(evaluation_id)
        if await graph.get_entity(recommendation_id) is not None:
            return recommendation_id
        await self._create_entity(
            graph,
            recommendation_id,
            ENTITY_HINT_RECOMMENDATION,
            {"evaluation_id": evaluation_id, "reason": reason},
        )
        self._append(
            HINT_RECOMMENDATION_RECORDED,
            {
                "recommendation_id": recommendation_id,
                "evaluation_id": evaluation_id,
                "reason": reason,
            },
        )
        return recommendation_id

    # -- gate rules --------------------------------------------------------

    async def _budget_available(self, graph: StateGraph) -> tuple[bool, str | None]:
        """True when the persisted paid-hint count is below ``max_hints``.

        The count is the number of ``hint_purchase`` entities — the
        graph IS the budget ledger (AGENTS.md data invariant: the paid
        hint count never exceeds the configured maximum).
        """
        paid = await graph.list_entities(ENTITY_HINT_PURCHASE)
        if len(paid) >= self._max_hints:
            return (
                False,
                (
                    f"paid hint budget exhausted: {len(paid)} purchase(s) "
                    f">= max_hints {self._max_hints}"
                ),
            )
        return True, None

    async def _no_recent_information_gain(self, graph: StateGraph) -> tuple[bool, str | None]:
        """True when no fact/evidence/observation is newer than the anchor.

        The anchor is the later of the latest ``hint_purchase``
        entity's ``created_at`` and the latest ``evaluation`` entity's
        ``created_at`` — the most recent moment the run assessed the
        state or decided it was worth paying for. Any
        ``fact``/``evidence``/``observation`` entity created strictly
        after the anchor means the model received information it has
        not had a chance to act on, so a paid hint is premature.

        Fail-closed: with neither a purchase nor an evaluation there is
        no anchor, so the rule is False — without evidence the current
        state was assessed, the gate cannot claim "no recent
        information gain".
        """
        anchors: list[tuple[datetime, str]] = []
        purchases = await graph.list_entities(ENTITY_HINT_PURCHASE)
        if purchases:
            latest = max(purchases, key=lambda record: (record.created_at, record.id))
            anchors.append((latest.created_at, f"hint purchase {latest.id}"))
        evaluations = await graph.list_entities(ENTITY_EVALUATION)
        if evaluations:
            latest = max(evaluations, key=lambda record: (record.created_at, record.id))
            anchors.append((latest.created_at, f"evaluation {latest.id}"))
        if not anchors:
            return False, (
                "no hint purchase or evaluation entity; no anchor to establish "
                "no recent information gain"
            )
        anchor_at, anchor_label = max(anchors, key=lambda item: (item[0], item[1]))
        for entity_type in self._information_gain_types:
            for record in await graph.list_entities(entity_type):
                if record.created_at > anchor_at:
                    return False, (
                        f"recent information gain: {entity_type} {record.id!r} "
                        f"created after {anchor_label}"
                    )
        return True, None

    async def _low_cost_actions_exhausted(self, graph: StateGraph) -> tuple[bool, str | None]:
        """True when every step of the latest plan has been attempted.

        The latest plan is the greatest ``(created_at, id)`` among
        ``plan`` entities (the evaluator's PR21 selection rule). Its
        ``plan_step`` entities are the modeled cheap action candidates;
        an ``action`` entity bound to a step via its ``plan_step_id``
        payload counts as an attempt (each approved executor turn
        records exactly one action).

        Fail-closed: no plan, a non-integer/missing ``step_count``, or
        a step set whose size mismatches ``step_count`` is
        unrepresentable and denies.
        """
        plan = await self._latest_plan(graph)
        if plan is None:
            return False, "no plan entity; no low-cost action candidates are modeled"
        step_count = plan.data.get("step_count")
        if isinstance(step_count, bool) or not isinstance(step_count, int) or step_count < 1:
            return False, (
                f"plan {plan.id!r} payload field 'step_count' is missing or not a positive integer"
            )
        steps = await graph.list_entities(ENTITY_PLAN_STEP)
        plan_steps = [record for record in steps if record.id.startswith(f"{plan.id}-step-")]
        if len(plan_steps) != step_count:
            return False, (
                f"plan {plan.id!r} step entities ({len(plan_steps)}) inconsistent "
                f"with step_count ({step_count})"
            )
        attempted: set[str] = set()
        for action in await graph.list_entities(ENTITY_ACTION):
            step_id = action.data.get("plan_step_id")
            if isinstance(step_id, str):
                attempted.add(step_id)
        untried = [record.id for record in plan_steps if record.id not in attempted]
        if untried:
            return False, (
                f"low-cost actions not exhausted: untried plan step(s) {sorted(untried)}"
            )
        return True, None

    async def _two_evaluator_recommendations(self, graph: StateGraph) -> tuple[bool, str | None]:
        """True when at least :data:`REQUIRED_RECOMMENDATIONS` exist.

        Each ``hint_recommendation`` entity is idempotent per
        evaluation, so this counts distinct evaluations that
        recommended a hint (docs/TECHNICAL_REQUIREMENTS.md: two
        evaluator recommendations).
        """
        recommendations = await graph.list_entities(ENTITY_HINT_RECOMMENDATION)
        if len(recommendations) < self._required_recommendations:
            return False, (
                f"evaluator recommendations {len(recommendations)} < required "
                f"{self._required_recommendations}"
            )
        return True, None

    async def _sufficient_expected_value(
        self, graph: StateGraph
    ) -> tuple[bool, float | None, str | None]:
        """True when the deterministic expected-value gain clears the bar.

        Formula (documented in docs/API_AND_INTEGRATIONS.md, "Hint
        Policy")::

            gain = (1 - progress) * min(1, attempts / EV_STALL_FLOOR)

        where ``progress`` is the fraction of the latest plan's steps
        marked ``completed`` in the latest evaluation's
        ``step_outcomes`` (0.0 when no evaluation exists, or when its
        ``step_outcomes`` payload is missing or not a list — a missing
        or non-list payload contributes zero completed steps) and
        ``attempts`` is the count of ``action`` entities bound to the
        latest plan via their ``plan_id`` payload. The rule passes iff
        ``gain >= min_ev_gain``: the run is at most half progressed AND
        has stalled at least halfway to the exhaustion floor.

        Fail-closed: with no plan the gain is 0.0 — unrepresentable
        state denies.
        """
        plan = await self._latest_plan(graph)
        if plan is None:
            return False, 0.0, "no plan entity; expected-value improvement unknown"
        step_count = plan.data.get("step_count")
        if isinstance(step_count, bool) or not isinstance(step_count, int) or step_count < 1:
            return (
                False,
                0.0,
                (
                    f"plan {plan.id!r} payload field 'step_count' is missing or not a "
                    f"positive integer"
                ),
            )
        attempts = 0
        for action in await graph.list_entities(ENTITY_ACTION):
            if action.data.get("plan_id") == plan.id:
                attempts += 1
        progress = await self._plan_progress(graph, step_count)
        gain = (1.0 - progress) * min(1.0, attempts / float(self._ev_stall_floor))
        if gain >= self._min_ev_gain:
            return True, gain, None
        return (
            False,
            gain,
            (
                f"expected-value improvement {gain:.3f} below threshold "
                f"{self._min_ev_gain:g} (progress {progress:.3f}, plan attempts {attempts})"
            ),
        )

    async def _plan_progress(self, graph: StateGraph, step_count: int) -> float:
        """Completed fraction of the latest plan, per the latest evaluation.

        The latest evaluation is the greatest ``(created_at, id)``
        among ``evaluation`` entities; completed steps are its
        ``step_outcomes`` entries whose ``outcome`` is ``"completed"``.
        No evaluation, or a missing/non-list ``step_outcomes`` payload,
        contributes zero completed steps (deterministic and documented;
        the conservative reading only ever lowers the gain, which is
        fail-closed).
        """
        evaluations = await graph.list_entities(ENTITY_EVALUATION)
        if not evaluations:
            return 0.0
        latest = max(evaluations, key=lambda record: (record.created_at, record.id))
        outcomes = latest.data.get("step_outcomes")
        if not isinstance(outcomes, list):
            return 0.0
        completed = sum(
            1
            for outcome in outcomes
            if isinstance(outcome, dict) and outcome.get("outcome") == "completed"
        )
        return min(1.0, completed / float(step_count))

    @staticmethod
    async def _latest_plan(graph: StateGraph) -> EntityRecord | None:
        """The ``plan`` entity with the greatest ``(created_at, id)``.

        Mirrors the evaluator's latest-plan selection: ``created_at``
        is authoritative graph state and replay reconstructs it
        exactly, so the selection is deterministic.
        """
        plans = await graph.list_entities(ENTITY_PLAN)
        if not plans:
            return None
        return max(plans, key=lambda record: (record.created_at, record.id))

    # -- persistence helpers (PR20 pattern) --------------------------------

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
                    HINTS_PRODUCER,
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        data=data,
                        at=at,
                    ),
                )
            )

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one policy run event when an event log is configured."""
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=HINTS_PRODUCER,
                    payload=payload,
                )
            )


class HintCoordinator:
    """Supervisor-only paid-hint coordinator (the only ``request_hint`` caller).

    Args:
        client: The privileged HalCTF client used for ``hint.request``.
            Must be ``privileged`` for paid hints — anything else raises
            :class:`HintPrivilegeError` before the gate runs. Hint zero
            stays open to any caller (free, not privileged), mirroring
            HalClient's own privilege model.
        run_id: Run identifier recorded on every event.
        challenge_id: The challenge the hint is requested for.
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``hint.*`` run events; when ``None`` no events
            are emitted.
        max_hints: Paid-hint budget passed to the default
            :class:`HintPolicy` (default
            :data:`~ozzgraph.config.DEFAULT_MAX_HINTS`); ignored when
            ``policy`` is injected.
        policy: The gate to evaluate; defaults to a :class:`HintPolicy`
            built from ``max_hints`` and the module thresholds.

    Raises:
        ValueError: If ``max_hints`` is less than 1.
    """

    def __init__(
        self,
        *,
        client: HintClient,
        run_id: str,
        challenge_id: str,
        event_log: EventLog | None = None,
        max_hints: int = DEFAULT_MAX_HINTS,
        policy: HintPolicy | None = None,
    ) -> None:
        if max_hints < 1:
            raise ValueError(f"max_hints must be >= 1, got {max_hints}")
        self._client = client
        self._run_id = run_id
        self._challenge_id = challenge_id
        self._event_log = event_log
        self._policy = policy if policy is not None else HintPolicy(max_hints=max_hints)
        #: Paid hints are always serialized (AGENTS.md rule #7): the lock
        #: spans evaluate -> request -> persist, and the budget is
        #: re-read from the graph inside the lock, so concurrent gate
        #: evaluations can never double-purchase.
        self._lock = asyncio.Lock()

    async def check_then_request(self, graph: StateGraph, index: int) -> HintResult:
        """Evaluate the paid-hint gate, then request the hint when allowed.

        Flow: free hint zero passes straight to the client (never
        gated, never privileged, never counted — bootstrap owns it);
        any paid hint (``index > 0``) is serialized under the
        coordinator lock: refuse loudly if the client is not privileged
        -> evaluate the gate -> on denial record ``hint.policy_denied``
        (rule breakdown + reasons) and raise
        :class:`HintPolicyDeniedError` -> on approval record
        ``hint.policy_approved``, then ``hint.purchase_attempted``
        BEFORE the wire call (the executor's "record the attempt before
        execution" boundary) -> call ``client.request_hint`` -> persist
        the ``hint_purchase`` entity (same-timestamp ``graph.*``
        event) -> record ``hint.purchase_succeeded`` (or
        ``hint.purchase_failed`` on a wire failure, which re-raises).

        Args:
            graph: The authoritative SQLite state graph to gate on and
                persist the purchase in.
            index: The hint index; ``0`` is free, ``> 0`` is paid.

        Raises:
            ValueError: If ``index`` is negative.
            HintPrivilegeError: If the client is not privileged and
                ``index > 0``.
            HintPolicyDeniedError: If the gate denied the paid hint
                (the ``hint.policy_denied`` event carries the reasons).
            HintStateError: If the platform answered a paid request
                with ``paid: false`` (no purchase is persisted).
            HalServiceError: If the platform call fails after bounded
                retries (the purchase is not persisted; the caller
                decides whether to re-evaluate later).

        Returns:
            The platform's typed :class:`HintResult`.
        """
        if index < 0:
            raise ValueError(f"index must be >= 0, got {index}")
        if index == FREE_HINT_INDEX:
            # Hint zero is free and open to any caller: no gating, no
            # privilege requirement, no purchase entity. Bootstrap owns
            # the automatic request; this path exists so the coordinator
            # contract covers it without ever blocking it.
            return await self._client.request_hint(self._challenge_id, index)

        async with self._lock:
            if not self._client.privileged:
                raise HintPrivilegeError(
                    "paid hints are supervisor-only; the client must be constructed "
                    "with privileged=True (AGENTS.md invariant 5)"
                )
            decision = await self._policy.evaluate(
                graph, PaidHintRequest(challenge_id=self._challenge_id, index=index)
            )
            if not decision.approved:
                self._append(HINT_POLICY_DENIED, self._decision_payload(decision))
                raise HintPolicyDeniedError(decision)
            self._append(HINT_POLICY_APPROVED, self._decision_payload(decision))
            self._append(
                HINT_PURCHASE_ATTEMPTED,
                {"challenge_id": self._challenge_id, "index": index},
            )
            try:
                result = await self._client.request_hint(self._challenge_id, index)
            except HalServiceError as exc:
                self._append(
                    HINT_PURCHASE_FAILED,
                    {
                        "challenge_id": self._challenge_id,
                        "index": index,
                        "error": exc.message,
                    },
                )
                raise
            if result.paid is not True:
                self._append(
                    HINT_PURCHASE_FAILED,
                    {
                        "challenge_id": self._challenge_id,
                        "index": index,
                        "error": (
                            "platform answered a paid hint request with paid=false; "
                            "no purchase persisted"
                        ),
                    },
                )
                raise HintStateError(
                    f"platform answered paid hint index {index} with paid=false; "
                    f"a purchase is only persisted for a genuine paid hint"
                )
            purchase_id = await self._persist_purchase(graph, result)
            self._append(
                HINT_PURCHASE_SUCCEEDED,
                {
                    "purchase_id": purchase_id,
                    "challenge_id": result.challenge_id,
                    "index": result.index,
                    "paid": result.paid,
                    "hint": result.hint,
                },
            )
            return result

    async def _persist_purchase(self, graph: StateGraph, result: HintResult) -> str:
        """Persist one ``hint_purchase`` entity (``hint-purchase-<seq>``).

        The sequence follows the existing purchase entities, so
        identical graph states yield identical ids and replay
        reconstructs them (mirrors ``submission-<seq>``). Purchases are
        entity-only: the entity count IS the paid-hint count the gate's
        budget rule reads.
        """
        existing = await graph.list_entities(ENTITY_HINT_PURCHASE)
        purchase_id = f"hint-purchase-{len(existing) + 1}"
        payload: dict[str, object] = {
            "challenge_id": result.challenge_id,
            "index": result.index,
            "paid": result.paid,
            "hint": result.hint,
        }
        await self._create_entity(graph, purchase_id, ENTITY_HINT_PURCHASE, payload)
        return purchase_id

    def _decision_payload(self, decision: PaidHintDecision) -> dict[str, object]:
        """The event payload carrying the full auditable decision."""
        return {
            "index": decision.index,
            "approved": decision.approved,
            "rules": dict(decision.rules),
            "reasons": list(decision.reasons),
            "expected_value_gain": decision.expected_value_gain,
        }

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
                    HINTS_PRODUCER,
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        data=data,
                        at=at,
                    ),
                )
            )

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one coordinator run event when an event log is configured."""
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=HINTS_PRODUCER,
                    payload=payload,
                )
            )
