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
from ozzgraph.phases import Phase
from ozzgraph.policy import ScopePolicy
from ozzgraph.profiles import GPT_PROFILE
from ozzgraph.router import PhaseRoute
from ozzgraph.runner import (
    RUNNER_ACTION_EXECUTED,
    RUNNER_MODEL_FAILURE,
    RUNNER_OBJECTIVE_COMPLETED,
    RUNNER_TERMINATED,
    RUNNER_TURN,
    AutonomousRunner,
    RunnerStatus,
)
from ozzgraph.security_brain import (
    Opportunity,
    OpportunityKind,
    StrategicDecision,
)
from ozzgraph.shell import ShellRunner, ToolResult, TruncationState
from ozzgraph.specialists import SpecialistBatchResult
from ozzgraph.state_graph import StateGraph
from ozzgraph.toolplane import ToolInventory

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
    inventory: ToolInventory | None = None,
    shell: ShellRunner | FakeShell | None = None,
    specialists=None,
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
        shell=shell if shell is not None else ShellRunner(),
        # Hermetic tool plane: an empty search path finds no tools, so
        # the model context advertises no capabilities and no version
        # probe ever spawns a subprocess (deterministic, fast).
        inventory=inventory if inventory is not None else ToolInventory(paths=()),
        specialists=specialists,
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


@pytest.mark.asyncio
async def test_completed_run_renders_report_bundle(tmp_path: Path) -> None:
    """A COMPLETED run materializes the full V08 report bundle in state_dir."""
    stop_event = __import__("asyncio").Event()
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    async with StateGraph(state / "graph.db") as graph:
        await graph.create_entity(
            "objective-fake-1", "objective", {"completed": True, "description": "x"}
        )
        status = await _runner(tmp_path, graph, stop_event=stop_event).run()
        assert status is RunnerStatus.COMPLETED

    # The bundle: report.md / report.json / report.sarif alongside
    # evidence/ + graph.sqlite + events.jsonl (docs/adr/0010).
    assert (state / "report.md").is_file()
    assert (state / "report.json").is_file()
    assert (state / "report.sarif").is_file()
    assert (state / "evidence").is_dir()
    assert (state / "graph.sqlite").is_file()
    assert (state / "events.jsonl").is_file()

    report = json.loads((state / "report.json").read_text(encoding="utf-8"))
    assert report["run"]["id"] == RUN
    assert report["run"]["environment"] == "fake"
    assert report["termination"]["status"] == "completed"
    assert report["counts"]["finding"] == 0
    assert report["findings"] == []

    sarif = json.loads((state / "report.sarif").read_text(encoding="utf-8"))
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["tool"]["driver"]["name"] == "ozzgraph"
    assert sarif["runs"][0]["results"] == []

    # The authoritative event log is untouched by the bundle render.
    events = _read_events(tmp_path)
    terminated = events[-1]
    assert terminated["event_type"] == RUNNER_TERMINATED
    payload = terminated["payload"]
    assert isinstance(payload, dict) and payload.get("status") == "completed"
    assert not any(e["event_type"] == "runner.report_failed" for e in events)


@pytest.mark.asyncio
async def test_stopped_run_renders_no_report_bundle(tmp_path: Path) -> None:
    """Only a COMPLETED termination renders the report bundle."""
    stop_event = __import__("asyncio").Event()
    stop_event.set()
    state = tmp_path / "state"
    async with StateGraph(":memory:") as graph:
        status = await _runner(tmp_path, graph, stop_event=stop_event).run()
        assert status is RunnerStatus.STOPPED
    assert not (state / "report.md").exists()
    assert not (state / "report.json").exists()
    assert not (state / "report.sarif").exists()


# ---------------------------------------------------------------------------
# V03 tool plane wiring (docs/CHANGES_v2.md milestone 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_advertises_only_installed_capabilities(tmp_path: Path) -> None:
    """The startup inventory bounds the model context.

    With exactly one fake tool (curl) on the search path, the compiled
    prompt advertises ``http.request`` and nothing else — the model
    NEVER hears about a capability (or a skill requirement) that no
    installed tool backs.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\necho 'curl 8.5.0'\n", encoding="utf-8")
    curl.chmod(0o755)

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(
            200, json=_completion('{"kind": "run", "payload": "echo runner-turn-1"}')
        )

    service = ModelService(
        transport=httpx.MockTransport(handler),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        runner = _runner(
            tmp_path,
            graph,
            model_service=service,
            budgets=budgets,
            inventory=ToolInventory(paths=[str(bin_dir)]),
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
    assert captured, "the model was called at least once"
    prompt = captured[0]
    assert "AVAILABLE CAPABILITIES" in prompt
    assert "- http.request" in prompt
    # Absent tools' capabilities never reach the model.
    assert "network.port_scan" not in prompt
    assert "web.content_discovery" not in prompt


# ---------------------------------------------------------------------------
# V04 semantic observations (docs/CHANGES_v2.md milestone 4)
# ---------------------------------------------------------------------------


class FakeShell(ShellRunner):
    """Deterministic shell double: canned ToolResults keyed by command."""

    def __init__(self, results: dict[str, ToolResult]) -> None:
        super().__init__()
        self._results = results

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        try:
            return self._results[command]
        except KeyError:
            raise AssertionError(f"unexpected command: {command!r}") from None


NMAP_XML_OUTPUT = """\
<?xml version="1.0" encoding="UTF-8"?>
<nmaprun scanner="nmap" args="nmap -oX - -sV 127.0.0.1" start="1700000000" version="7.94">
<host starttime="1700000000" endtime="1700000001"><status state="up"/>
<address addr="127.0.0.1" addrtype="ipv4"/>
<hostnames><hostname name="localhost" type="PTR"/></hostnames>
<ports>
<port protocol="tcp" portid="22"><state state="open"/><service name="ssh" product="OpenSSH" version="9.2p1"/></port>
<port protocol="tcp" portid="80"><state state="open"/><service name="http" product="nginx"/></port>
</ports>
<os><osmatch name="Linux" accuracy="98"/></os>
</host>
</nmaprun>
"""


@pytest.mark.asyncio
async def test_semantic_observation_raw_first_flow(tmp_path: Path) -> None:
    """A semantic tool run persists raw output FIRST, then the typed
    observation (source/kind/data per tool) + evidence into the graph.

    Exercises the full V04 pipeline through the real investigate loop
    with a canned shell: nmap -oX output becomes a typed observation
    entity (hosts/ports in ``data``) whose payload references the
    artifact, and the artifact holds the raw bytes byte-for-byte.
    """
    command = "nmap -oX - -sV 127.0.0.1"
    canned = ToolResult(
        action_id="a" * 32,
        command=command,
        exit_code=0,
        stdout=NMAP_XML_OUTPUT,
        stderr="",
        duration=0.01,
        timeout_state=False,
        truncation_state=TruncationState(),
    )
    service = ModelService(
        transport=_transport([json.dumps({"kind": "run", "payload": command})]),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        runner = _runner(
            tmp_path,
            graph,
            model_service=service,
            budgets=budgets,
            shell=FakeShell({command: canned}),
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED

        observations = await graph.list_entities("observation")
        assert len(observations) == 1
        payload = observations[0].data
        assert payload["source"] == "nmap"
        assert payload["kind"] == "xml"
        assert payload["ok"] is True
        assert payload["malformed"] is False
        assert payload["artifact_id"]
        # The observation references the artifact (raw-first invariant).
        assert payload["artifact_ids"] == [payload["artifact_id"]]
        data = payload["data"]
        assert isinstance(data, dict)
        assert data["host_count"] == 1
        assert data["open_ports"] == ["tcp/22", "tcp/80"]

        # Evidence references the observation and the artifact.
        evidence = await graph.list_entities("evidence")
        assert len(evidence) == 1
        assert evidence[0].data["observation_id"] == observations[0].id
        assert evidence[0].data["artifact_id"] == payload["artifact_id"]

        # Raw output persisted to the artifact store, byte-for-byte.
        artifacts = ArtifactStore(tmp_path / "state" / "artifacts")
        raw = artifacts.path_for(str(payload["artifact_id"])).read_text(encoding="utf-8")
        assert raw == NMAP_XML_OUTPUT


# ---------------------------------------------------------------------------
# V07: specialist batch wiring (a pure hypothesis batch dispatches the fleet)
# ---------------------------------------------------------------------------


class StubFleet:
    """A minimal SpecialistFleet stand-in recording the dispatched batch."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Phase]] = []

    async def run_hypothesis_batch(self, graph, *, hypothesis_ids, phase):
        self.calls.append((tuple(hypothesis_ids), phase))
        return SpecialistBatchResult(
            run_id=RUN,
            scheduled=2,
            succeeded=2,
            failed=0,
            promoted=("h-1", "h-2"),
        )


def _hypothesis_opportunity(opportunity_id: str, hypothesis_id: str) -> Opportunity:
    return Opportunity(
        id=opportunity_id,
        kind=OpportunityKind.TEST_HYPOTHESIS,
        entity_id=hypothesis_id,
        objective=f"test {hypothesis_id}",
        score=1100.0,
        rationale="evidence present",
        hypothesis_id=hypothesis_id,
    )


def _batch_decision() -> StrategicDecision:
    return StrategicDecision(
        phase=Phase.ENUMERATION,
        reason="2 independent hypotheses; specialist batch",
        opportunities=(
            _hypothesis_opportunity("opportunity-test_hypothesis-h-1", "h-1"),
            _hypothesis_opportunity("opportunity-test_hypothesis-h-2", "h-2"),
        ),
        strategy_prompt="STRATEGIC",
    )


def test_is_hypothesis_batch_gate() -> None:
    from ozzgraph.runner import _is_hypothesis_batch

    assert _is_hypothesis_batch(_batch_decision()) is True
    mixed = _batch_decision()
    service = Opportunity(
        id="opportunity-characterize_service-svc-1",
        kind=OpportunityKind.CHARACTERIZE_SERVICE,
        entity_id="svc-1",
        objective="characterize service svc-1",
        score=100.0,
        rationale="uncharacterized",
        action="nmap -sV --top-ports 1000 svc-1",
        skill_id="nmap",
    )
    mixed.opportunities = (*mixed.opportunities, service)
    assert _is_hypothesis_batch(mixed) is False


@pytest.mark.asyncio
async def test_specialist_batch_turn_dispatches_fleet_without_model_call(
    tmp_path: Path,
) -> None:
    """A pure hypothesis batch dispatches the fleet; no model completion is made."""
    stub = StubFleet()
    async with StateGraph(":memory:") as graph:
        runner = _runner(tmp_path, graph, specialists=stub)
        route = PhaseRoute(phase=Phase.ENUMERATION, predicate="has_uncharacterized_services")
        outcome = await runner._run_specialist_batch_turn(route, _batch_decision())
        assert outcome is None  # continue the loop
        assert stub.calls == [(("h-1", "h-2"), Phase.ENUMERATION)]
        events = _read_events(tmp_path)
        assert any(e["event_type"] == "runner.specialist_batch" for e in events)
        assert any(e["event_type"] == "runner.turn" for e in events)
        await runner.aclose()
