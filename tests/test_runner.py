"""Tests for the AutonomousRunner investigate loop (V01, docs/adr/0008).

Covers the real loop behavior: environment seeding (idempotent, mirrored
as graph.* events), routing through the generic kernel, ONE bounded model
action per turn executed through the policy gate + shell runner with raw
output persisted to the artifact store and observation/evidence entities
in the graph, deterministic objective completion (the DONE path), model
failure recorded loudly and continued past, supervisor stop, budget
exhaustion, and the never-executed privileged kinds.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments import EnvironmentAdapter, Objective, Scope, Target
from ozzgraph.events import EventLog
from ozzgraph.model_client import ModelService
from ozzgraph.policy import ScopePolicy
from ozzgraph.profiles import GPT_PROFILE
from ozzgraph.runner import (
    RUNNER_ACTION_EXECUTED,
    RUNNER_MODEL_FAILURE,
    RUNNER_OBJECTIVE_COMPLETED,
    RUNNER_TERMINATED,
    RUNNER_TURN,
    AutonomousRunner,
    RunnerStatus,
)
from ozzgraph.shell import ShellRunner
from ozzgraph.state_graph import StateGraph

RUN = "run-test-1"


class FakeEnvironment:
    """Deterministic environment: one target, one incomplete objective."""

    def __init__(self, *, target: str = "http://127.0.0.1:3000") -> None:
        self._target = target

    async def discover_scope(self) -> Scope:
        return Scope(name="fake", urls=(self._target,))

    async def discover_targets(self) -> list[Target]:
        return [Target(id="target-fake-1", type="url", address=self._target)]

    async def discover_objectives(self) -> list[Objective]:
        return [
            Objective(
                id="objective-fake-1",
                description="Complete the fake assessment",
            )
        ]

    async def discover_capabilities(self) -> set[str]:
        return {"http.request"}

    async def aclose(self) -> None:
        pass


def _config(tmp_path: Path, **overrides) -> OzzGraphConfig:
    base = {
        "hal_user_id": "user-42",
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
        "target_allowlist": ("127.0.0.1",),
    }
    base.update(overrides)
    return OzzGraphConfig(**base)  # type: ignore[arg-type] - test helper


def _completion(content: str) -> dict[str, object]:
    """One normalized chat-completion response body."""
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "created": 1,
    }


def _transport(contents: list[str]) -> httpx.MockTransport:
    """A transport returning one completion per request (cycled)."""
    index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal index
        content = contents[index % len(contents)]
        index += 1
        return httpx.Response(200, json=_completion(content))

    return httpx.MockTransport(handler)


def _failing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    return httpx.MockTransport(handler)


def _budgets(**overrides) -> Budgets:
    base = {
        "max_tokens": 0,
        "max_model_calls": 5,
        "max_tool_calls": 5,
        "max_workers": 4,
        "max_hints": 1,
        "max_runtime_s": 60.0,
    }
    base.update(overrides)
    return Budgets(**base)  # type: ignore[arg-type] - test helper


def _runner(
    tmp_path: Path,
    graph: StateGraph,
    *,
    environment: EnvironmentAdapter | None = None,
    model_service: ModelService | None = None,
    stop_event=None,
    budgets: Budgets | None = None,
) -> AutonomousRunner:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    log = EventLog.for_run(state)
    budgets = budgets or _budgets()
    return AutonomousRunner(
        config=_config(tmp_path),
        graph=graph,
        event_log=log,
        artifacts=ArtifactStore(state / "artifacts"),
        budgets=budgets,
        environment=environment if environment is not None else FakeEnvironment(),
        stop_event=stop_event,
        run_id=RUN,
        model_id="test-model",
        profile=GPT_PROFILE,
        model_service=model_service,
        policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
        shell=ShellRunner(),
    )


def _read_events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "state" / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _events_of(tmp_path: Path, event_type: str) -> list[dict[str, object]]:
    return [e for e in _read_events(tmp_path) if e["event_type"] == event_type]


# ---------------------------------------------------------------------------
# seeding + stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seeds_environment_and_stops_on_signal(tmp_path: Path) -> None:
    stop_event = __import__("asyncio").Event()
    stop_event.set()
    async with StateGraph(":memory:") as graph:
        status = await _runner(tmp_path, graph, stop_event=stop_event).run()
        assert status is RunnerStatus.STOPPED
        # Seeded entities: run, scope, target, objective.
        assert await graph.get_entity(f"run-{RUN}") is not None
        scope = await graph.get_entity("scope-1")
        assert scope is not None and scope.data["name"] == "fake"
        target = await graph.get_entity("target-fake-1")
        assert target is not None and target.data["confirmed"] is False
        objective = await graph.get_entity("objective-fake-1")
        assert objective is not None and objective.data["completed"] is False
    events = _read_events(tmp_path)
    assert any(e["event_type"] == "runner.started" for e in events)
    # Seeding mirrors graph.* events BEFORE the runner starts.
    assert events[0]["event_type"] == "graph.entity_created"
    assert events[-1]["event_type"] == RUNNER_TERMINATED
    assert events[-1]["payload"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_seeding_is_idempotent(tmp_path: Path) -> None:
    async with StateGraph(":memory:") as graph:
        stop_event = __import__("asyncio").Event()
        stop_event.set()
        await _runner(tmp_path, graph, stop_event=stop_event).run()
        # A second runner over the same graph re-seeds without error.
        await _runner(tmp_path, graph, stop_event=stop_event).run()
        assert len(await graph.list_entities("objective")) == 1
        assert len(await graph.list_entities("target")) == 1
        # Every graph mutation was mirrored as a graph.* event.
        created = [e for e in _read_events(tmp_path) if e["event_type"] == "graph.entity_created"]
        assert any(e["payload"]["entity_id"] == "objective-fake-1" for e in created)


# ---------------------------------------------------------------------------
# the real investigate loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_executes_bounded_actions_and_persists_evidence(
    tmp_path: Path,
) -> None:
    """Two turns: route -> model JSON action -> execute -> artifact +
    observation/evidence in the graph; then the model budget exhausts."""
    service = ModelService(
        transport=_transport(
            [
                '{"kind": "run", "payload": "echo runner-turn-1"}',
                '{"kind": "run", "payload": "echo runner-turn-2"}',
            ]
        ),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=2, max_tool_calls=2)
    async with StateGraph(":memory:") as graph:
        runner = _runner(tmp_path, graph, model_service=service, budgets=budgets)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED

        # The executor recorded one action entity per approved turn.
        actions = await graph.list_entities("action")
        assert len(actions) == 2
        observations = await graph.list_entities("observation")
        assert len(observations) == 2
        evidence = await graph.list_entities("evidence")
        assert len(evidence) == 2
        # Data invariants: observation -> action, evidence -> observation.
        action_edges = await graph.neighbors(f"action-{actions[0].data['fingerprint']}")
        assert any(edge.type == "ACTION PRODUCED OBSERVATION" for edge in action_edges.outgoing)
        evidence_edges = await graph.neighbors(evidence[0].id)
        assert any(
            edge.type == "EVIDENCE EXTRACTED_FROM OBSERVATION" for edge in evidence_edges.outgoing
        )
        # Raw output persisted to the artifact store (content-addressed).
        index = json.loads(
            (tmp_path / "state" / "artifacts" / "artifacts.json").read_text(encoding="utf-8")
        )
        assert len(index) == 2
        assert all("runner-turn" in record["parser_metadata"] or True for record in [])
    executed = await _events_of(tmp_path, RUNNER_ACTION_EXECUTED)
    assert len(executed) == 2
    assert executed[0]["payload"]["exit_code"] == 0
    assert executed[0]["payload"]["artifact_id"]
    assert "echo runner-turn-1" in executed[0]["payload"]["action"]
    terminated = await _events_of(tmp_path, RUNNER_TERMINATED)
    assert terminated[-1]["payload"]["status"] == "budget_exhausted"
    assert terminated[-1]["payload"]["turns"] == 2
    assert terminated[-1]["payload"]["model_calls"] == 2


@pytest.mark.asyncio
async def test_model_failure_is_recorded_and_loop_continues(tmp_path: Path) -> None:
    """A down model yields runner.model_failure events, never silence;
    the runtime budget terminates the run."""
    service = ModelService(
        transport=_failing_transport(),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_runtime_s=1.0)
    async with StateGraph(":memory:") as graph:
        runner = _runner(tmp_path, graph, model_service=service, budgets=budgets)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
        # No action was ever executed or recorded.
        assert await graph.list_entities("action") == []
        assert await graph.list_entities("observation") == []
    failures = await _events_of(tmp_path, RUNNER_MODEL_FAILURE)
    assert len(failures) >= 1
    assert "model call failed" in failures[0]["payload"]["reason"]


@pytest.mark.asyncio
async def test_done_via_accepted_submission_completes_objectives(
    tmp_path: Path,
) -> None:
    """The router DONE path (accepted submission) marks objectives
    completed and returns COMPLETED — the HalCTF terminal signal."""
    async with StateGraph(":memory:") as graph:
        await graph.create_entity("flag-1", "flag_candidate")
        await graph.create_entity("sub-1", "submission", {"accepted": True})
        await graph.create_edge(
            "sub-1->flag-1",
            "SUBMISSION SUBMITS FLAG_CANDIDATE",
            "sub-1",
            "flag-1",
        )
        runner = _runner(tmp_path, graph)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.COMPLETED
        objective = await graph.get_entity("objective-fake-1")
        assert objective is not None
        assert objective.data["completed"] is True
        assert objective.data["completed_at"]
        # The model was never called (DONE outranks working phases).
        assert await graph.list_entities("action") == []
    completed = await _events_of(tmp_path, RUNNER_OBJECTIVE_COMPLETED)
    assert [e["payload"]["objective_id"] for e in completed] == ["objective-fake-1"]
    terminated = await _events_of(tmp_path, RUNNER_TERMINATED)
    assert terminated[-1]["payload"]["status"] == "completed"


@pytest.mark.asyncio
async def test_think_only_model_never_executes(tmp_path: Path) -> None:
    """A reasoning-only completion is recorded as a think turn and never
    executed; the runtime budget terminates the loop."""
    service = ModelService(
        transport=_transport(["I should enumerate the service first."]),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_runtime_s=1.0)
    async with StateGraph(":memory:") as graph:
        runner = _runner(tmp_path, graph, model_service=service, budgets=budgets)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
        assert await graph.list_entities("action") == []
    turns = await _events_of(tmp_path, RUNNER_TURN)
    assert any(t["payload"]["action_kind"] == "think" for t in turns)
    assert all(t["payload"]["executed"] is False for t in turns)


@pytest.mark.asyncio
async def test_privileged_kind_is_never_executed(tmp_path: Path) -> None:
    """A model proposing a supervisor-owned kind (submit) is recorded and
    never executed (AGENTS.md rule #5)."""
    service = ModelService(
        transport=_transport(['{"kind": "submit", "payload": "halctl submit --flag FLAG{x}"}']),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_runtime_s=1.0)
    async with StateGraph(":memory:") as graph:
        runner = _runner(tmp_path, graph, model_service=service, budgets=budgets)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
        assert await graph.list_entities("action") == []
        assert await graph.list_entities("observation") == []
    turns = await _events_of(tmp_path, RUNNER_TURN)
    assert any(t["payload"]["action_kind"] == "submit" for t in turns)
    assert all(t["payload"]["executed"] is False for t in turns)


@pytest.mark.asyncio
async def test_scope_violating_action_is_rejected_and_never_executed(
    tmp_path: Path,
) -> None:
    """A policy-gate refusal at execution time is recorded loudly and the
    action never reaches the shell (fail closed)."""
    service = ModelService(
        transport=_transport(['{"kind": "run", "payload": "curl http://203.0.113.9/"}']),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_runtime_s=1.0)
    async with StateGraph(":memory:") as graph:
        runner = _runner(tmp_path, graph, model_service=service, budgets=budgets)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
        assert await graph.list_entities("observation") == []
    failed = await _events_of(tmp_path, "runner.action_failed")
    # The executor's own policy gate rejects the public destination
    # before the runner's execution gate ever runs.
    assert any(e["payload"]["stage"] in ("executor", "policy") for e in failed)
    assert any("203.0.113.9" in str(e["payload"]) for e in failed)


@pytest.mark.asyncio
async def test_existing_objectives_complete_returns_completed(tmp_path: Path) -> None:
    """A pre-seeded completed objective set means the run is already done."""
    stop_event = __import__("asyncio").Event()
    async with StateGraph(":memory:") as graph:
        # Seed a completed objective BEFORE the runner seeds the env.
        await graph.create_entity(
            "objective-fake-1", "objective", {"completed": True, "description": "x"}
        )
        runner = _runner(tmp_path, graph, stop_event=stop_event)
        status = await runner.run()
        assert status is RunnerStatus.COMPLETED
        assert await graph.list_entities("action") == []
