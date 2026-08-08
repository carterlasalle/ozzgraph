"""Tests for the deterministic paid-hint policy gate (PR23).

Covers the docs/TECHNICAL_REQUIREMENTS.md "Hint Policy" contract:
hint zero is never gated, paid hints are supervisor-only (a
non-privileged client is refused before the gate), the paid-hint
budget is enforced against the persisted purchase count (never
exceeding ``max_hints``), every gate rule denies its own
deterministic condition (recent information gain, untried low-cost
actions, fewer than two evaluator recommendations, insufficient
expected-value improvement), the gate is fail-closed on
unrepresentable state, an approved purchase persists the entity and
records the full event sequence (policy_approved -> purchase_attempted
-> purchase_succeeded), the gate and purchases are replay-consistent,
and concurrent gate evaluations never double-purchase.

Every test uses its own in-memory SQLite graph (``":memory:"``);
replay tests use a file-backed live graph plus a fresh replay
database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ozzgraph.environments.halctf import (
    ENTITY_HINT_PURCHASE,
    ENTITY_HINT_RECOMMENDATION,
    RULE_FREE_HINT,
    HintCoordinator,
    HintPolicy,
    HintPolicyDeniedError,
    HintPrivilegeError,
    HintStateError,
    PaidHintRequest,
    hint_recommendation_id,
)
from ozzgraph.evaluator import ENTITY_EVALUATION
from ozzgraph.events import (
    GRAPH_ENTITY_CREATED,
    HINT_POLICY_APPROVED,
    HINT_POLICY_DENIED,
    HINT_PURCHASE_ATTEMPTED,
    HINT_PURCHASE_FAILED,
    HINT_PURCHASE_SUCCEEDED,
    HINT_RECOMMENDATION_RECORDED,
    EventLog,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.executor import ENTITY_ACTION, ENTITY_PLAN, ENTITY_PLAN_STEP
from ozzgraph.hal_client import HalServiceError, HintResult
from ozzgraph.replay import replay_graph
from ozzgraph.state_graph import StateGraph

CHALLENGE = "ch-1"
RUN = "run-1"
PLAN = "plan-exp-abc"

PAID = HintResult(challenge_id=CHALLENGE, index=1, hint="try sqlmap --level 2", paid=True)

# Deterministic timestamps: observations/plan/actions predate the
# evaluations, so the "no recent information gain" anchor (the latest
# evaluation) is never violated on the happy path.
OLD_AT = datetime(2026, 1, 1, tzinfo=UTC)
EVAL_AT = datetime(2026, 1, 2, tzinfo=UTC)
NEW_AT = datetime(2026, 1, 3, tzinfo=UTC)


class FakeHintClient:
    """A scripted privileged hint surface (records every call)."""

    def __init__(
        self,
        *,
        privileged: bool = True,
        result: HintResult | None = None,
        failure: HalServiceError | None = None,
        delay: float = 0.0,
    ) -> None:
        self._privileged = privileged
        self.result = result
        self.failure = failure
        self.delay = delay
        self.calls: list[tuple[str, int]] = []

    @property
    def privileged(self) -> bool:
        return self._privileged

    async def request_hint(self, challenge_id: str, index: int) -> HintResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((challenge_id, index))
        if self.failure is not None:
            raise self.failure
        if self.result is None:
            # Scripted default: echo the request like the platform does.
            return HintResult(
                challenge_id=challenge_id, index=index, hint="try sqlmap --level 2", paid=True
            )
        return self.result

    async def aclose(self) -> None:
        """No-op: the fake owns no connection (protocol conformance)."""


def _coordinator(
    client: FakeHintClient,
    *,
    event_log: EventLog | None = None,
    max_hints: int = 1,
    policy: HintPolicy | None = None,
) -> HintCoordinator:
    return HintCoordinator(
        client=client,
        run_id=RUN,
        challenge_id=CHALLENGE,
        event_log=event_log,
        max_hints=max_hints,
        policy=policy,
    )


async def _create(
    graph: StateGraph,
    entity_id: str,
    entity_type: str,
    data: dict[str, object],
    *,
    at: datetime,
    event_log: EventLog | None,
) -> None:
    """Create one seeded entity, mirroring the mutation when a log is set."""
    await graph.create_entity(entity_id, entity_type, data, at=at)
    if event_log is not None:
        event_log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "test",
                GraphEntityCreated(entity_id=entity_id, entity_type=entity_type, data=data, at=at),
            )
        )


async def _seed_plan(
    graph: StateGraph,
    *,
    plan_id: str = PLAN,
    step_count: int = 2,
    step_count_payload: object | None = None,
    at: datetime = OLD_AT,
    event_log: EventLog | None = None,
) -> None:
    """Seed a plan entity plus its step entities."""
    await _create(
        graph,
        plan_id,
        ENTITY_PLAN,
        {
            "phase": "EXPLOITATION",
            "step_count": step_count if step_count_payload is None else step_count_payload,
            "hypotheses": [],
            "completion_conditions": [],
            "abandonment_conditions": [],
        },
        at=at,
        event_log=event_log,
    )
    for n in range(1, step_count + 1):
        await _create(
            graph,
            f"{plan_id}-step-{n}",
            ENTITY_PLAN_STEP,
            {
                "hypothesis_id": None,
                "objective": f"objective {n}",
                "skill_id": "exploit_parameter_injection",
                "completion_condition": f"step {n} done",
                "abandon_condition": {"condition": "budget", "scope": None},
            },
            at=at,
            event_log=event_log,
        )


async def _seed_actions(
    graph: StateGraph,
    *,
    plan_id: str = PLAN,
    step_count: int = 2,
    attempts_per_step: int = 3,
    steps: tuple[int, ...] | None = None,
    at: datetime = OLD_AT,
    event_log: EventLog | None = None,
) -> None:
    """Seed attempted ``action`` entities bound to the plan's steps."""
    for n in steps if steps is not None else tuple(range(1, step_count + 1)):
        step_id = f"{plan_id}-step-{n}"
        for i in range(attempts_per_step):
            fingerprint = hashlib.sha256(f"{step_id}-{i}".encode()).hexdigest()
            await _create(
                graph,
                f"action-{fingerprint}",
                ENTITY_ACTION,
                {
                    "command": f"probe {n} {i}",
                    "skill_id": "exploit_parameter_injection",
                    "phase": "EXPLOITATION",
                    "plan_id": plan_id,
                    "plan_step_id": step_id,
                },
                at=at,
                event_log=event_log,
            )


async def _seed_evaluations(
    graph: StateGraph,
    *,
    plan_id: str = PLAN,
    count: int = 2,
    step_count: int = 2,
    completed: int = 0,
    at: datetime = EVAL_AT,
    event_log: EventLog | None = None,
) -> None:
    """Seed persisted evaluation entities (the recommendation anchors)."""
    for seq in range(1, count + 1):
        outcomes = [
            {
                "step_id": f"{plan_id}-step-{n}",
                "outcome": "completed" if n <= completed else "pending",
            }
            for n in range(1, step_count + 1)
        ]
        await _create(
            graph,
            f"eval-{plan_id}-{seq}",
            ENTITY_EVALUATION,
            {
                "plan_id": plan_id,
                "verdict": "continue",
                "step_outcomes": outcomes,
                "hypothesis_outcomes": [],
                "reason": "no signal",
                "superseded_by": None,
            },
            at=at,
            event_log=event_log,
        )


async def _seed_observations(
    graph: StateGraph, *, at: datetime = OLD_AT, event_log: EventLog | None = None
) -> None:
    """Seed information-bearing entities that predate the evaluations."""
    await _create(
        graph, "obs-1", "observation", {"summary": "observed"}, at=at, event_log=event_log
    )
    await _create(graph, "ev-1", "evidence", {"note": "parsed"}, at=at, event_log=event_log)


async def _seed_hint_ready_graph(
    graph: StateGraph,
    *,
    recommendations: int = 2,
    info_gain: bool = False,
    attempts_per_step: int = 3,
    completed: int = 0,
    purchases: int = 0,
    max_hints: int = 1,
    event_log: EventLog | None = None,
) -> None:
    """Seed a graph where every paid-hint gate rule passes (or as directed).

    The happy-path default: observations/plan/actions at ``OLD_AT``,
    two evaluations at ``EVAL_AT`` (nothing completed -> progress 0),
    six plan-bound attempts (both steps attempted -> low-cost actions
    exhausted; ``gain = (1 - 0) * min(1, 6/6) = 1.0 >= 0.5``), and two
    distinct evaluator recommendations.
    """
    await _seed_observations(graph, at=OLD_AT, event_log=event_log)
    await _seed_plan(graph, at=OLD_AT, event_log=event_log)
    await _seed_actions(graph, attempts_per_step=attempts_per_step, at=OLD_AT, event_log=event_log)
    await _seed_evaluations(graph, completed=completed, at=EVAL_AT, event_log=event_log)
    if info_gain:
        await _create(
            graph,
            "obs-fresh",
            "observation",
            {"summary": "fresh"},
            at=NEW_AT,
            event_log=event_log,
        )
    if recommendations:
        policy = HintPolicy(max_hints=max_hints)
        for seq in range(1, recommendations + 1):
            await policy.record_evaluator_recommendation(
                graph, f"eval-{PLAN}-{seq}", reason=f"recommendation {seq}"
            )
    for seq in range(1, purchases + 1):
        await _create(
            graph,
            f"hint-purchase-{seq}",
            ENTITY_HINT_PURCHASE,
            {"challenge_id": CHALLENGE, "index": 1, "paid": True, "hint": "old"},
            at=OLD_AT,
            event_log=event_log,
        )


# ---------------------------------------------------------------------------
# free hint zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_never_blocks_free_hint_zero() -> None:
    """Hint zero is approved unconditionally, even on an empty graph."""
    async with StateGraph(":memory:") as graph:
        decision = await HintPolicy().evaluate(
            graph, PaidHintRequest(challenge_id=CHALLENGE, index=0)
        )
        assert decision.approved is True
        assert decision.rules == {RULE_FREE_HINT: True}
        assert decision.reasons == ()
        assert decision.expected_value_gain is None


@pytest.mark.asyncio
async def test_coordinator_free_hint_zero_reaches_wire_unprivileged() -> None:
    """Hint zero is free: no gate, no privilege, no purchase entity."""
    async with StateGraph(":memory:") as graph:
        client = FakeHintClient(privileged=False)
        result = await _coordinator(client).check_then_request(graph, 0)

        assert result.index == 0
        assert client.calls == [(CHALLENGE, 0)]
        purchases = await graph.list_entities(ENTITY_HINT_PURCHASE)
        assert purchases == []


@pytest.mark.asyncio
async def test_coordinator_rejects_negative_index() -> None:
    """A negative index is a caller mistake, refused loudly."""
    async with StateGraph(":memory:") as graph:
        with pytest.raises(ValueError, match="index"):
            await _coordinator(FakeHintClient()).check_then_request(graph, -1)


# ---------------------------------------------------------------------------
# supervisor-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_hint_requires_privileged_client() -> None:
    """Only a privileged client may buy a paid hint; the wire is untouched."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        client = FakeHintClient(privileged=False)

        with pytest.raises(HintPrivilegeError, match="supervisor-only"):
            await _coordinator(client).check_then_request(graph, 1)

        assert client.calls == []


# ---------------------------------------------------------------------------
# gate rule denials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_hint_denied_when_budget_exhausted() -> None:
    """A persisted purchase count at max_hints blocks the next purchase."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph, purchases=1)
        client = FakeHintClient()

        with pytest.raises(HintPolicyDeniedError) as exc_info:
            await _coordinator(client, max_hints=1).check_then_request(graph, 1)

        decision = exc_info.value.decision
        assert decision.approved is False
        assert decision.rules["budget_available"] is False
        assert any("budget exhausted" in reason for reason in decision.reasons)
        assert client.calls == []


@pytest.mark.asyncio
async def test_paid_hint_denied_on_recent_information_gain() -> None:
    """New fact/evidence/observation after the anchor denies the purchase."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph, info_gain=True)
        client = FakeHintClient()

        with pytest.raises(HintPolicyDeniedError) as exc_info:
            await _coordinator(client).check_then_request(graph, 1)

        decision = exc_info.value.decision
        assert decision.rules["no_recent_information_gain"] is False
        assert any("recent information gain" in reason for reason in decision.reasons)
        assert client.calls == []


@pytest.mark.asyncio
async def test_paid_hint_denied_when_low_cost_actions_not_exhausted() -> None:
    """An untried plan step means cheap candidates remain: denied."""
    async with StateGraph(":memory:") as graph:
        # Everything except actions (attempts_per_step=0), then only step 1
        # of the two-step plan gets attempts: step 2 stays untried.
        await _seed_hint_ready_graph(graph, attempts_per_step=0)
        await _seed_actions(graph, steps=(1,), attempts_per_step=3, at=NEW_AT)
        client = FakeHintClient()

        with pytest.raises(HintPolicyDeniedError) as exc_info:
            await _coordinator(client).check_then_request(graph, 1)

        decision = exc_info.value.decision
        assert decision.rules["low_cost_actions_exhausted"] is False
        assert any("untried plan step" in reason for reason in decision.reasons)
        assert client.calls == []


@pytest.mark.asyncio
async def test_paid_hint_denied_with_fewer_than_two_recommendations() -> None:
    """One evaluator recommendation is not enough."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph, recommendations=1)
        client = FakeHintClient()

        with pytest.raises(HintPolicyDeniedError) as exc_info:
            await _coordinator(client).check_then_request(graph, 1)

        decision = exc_info.value.decision
        assert decision.rules["two_evaluator_recommendations"] is False
        assert any(
            "evaluator recommendations 1 < required 2" in reason for reason in decision.reasons
        )
        assert client.calls == []


@pytest.mark.asyncio
async def test_paid_hint_denied_when_expected_value_insufficient() -> None:
    """Too little stall effort yields an expected-value gain below 0.5."""
    async with StateGraph(":memory:") as graph:
        # Both steps attempted once each (2 attempts, progress 0):
        # gain = (1 - 0) * min(1, 2/6) = 0.333 < 0.5.
        await _seed_hint_ready_graph(graph, attempts_per_step=1)
        client = FakeHintClient()

        with pytest.raises(HintPolicyDeniedError) as exc_info:
            await _coordinator(client).check_then_request(graph, 1)

        decision = exc_info.value.decision
        assert decision.rules["sufficient_expected_value"] is False
        assert any("expected-value improvement" in reason for reason in decision.reasons)
        assert decision.expected_value_gain is not None
        assert decision.expected_value_gain < 0.5
        assert client.calls == []


# ---------------------------------------------------------------------------
# fail-closed gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_on_empty_graph() -> None:
    """Unknown state (no plan, no evaluation, no recommendations) denies."""
    async with StateGraph(":memory:") as graph:
        decision = await HintPolicy().evaluate(
            graph, PaidHintRequest(challenge_id=CHALLENGE, index=1)
        )

        assert decision.approved is False
        assert decision.rules["budget_available"] is True  # nothing purchased yet
        assert decision.rules["no_recent_information_gain"] is False
        assert decision.rules["low_cost_actions_exhausted"] is False
        assert decision.rules["two_evaluator_recommendations"] is False
        assert decision.rules["sufficient_expected_value"] is False
        assert any("no plan entity" in reason for reason in decision.reasons)
        assert any("no hint purchase or evaluation entity" in reason for reason in decision.reasons)


@pytest.mark.asyncio
async def test_fail_closed_on_corrupt_step_count() -> None:
    """A non-integer step_count is unrepresentable state: denied."""
    async with StateGraph(":memory:") as graph:
        await _seed_plan(graph, step_count_payload="two")
        await _seed_evaluations(graph)
        policy = HintPolicy()
        await policy.record_evaluator_recommendation(graph, "eval-plan-exp-abc-1")
        await policy.record_evaluator_recommendation(graph, "eval-plan-exp-abc-2")
        client = FakeHintClient()

        with pytest.raises(HintPolicyDeniedError) as exc_info:
            await _coordinator(client).check_then_request(graph, 1)

        decision = exc_info.value.decision
        assert decision.approved is False
        assert decision.rules["low_cost_actions_exhausted"] is False
        assert decision.rules["sufficient_expected_value"] is False
        assert any("step_count" in reason for reason in decision.reasons)
        assert client.calls == []


# ---------------------------------------------------------------------------
# approval path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_hint_approved_when_all_conditions_hold() -> None:
    """All rules pass -> wire called once, purchase persisted, result returned."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        client = FakeHintClient()
        result = await _coordinator(client).check_then_request(graph, 1)

        assert result.paid is True
        assert result.hint == PAID.hint
        assert client.calls == [(CHALLENGE, 1)]

        purchases = await graph.list_entities(ENTITY_HINT_PURCHASE)
        assert len(purchases) == 1
        purchase = purchases[0]
        assert purchase.id == "hint-purchase-1"
        assert purchase.data["challenge_id"] == CHALLENGE
        assert purchase.data["index"] == 1
        assert purchase.data["paid"] is True
        assert purchase.data["hint"] == PAID.hint


@pytest.mark.asyncio
async def test_approved_purchase_records_event_sequence(tmp_path: Path) -> None:
    """policy_approved -> purchase_attempted -> purchase_succeeded, in order."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        await _coordinator(FakeHintClient(), event_log=log).check_then_request(graph, 1)

    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert (
        event_types.index(HINT_POLICY_APPROVED)
        < event_types.index(HINT_PURCHASE_ATTEMPTED)
        < event_types.index(HINT_PURCHASE_SUCCEEDED)
    )
    assert HINT_POLICY_DENIED not in event_types
    approved = next(event for event in events if event["event_type"] == HINT_POLICY_APPROVED)
    assert approved["producer"] == "hints"
    assert approved["payload"]["approved"] is True
    assert approved["payload"]["rules"]["sufficient_expected_value"] is True
    attempted = next(event for event in events if event["event_type"] == HINT_PURCHASE_ATTEMPTED)
    assert attempted["payload"]["index"] == 1
    assert attempted["payload"]["challenge_id"] == CHALLENGE
    succeeded = next(event for event in events if event["event_type"] == HINT_PURCHASE_SUCCEEDED)
    assert succeeded["payload"]["purchase_id"] == "hint-purchase-1"
    assert succeeded["payload"]["paid"] is True


@pytest.mark.asyncio
async def test_denial_records_policy_denied_event_with_reasons(tmp_path: Path) -> None:
    """A denial records hint.policy_denied carrying the rule breakdown."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph, purchases=1)
        with pytest.raises(HintPolicyDeniedError):
            await _coordinator(FakeHintClient(), event_log=log).check_then_request(graph, 1)

    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    denied = next(event for event in events if event["event_type"] == HINT_POLICY_DENIED)
    assert denied["producer"] == "hints"
    assert denied["payload"]["approved"] is False
    assert denied["payload"]["rules"]["budget_available"] is False
    assert denied["payload"]["reasons"]
    assert HINT_PURCHASE_ATTEMPTED not in [event["event_type"] for event in events]


@pytest.mark.asyncio
async def test_platform_free_result_for_paid_request_fails_loudly() -> None:
    """paid=false for a paid request raises and persists no purchase."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        client = FakeHintClient(
            result=HintResult(challenge_id=CHALLENGE, index=1, hint="x", paid=False)
        )

        with pytest.raises(HintStateError, match="paid=false"):
            await _coordinator(client).check_then_request(graph, 1)

        assert await graph.list_entities(ENTITY_HINT_PURCHASE) == []


@pytest.mark.asyncio
async def test_wire_failure_records_purchase_failed_and_reraises(tmp_path: Path) -> None:
    """A HalServiceError after approval records hint.purchase_failed."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        client = FakeHintClient(
            failure=HalServiceError(
                provider="halctf", status_code=500, retryable=True, message="boom"
            )
        )

        with pytest.raises(HalServiceError, match="boom"):
            await _coordinator(client, event_log=log).check_then_request(graph, 1)

        assert await graph.list_entities(ENTITY_HINT_PURCHASE) == []
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    failed = next(event for event in events if event["event_type"] == HINT_PURCHASE_FAILED)
    assert failed["payload"]["index"] == 1
    assert failed["payload"]["error"] == "boom"
    assert HINT_PURCHASE_SUCCEEDED not in [event["event_type"] for event in events]


# ---------------------------------------------------------------------------
# evaluator recommendations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_is_idempotent_per_evaluation() -> None:
    """The same evaluation can never recommend twice (one entity)."""
    async with StateGraph(":memory:") as graph:
        await _seed_evaluations(graph, count=1)
        policy = HintPolicy()
        first = await policy.record_evaluator_recommendation(graph, "eval-plan-exp-abc-1")
        second = await policy.record_evaluator_recommendation(graph, "eval-plan-exp-abc-1")

        assert first == second == hint_recommendation_id("eval-plan-exp-abc-1")
        recommendations = await graph.list_entities(ENTITY_HINT_RECOMMENDATION)
        assert len(recommendations) == 1
        assert recommendations[0].data["evaluation_id"] == "eval-plan-exp-abc-1"


@pytest.mark.asyncio
async def test_recommendation_rejects_unknown_evaluation() -> None:
    """A recommendation must reference a real evaluation entity (fail loudly)."""
    async with StateGraph(":memory:") as graph:
        policy = HintPolicy()
        with pytest.raises(HintStateError, match="evaluation"):
            await policy.record_evaluator_recommendation(graph, "eval-missing-1")


@pytest.mark.asyncio
async def test_recommendation_records_run_event(tmp_path: Path) -> None:
    """Recording a recommendation emits hint.recommendation_recorded."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(":memory:") as graph:
        await _seed_evaluations(graph, count=1)
        policy = HintPolicy(run_id=RUN, event_log=log)
        recommendation_id = await policy.record_evaluator_recommendation(
            graph, "eval-plan-exp-abc-1"
        )

    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    recorded = next(
        event for event in events if event["event_type"] == HINT_RECOMMENDATION_RECORDED
    )
    assert recorded["producer"] == "hints"
    assert recorded["payload"]["recommendation_id"] == recommendation_id
    assert recorded["payload"]["evaluation_id"] == "eval-plan-exp-abc-1"


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_gate_evaluations_never_double_purchase() -> None:
    """Two concurrent requests yield one purchase and one wire call."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        client = FakeHintClient(delay=0.05)
        coordinator = _coordinator(client)

        async def _attempt() -> HintResult | None:
            try:
                return await coordinator.check_then_request(graph, 1)
            except HintPolicyDeniedError:
                return None

        results = await asyncio.gather(_attempt(), _attempt())

        assert sum(1 for result in results if result is not None) == 1
        assert len(client.calls) == 1
        purchases = await graph.list_entities(ENTITY_HINT_PURCHASE)
        assert len(purchases) == 1  # the count never exceeds max_hints


@pytest.mark.asyncio
async def test_budget_allows_multiple_hints_when_configured() -> None:
    """max_hints > 1 permits sequential purchases, each gated and counted."""
    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        client = FakeHintClient()
        coordinator = _coordinator(client, max_hints=2)

        first = await coordinator.check_then_request(graph, 1)
        second = await coordinator.check_then_request(graph, 1)

        assert first.index == 1 and second.index == 1
        purchases = await graph.list_entities(ENTITY_HINT_PURCHASE)
        assert [purchase.id for purchase in purchases] == ["hint-purchase-1", "hint-purchase-2"]

        with pytest.raises(HintPolicyDeniedError, match="budget exhausted"):
            await coordinator.check_then_request(graph, 1)


# ---------------------------------------------------------------------------
# replay consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purchase_and_recommendations_replay_to_identical_hash(tmp_path: Path) -> None:
    """Replaying the event log reconstructs the identical graph hash."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(tmp_path / "live.db") as live:
        # Recommendations are recorded through an event-log-aware policy so
        # their graph mutations land in the log too.
        await _seed_hint_ready_graph(graph=live, recommendations=0, event_log=log)
        policy = HintPolicy(run_id=RUN, event_log=log)
        await policy.record_evaluator_recommendation(live, "eval-plan-exp-abc-1")
        await policy.record_evaluator_recommendation(live, "eval-plan-exp-abc-2")
        await _coordinator(FakeHintClient(), event_log=log).check_then_request(live, 1)
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


def test_invalid_max_hints_rejected() -> None:
    """max_hints must be >= 1 (budget-style, never silently unbounded)."""
    with pytest.raises(ValueError):
        HintPolicy(max_hints=0)
    with pytest.raises(ValueError):
        HintCoordinator(
            client=FakeHintClient(),
            run_id=RUN,
            challenge_id=CHALLENGE,
            max_hints=0,
        )
