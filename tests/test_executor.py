"""Tests for the bounded executor loop and its schemas (PR20).

Covers the happy path (one bounded action per turn with a valid
schema), plan-step selection (the executor binds the next step with no
failed attempt), the typed no-plan decision, budget exhaustion
(:class:`~ozzgraph.budgets.BudgetExceeded`), malformed model output
(:class:`MalformedOutputError`), failed-action feedback (failed steps
are skipped, failed fingerprints never retried), duplicate-fingerprint
rejection, timeout/output-limit propagation from the skill, plan
persistence as graph entities with replayable events, the scope-policy
gate integration, and the typed error hierarchy (AGENTS.md rule #9).

Every test uses its own in-memory SQLite graph (``":memory:"``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ozzgraph.budgets import BudgetExceeded, BudgetKind, Budgets
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.executor import (
    DEFAULT_OUTPUT_LIMIT,
    EXECUTOR_ACTION_ATTEMPTED,
    EXECUTOR_PLAN_PERSISTED,
    MAX_ACTION_LENGTH,
    ActionRequest,
    BudgetAccounting,
    DuplicateFingerprintError,
    Executor,
    ExecutorError,
    ExecutorTurn,
    FailedAction,
    InvalidSkillError,
    MalformedOutputError,
    ModelAction,
    PlanExhaustedError,
)
from ozzgraph.phases import Phase
from ozzgraph.planner import NoPlanDecision, Plan, Planner
from ozzgraph.policy import AllowlistViolationError, ScopePolicy, fingerprint_command
from ozzgraph.replay import replay_graph
from ozzgraph.router import EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, PhaseRouter
from ozzgraph.state_graph import StateGraph


async def _entity(
    graph: StateGraph,
    entity_id: str,
    entity_type: str,
    data: dict[str, object] | None = None,
) -> None:
    """Create one entity."""
    await graph.create_entity(entity_id, entity_type, data)


async def _edge(
    graph: StateGraph,
    edge_id: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
) -> None:
    """Create one typed edge."""
    await graph.create_edge(edge_id, edge_type, src_id, dst_id)


async def _seed_baseline(graph: StateGraph) -> None:
    """Seed a run with a confirmed target and a characterized service.

    This is the "nothing pending" baseline: recon and enumeration are
    complete, no hypothesis, no access, no flags, no submissions.
    """
    await _entity(graph, "run-1", "run")
    await _entity(graph, "tgt-1", "target", {"confirmed": True})
    await _entity(graph, "svc-1", "service", {"characterized": True})


async def _seed_hypothesis(
    graph: StateGraph,
    hypothesis_id: str,
    supporting: tuple[str, ...] = (),
    *,
    data: dict[str, object] | None = None,
) -> None:
    """Seed one hypothesis with evidence entities and edges."""
    await _entity(graph, hypothesis_id, "hypothesis", data)
    for evidence_id in supporting:
        await _entity(graph, f"ev-{evidence_id}", "evidence")
        await _edge(
            graph,
            f"ev-{evidence_id}->{hypothesis_id}",
            EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
            f"ev-{evidence_id}",
            hypothesis_id,
        )


def _budgets(**overrides) -> Budgets:
    base = {
        "max_tokens": 100,
        "max_model_calls": 10,
        "max_tool_calls": 20,
        "max_workers": 2,
        "max_hints": 1,
        "max_runtime_s": 100.0,
    }
    base.update(overrides)
    return Budgets(**base)


def _executor(*, event_log: EventLog | None = None, **overrides) -> Executor:
    return Executor(
        budgets=overrides.pop("budgets", _budgets()),
        run_id="run-1",
        event_log=event_log,
        policy=overrides.pop("policy", ScopePolicy()),
        **overrides,
    )


async def _seed_recon(graph: StateGraph) -> None:
    """Seed the RECON state: a run with an unconfirmed target."""
    await _entity(graph, "run-1", "run")
    await _entity(graph, "tgt-1", "target", {"confirmed": False})


async def _seed_branching_exploitation(graph: StateGraph) -> None:
    """Seed a branching exploitation graph: two evidenced hypotheses."""
    await _seed_baseline(graph)
    await _seed_hypothesis(graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8})
    await _seed_hypothesis(graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.9})


# ---------------------------------------------------------------------------
# happy path: one bounded action per turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_one_bounded_action() -> None:
    """A valid proposal under a non-branching phase yields one bounded action."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        turn = await _executor().turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})
    assert isinstance(turn, ExecutorTurn)
    assert turn.phase == Phase.RECON
    assert turn.predicate == "targets_unconfirmed"
    assert turn.action.plan_id is None
    assert turn.action.plan_step_id is None
    assert turn.action.hypothesis_id is None
    assert turn.action.action == "echo probe"
    assert turn.action.skill_id == "recon_dns_enum"
    assert turn.action.fingerprint == fingerprint_command("echo probe")[1]
    assert turn.action.timeout_seconds == 60  # recon_dns_enum default timeout
    assert turn.action.output_limit == DEFAULT_OUTPUT_LIMIT
    assert turn.budget.tokens_used == 0
    assert turn.budget.model_calls_used == 1
    assert turn.budget.tool_calls_used == 1


@pytest.mark.asyncio
async def test_json_string_output_is_accepted() -> None:
    """The model output contract also accepts a JSON object string."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        turn = await _executor().turn(
            graph, '{"action": "echo probe", "skill_id": "recon_dns_enum"}'
        )
    assert turn.action.action == "echo probe"
    assert turn.action.skill_id == "recon_dns_enum"


@pytest.mark.asyncio
async def test_same_state_and_proposal_yield_identical_turn() -> None:
    """Deterministic: the same graph and proposal yield the identical turn."""

    async def run_once() -> ExecutorTurn:
        async with StateGraph(":memory:") as graph:
            await _seed_recon(graph)
            return await _executor().turn(
                graph, {"action": "echo probe", "skill_id": "recon_dns_enum"}
            )

    first = await run_once()
    second = await run_once()
    assert first == second


# ---------------------------------------------------------------------------
# plan binding: the executor selects the next plan step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_with_steps_selects_next_step() -> None:
    """A branching graph yields a plan; the turn binds the first step."""
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        turn = await _executor().turn(
            graph, {"action": "echo probe", "skill_id": plan.steps[0].skill_id}
        )
        assert turn.phase == Phase.EXPLOITATION
        assert turn.action.plan_id == plan.id
        assert turn.action.plan_step_id == plan.steps[0].id
        assert turn.action.hypothesis_id == plan.steps[0].hypothesis_id == "hyp-b"
        assert turn.action.skill_id == plan.steps[0].skill_id
        # the plan was persisted as graph entities
        plan_record = await graph.get_entity(plan.id)
        assert plan_record is not None and plan_record.type == "plan"
        assert plan_record.data["step_count"] == len(plan.steps)
        step_record = await graph.get_entity(plan.steps[0].id)
        assert step_record is not None and step_record.type == "plan_step"
        assert step_record.data["skill_id"] == plan.steps[0].skill_id


@pytest.mark.asyncio
async def test_plan_step_skill_mismatch_raises() -> None:
    """A bound step's assigned skill is authoritative; the model cannot dodge it."""
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        assert plan.steps[0].skill_id != "recon_dns_enum"
        with pytest.raises(InvalidSkillError, match="requires its assigned skill"):
            await _executor().turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})


# ---------------------------------------------------------------------------
# no-plan decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_plan_decision_yields_unbound_action() -> None:
    """A non-branching graph serves an action without plan binding."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(graph, "hyp-1", ("a1",), data={"exploitable": True})
        route = await PhaseRouter().route(graph)
        decision = await Planner().plan(graph, route)
        assert isinstance(decision, NoPlanDecision)
        turn = await _executor().turn(
            graph, {"action": "echo probe", "skill_id": "exploit_parameter_injection"}
        )
        assert turn.phase == Phase.EXPLOITATION
        assert turn.action.plan_id is None
        assert turn.action.plan_step_id is None
        assert turn.action.hypothesis_id is None


# ---------------------------------------------------------------------------
# budgets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_call_budget_exhaustion_raises() -> None:
    """An exhausted model-call budget raises BudgetExceeded before the turn."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        executor = _executor(budgets=_budgets(max_model_calls=1, max_tool_calls=10))
        first = await executor.turn(graph, {"action": "echo one", "skill_id": "recon_dns_enum"})
        assert first.budget.model_calls_used == 1
        with pytest.raises(BudgetExceeded) as exc:
            await executor.turn(graph, {"action": "echo two", "skill_id": "recon_dns_enum"})
        assert exc.value.kind == BudgetKind.MODEL_CALLS


@pytest.mark.asyncio
async def test_tool_call_budget_exhaustion_raises() -> None:
    """An exhausted tool-call budget raises BudgetExceeded before the turn."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        executor = _executor(budgets=_budgets(max_model_calls=10, max_tool_calls=1))
        await executor.turn(graph, {"action": "echo one", "skill_id": "recon_dns_enum"})
        with pytest.raises(BudgetExceeded) as exc:
            await executor.turn(graph, {"action": "echo two", "skill_id": "recon_dns_enum"})
        assert exc.value.kind == BudgetKind.TOOL_CALLS


# ---------------------------------------------------------------------------
# malformed model output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"action": "echo probe"},
        {"skill_id": "recon_dns_enum"},
        {"action": "", "skill_id": "recon_dns_enum"},
        {"action": "echo probe", "skill_id": ""},
        {"action": "echo probe", "skill_id": "recon_dns_enum", "extra": 1},
        {"action": 42, "skill_id": "recon_dns_enum"},
        {"action": "echo probe", "skill_id": 42},
        "not json",
        "[1, 2]",
        "null",
        42,
        None,
    ],
)
@pytest.mark.asyncio
async def test_malformed_output_raises(output: object) -> None:
    """Anything outside the strict ModelAction contract fails loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        with pytest.raises(MalformedOutputError):
            await _executor().turn(graph, output)


@pytest.mark.asyncio
async def test_overlong_action_text_raises() -> None:
    """Action text beyond MAX_ACTION_LENGTH is malformed output."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        with pytest.raises(MalformedOutputError):
            await _executor().turn(
                graph,
                {"action": "x" * (MAX_ACTION_LENGTH + 1), "skill_id": "recon_dns_enum"},
            )


# ---------------------------------------------------------------------------
# failed-action feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_plan_step_is_skipped() -> None:
    """A step with a failed attempt is skipped; the next step is selected."""
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        assert len(plan.steps) >= 2
        failed = FailedAction(
            fingerprint=fingerprint_command("echo stale")[1],
            reason="timeout",
            plan_step_id=plan.steps[0].id,
        )
        turn = await _executor().turn(
            graph,
            {"action": "echo fresh", "skill_id": plan.steps[1].skill_id},
            failed_actions=[failed],
        )
        assert turn.action.plan_step_id == plan.steps[1].id
        assert turn.action.hypothesis_id == plan.steps[1].hypothesis_id


@pytest.mark.asyncio
async def test_failed_fingerprint_is_never_retried() -> None:
    """A proposal whose fingerprint already failed is rejected loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        failed = FailedAction(
            fingerprint=fingerprint_command("echo probe")[1],
            reason="error",
        )
        with pytest.raises(DuplicateFingerprintError, match="refusing to retry"):
            await _executor().turn(
                graph,
                {"action": "echo probe", "skill_id": plan.steps[0].skill_id},
                failed_actions=[failed],
            )


@pytest.mark.asyncio
async def test_all_steps_failed_raises_plan_exhausted() -> None:
    """A plan whose every step failed raises PlanExhaustedError, never a retry."""
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        failed_actions = [
            FailedAction(
                fingerprint=fingerprint_command(f"echo stale-{index}")[1],
                reason="timeout",
                plan_step_id=step.id,
            )
            for index, step in enumerate(plan.steps)
        ]
        with pytest.raises(PlanExhaustedError, match="refusing"):
            await _executor().turn(
                graph,
                {"action": "echo probe", "skill_id": plan.steps[0].skill_id},
                failed_actions=failed_actions,
            )


# ---------------------------------------------------------------------------
# duplicate-fingerprint rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_fingerprint_is_rejected() -> None:
    """The same action twice in one run is rejected by the fingerprint store."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        executor = _executor()
        first = await executor.turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})
        assert first.action.fingerprint == fingerprint_command("echo probe")[1]
        with pytest.raises(DuplicateFingerprintError, match="already recorded"):
            await executor.turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})


# ---------------------------------------------------------------------------
# timeout / output-limit propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_and_output_limit_propagate_into_action() -> None:
    """The skill's default timeout and the module output limit bound the action."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        turn = await _executor().turn(
            graph, {"action": "echo probe", "skill_id": "recon_port_probe"}
        )
        assert turn.action.timeout_seconds == 90  # recon_port_probe default timeout
        assert turn.action.output_limit == DEFAULT_OUTPUT_LIMIT


# ---------------------------------------------------------------------------
# skill validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_not_covering_phase_raises() -> None:
    """A skill from another phase cannot be selected for the routed phase."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        with pytest.raises(InvalidSkillError, match="does not cover phase RECON"):
            await _executor().turn(
                graph, {"action": "echo probe", "skill_id": "exploit_parameter_injection"}
            )


@pytest.mark.asyncio
async def test_unknown_skill_raises() -> None:
    """An unregistered skill_id is rejected loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        with pytest.raises(InvalidSkillError, match="no_such_skill"):
            await _executor().turn(graph, {"action": "echo probe", "skill_id": "no_such_skill"})


@pytest.mark.asyncio
async def test_phase_without_skills_raises() -> None:
    """REPLAN has no skill packs, so no proposal can be bound there."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        route = await PhaseRouter().route(graph)
        assert route.phase == Phase.REPLAN
        with pytest.raises(InvalidSkillError, match="does not cover phase REPLAN"):
            await _executor().turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})


# ---------------------------------------------------------------------------
# policy gate integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_gate_rejects_unallowlisted_destination() -> None:
    """The scope gate's typed rejection propagates unchanged (fail closed)."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        with pytest.raises(AllowlistViolationError, match="not in the target allowlist"):
            await _executor().turn(
                graph,
                {"action": "curl -sS -m 5 http://10.0.0.5/", "skill_id": "recon_http_fingerprint"},
            )


@pytest.mark.asyncio
async def test_allowlisted_destination_passes_the_gate() -> None:
    """An allowlisted destination yields an approved, fingerprinted action."""
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        turn = await _executor(policy=ScopePolicy(target_allowlist=("10.0.0.5",))).turn(
            graph,
            {"action": "curl -sS -m 5 http://10.0.0.5/", "skill_id": "recon_http_fingerprint"},
        )
        assert turn.action.fingerprint == fingerprint_command("curl -sS -m 5 http://10.0.0.5/")[1]
        assert turn.action.timeout_seconds == 60  # recon_http_fingerprint default timeout


# ---------------------------------------------------------------------------
# persistence: plan entities, action entities, replayable events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_and_attempts_persisted_over_turns(tmp_path) -> None:
    """Plans persist as entities; every attempt is recorded as an action entity."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        executor = _executor(event_log=event_log)
        await executor.turn(graph, {"action": "echo one", "skill_id": plan.steps[0].skill_id})

        # the served plan was persisted as entities, exactly once so far
        assert await graph.get_entity(plan.id) is not None
        assert len(await graph.list_entities("plan")) == 1
        # every step was persisted
        step_records = await graph.list_entities("plan_step")
        assert len(step_records) == len(plan.steps)
        assert {step.id for step in plan.steps} == {record.id for record in step_records}
        # hypothesis-testing steps got PLANSTEP TESTS HYPOTHESIS edges
        for step in plan.steps:
            if step.hypothesis_id is not None:
                neighbors = await graph.neighbors(step.id)
                assert any(
                    edge.type == "PLANSTEP TESTS HYPOTHESIS" and edge.dst_id == step.hypothesis_id
                    for edge in neighbors.outgoing
                )
        # the attempt was recorded as an action entity keyed by fingerprint
        action_records = await graph.list_entities("action")
        assert len(action_records) == 1
        assert action_records[0].id == f"action-{fingerprint_command('echo one')[1]}"
        assert action_records[0].data["plan_id"] == plan.id

        # A second turn on the evolved graph derives a NEW deterministic
        # plan id (PR19 ids come from the graph hash, and the executor's
        # own persistence is part of that state), so the graph accumulates
        # the plan timeline; a plan id already present is never rewritten.
        second = await executor.turn(
            graph, {"action": "echo two", "skill_id": plan.steps[0].skill_id}
        )
        assert second.action.plan_id != plan.id
        assert len(await graph.list_entities("plan")) == 2
        assert len(await graph.list_entities("action")) == 2

    events = [json.loads(line) for line in (tmp_path / "actions.jsonl").read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types.count(EXECUTOR_PLAN_PERSISTED) == 2
    assert event_types.count(EXECUTOR_ACTION_ATTEMPTED) == 2
    assert GRAPH_ENTITY_CREATED in event_types
    assert GRAPH_EDGE_CREATED in event_types
    attempted = next(event for event in events if event["event_type"] == EXECUTOR_ACTION_ATTEMPTED)
    assert attempted["producer"] == "executor"
    assert attempted["payload"]["command"] == "echo one"
    assert attempted["payload"]["plan_id"] == plan.id


@pytest.mark.asyncio
async def test_plan_persistence_is_idempotent_per_plan_id() -> None:
    """Persisting the same plan id twice leaves exactly one plan entity."""
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        executor = _executor()
        # The guard is reachable through the private persistence path: the
        # same plan id can only be derived again from an unchanged graph,
        # which a turn's own writes preclude, so it is unit-tested directly.
        await executor._persist_plan(graph, plan)
        await executor._persist_plan(graph, plan)
        assert len(await graph.list_entities("plan")) == 1


@pytest.mark.asyncio
async def test_events_replay_reconstructs_graph_hash(tmp_path) -> None:
    """Replaying the executor's events reconstructs the identical graph hash."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    at = datetime(2026, 8, 7, tzinfo=UTC)
    async with StateGraph(":memory:") as graph:
        # Seed the baseline through logged mutations, mirroring the real
        # system: every graph mutation has a matching graph.* event, so a
        # fresh replay database reconstructs the same state.
        seed_entities = [
            ("run-1", "run", {}),
            ("tgt-1", "target", {"confirmed": True}),
            ("svc-1", "service", {"characterized": True}),
            ("hyp-a", "hypothesis", {"exploitable": True, "confidence": 0.8}),
            ("hyp-b", "hypothesis", {"exploitable": True, "confidence": 0.9}),
            ("ev-a1", "evidence", {}),
            ("ev-b1", "evidence", {}),
        ]
        for entity_id, entity_type, data in seed_entities:
            await graph.create_entity(entity_id, entity_type, data, at=at)
            event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    "run-1",
                    "test",
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        data=data,
                        at=at,
                    ),
                )
            )
        for edge_id, src_id, dst_id in (
            ("ev-a1->hyp-a", "ev-a1", "hyp-a"),
            ("ev-b1->hyp-b", "ev-b1", "hyp-b"),
        ):
            await graph.create_edge(
                edge_id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, src_id, dst_id, at=at
            )
            event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    "run-1",
                    "test",
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=at,
                    ),
                )
            )
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        executor = _executor(event_log=event_log)
        await executor.turn(graph, {"action": "echo one", "skill_id": plan.steps[0].skill_id})
        live_hash = await graph.graph_hash()

    replayed_hash = await replay_graph(tmp_path / "actions.jsonl", tmp_path / "replay.db")
    assert replayed_hash == live_hash


# ---------------------------------------------------------------------------
# typed errors and strict schemas
# ---------------------------------------------------------------------------


def test_error_hierarchy_is_typed() -> None:
    """All executor error classes derive from ExecutorError(RuntimeError)."""
    assert issubclass(MalformedOutputError, ExecutorError)
    assert issubclass(InvalidSkillError, ExecutorError)
    assert issubclass(DuplicateFingerprintError, ExecutorError)
    assert issubclass(PlanExhaustedError, ExecutorError)
    assert issubclass(ExecutorError, RuntimeError)


def test_schemas_reject_extra_fields() -> None:
    """The executor schemas are strict pydantic contracts (extra='forbid')."""
    fingerprint = fingerprint_command("echo probe")[1]
    with pytest.raises(ValidationError):
        ModelAction(action="echo probe", skill_id="recon_dns_enum", bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        FailedAction(fingerprint=fingerprint, reason="timeout", bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ActionRequest(
            action="echo probe",
            skill_id="recon_dns_enum",
            timeout_seconds=60,
            output_limit=DEFAULT_OUTPUT_LIMIT,
            fingerprint=fingerprint,
            phase=Phase.RECON,
            bogus=1,
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BudgetAccounting(tokens_used=0, model_calls_used=0, tool_calls_used=0, bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ExecutorTurn(
            phase=Phase.RECON,
            predicate="targets_unconfirmed",
            action=ActionRequest(
                action="echo probe",
                skill_id="recon_dns_enum",
                timeout_seconds=60,
                output_limit=DEFAULT_OUTPUT_LIMIT,
                fingerprint=fingerprint,
                phase=Phase.RECON,
            ),
            budget=BudgetAccounting(tokens_used=0, model_calls_used=0, tool_calls_used=0),
            bogus=1,
        )  # type: ignore[call-arg]


def test_failed_action_rejects_invalid_fingerprint() -> None:
    """FailedAction fingerprints must be 64-char sha256 hex digests."""
    with pytest.raises(ValidationError):
        FailedAction(fingerprint="not-a-fingerprint", reason="timeout")


def test_module_constants_are_documented() -> None:
    """The bounded-action contract is explicit module state."""
    assert MAX_ACTION_LENGTH == 4096
    assert DEFAULT_OUTPUT_LIMIT == 65536
    assert EXECUTOR_ACTION_ATTEMPTED == "executor.action_attempted"
    assert EXECUTOR_PLAN_PERSISTED == "executor.plan_persisted"
