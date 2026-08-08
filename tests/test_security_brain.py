"""Tests for the V06 security brain (docs/CHANGES_v2.md milestone 6).

Covers the opportunity generator (scored ranking, lifecycle-status and
executed-command exclusion, deterministic service probes, loud payload
failures), the brain's decision rules (single obvious action ->
deterministic with zero LLM calls, > 1 viable path -> StrategicPlanner,
lone hypothesis / fresh graph -> model fallback), the runner wiring
(the single-obvious path makes ZERO model completions against a
recording model client; the multi-path turn invokes the model exactly
once with the ranked opportunities in context and the confirmed
hypothesis is promoted end-to-end), the hypothesis lifecycle manager
(create -> evidence -> promote/abandon, event-mirrored), the task
builder's executor-parity binding, and the progress evaluator's
continue/pivot/finish decisions.

Every test uses its own in-memory SQLite graph (":memory:").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozzgraph.adapters import ParsedAction
from ozzgraph.artifacts import ArtifactStore
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments import Objective, Scope, Target
from ozzgraph.evaluator import Evaluator
from ozzgraph.events import EventLog
from ozzgraph.executor import FailedAction
from ozzgraph.model_client import (
    ModelChoice,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from ozzgraph.phases import Phase
from ozzgraph.planner import EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS, InvalidGraphStateError
from ozzgraph.policy import ScopePolicy
from ozzgraph.profiles import GPT_PROFILE
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    PhaseRoute,
    PhaseRouter,
)
from ozzgraph.runner import AutonomousRunner, RunnerStatus
from ozzgraph.security_brain import (
    BRAIN_DETERMINISTIC_ACTION,
    BRAIN_HYPOTHESIS_ABANDONED,
    BRAIN_HYPOTHESIS_PROMOTED,
    STATUS_ABANDONED,
    STATUS_OPEN,
    STATUS_PROMOTED,
    BrainError,
    DeterministicActionDecision,
    FallbackDecision,
    HypothesisManager,
    Opportunity,
    OpportunityGenerator,
    OpportunityKind,
    ProgressEvaluator,
    ProgressVerdict,
    SecurityBrain,
    StrategicDecision,
    StrategicPlanner,
    TaskBuilder,
)
from ozzgraph.shell import ShellRunner, ToolResult, TruncationState
from ozzgraph.state_graph import StateGraph
from ozzgraph.toolplane import ToolInventory

RUN = "run-brain-test-1"

#: The deterministic service probe the generator derives.
SERVICE_PROBE = "nmap -sV --top-ports 1000 svc-2"


class FakeEnvironment:
    """Deterministic environment: one target, one incomplete objective."""

    async def discover_scope(self) -> Scope:
        return Scope(name="fake", urls=("http://127.0.0.1:3000",))

    async def discover_targets(self) -> list[Target]:
        return [Target(id="target-fake-1", type="url", address="http://127.0.0.1:3000")]

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


class RecordingModel:
    """Duck-typed model client recording every completion request."""

    def __init__(self, content: str | None = None) -> None:
        self.content = content or '{"kind": "run", "payload": "echo brain-probe"}'
        self.calls: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            id="chatcmpl-brain",
            model="test-model",
            choices=[
                ModelChoice(
                    index=0,
                    message=ModelMessage(role="assistant", content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            created=1,
        )

    async def aclose(self) -> None:
        pass


class FakeShell(ShellRunner):
    """Deterministic shell double: canned ToolResults keyed by command."""

    def __init__(self, results: dict[str, ToolResult]) -> None:
        super().__init__()
        self._results = results
        self.ran: list[str] = []

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        self.ran.append(command)
        try:
            return self._results[command]
        except KeyError:
            raise AssertionError(f"unexpected command: {command!r}") from None


def _ok_result(command: str, stdout: str = "ok") -> ToolResult:
    return ToolResult(
        action_id="a" * 32,
        command=command,
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration=0.01,
        timeout_state=False,
        truncation_state=TruncationState(),
    )


def _config(tmp_path: Path) -> OzzGraphConfig:
    return OzzGraphConfig(
        hal_user_id="user-42",
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "state" / "artifacts",
        target_allowlist=("127.0.0.1",),
    )


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
    model_service: RecordingModel | None = None,
    budgets: Budgets | None = None,
    shell: FakeShell | None = None,
    with_evaluator: bool = False,
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
        environment=FakeEnvironment(),
        run_id=RUN,
        model_id="test-model",
        profile=GPT_PROFILE,
        model_service=model_service,  # type: ignore[arg-type] - duck-typed fake
        policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
        shell=shell if shell is not None else ShellRunner(),
        # Hermetic tool plane: an empty search path finds no tools, so
        # no version probe ever spawns a subprocess (deterministic).
        inventory=ToolInventory(paths=()),
        evaluator=Evaluator(run_id=RUN, event_log=log) if with_evaluator else None,
    )


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
    """Seed a run with a confirmed target and a characterized service."""
    await _entity(graph, "run-1", "run")
    await _entity(graph, "tgt-1", "target", {"confirmed": True})
    await _entity(graph, "svc-1", "service", {"characterized": True})


async def _seed_hypothesis(
    graph: StateGraph,
    hypothesis_id: str,
    supporting: tuple[str, ...] = (),
    contradicting: tuple[str, ...] = (),
    *,
    data: dict[str, object] | None = None,
) -> None:
    """Seed one evidenced hypothesis with evidence entities and edges."""
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
    for evidence_id in contradicting:
        await _entity(graph, f"ev-{evidence_id}", "evidence")
        await _edge(
            graph,
            f"ev-{evidence_id}->{hypothesis_id}",
            EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS,
            f"ev-{evidence_id}",
            hypothesis_id,
        )


# ---------------------------------------------------------------------------
# opportunity generator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generator_ranks_hypotheses_then_services() -> None:
    """Hypotheses (confidence, then weight, then id) outrank services."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8}
        )
        await _seed_hypothesis(
            graph,
            "hyp-b",
            ("b1", "b2"),
            data={"exploitable": True, "confidence": 0.9},
        )
        await _entity(graph, "svc-2", "service", {"characterized": False})
        route = await PhaseRouter().route(graph)
        opportunities = await OpportunityGenerator().generate(graph, route)

    assert [o.entity_id for o in opportunities] == ["hyp-b", "hyp-a", "svc-2"]
    assert [o.kind for o in opportunities] == [
        OpportunityKind.TEST_HYPOTHESIS,
        OpportunityKind.TEST_HYPOTHESIS,
        OpportunityKind.CHARACTERIZE_SERVICE,
    ]
    # Hypothesis opportunities carry no deterministic action (testing
    # needs judgment); the service opportunity is fully deterministic.
    assert all(o.action is None for o in opportunities[:2])
    assert opportunities[2].action == SERVICE_PROBE
    assert opportunities[2].skill_id == route.skills[0].skill_id
    # Scores: hypotheses outrank services; confidence outranks weight.
    assert opportunities[0].score > opportunities[1].score > opportunities[2].score


@pytest.mark.asyncio
async def test_generator_skips_resolved_and_already_attempted_paths() -> None:
    """Promoted/abandoned hypotheses and executed probes never resurface."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph,
            "hyp-done",
            ("a1",),
            data={"exploitable": True, "status": STATUS_PROMOTED},
        )
        await _seed_hypothesis(
            graph,
            "hyp-dead",
            ("b1",),
            data={"exploitable": True, "status": STATUS_ABANDONED},
        )
        await _seed_hypothesis(graph, "hyp-open", ("c1",), data={"exploitable": True})
        await _entity(graph, "svc-2", "service", {"characterized": False})
        # The probe for svc-2 was already executed (recorded action).
        await _entity(graph, "action-1", "action", {"command": SERVICE_PROBE})
        route = await PhaseRouter().route(graph)
        opportunities = await OpportunityGenerator().generate(graph, route)

    assert [o.entity_id for o in opportunities] == ["hyp-open"]


@pytest.mark.asyncio
async def test_generator_fails_loudly_on_invalid_confidence() -> None:
    """A wrong-typed hypothesis payload fails loudly (AGENTS.md rule #9)."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-bad", ("a1",), data={"exploitable": True, "confidence": 5.0}
        )
        route = await PhaseRouter().route(graph)
        with pytest.raises(InvalidGraphStateError):
            await OpportunityGenerator().generate(graph, route)


# ---------------------------------------------------------------------------
# security brain decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_fresh_graph_falls_back_to_model() -> None:
    """No opportunities: the runner keeps the model-propose path."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        route = await PhaseRouter().route(graph)
        decision = await SecurityBrain().decide(graph, route)
    assert isinstance(decision, FallbackDecision)
    assert decision.phase is route.phase


@pytest.mark.asyncio
async def test_decide_lone_hypothesis_falls_back_to_model() -> None:
    """A lone hypothesis needs judgment to test: no deterministic action."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(graph, "hyp-1", ("a1",), data={"exploitable": True})
        route = await PhaseRouter().route(graph)
        decision = await SecurityBrain().decide(graph, route)
    assert isinstance(decision, FallbackDecision)
    assert "hyp-1" in decision.reason


@pytest.mark.asyncio
async def test_decide_single_service_is_deterministic() -> None:
    """Exactly one obvious action: a deterministic task, no LLM needed."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "svc-2", "service", {"characterized": False})
        route = await PhaseRouter().route(graph)
        decision = await SecurityBrain().decide(graph, route)
    assert isinstance(decision, DeterministicActionDecision)
    assert decision.task.command == SERVICE_PROBE
    assert decision.task.skill_id == route.skills[0].skill_id
    assert decision.task.hypothesis_id is None


@pytest.mark.asyncio
async def test_decide_multi_path_is_strategic() -> None:
    """More than one viable path invokes the StrategicPlanner (LLM)."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8}
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.9}
        )
        route = await PhaseRouter().route(graph)
        decision = await SecurityBrain().decide(graph, route)
    assert isinstance(decision, StrategicDecision)
    assert [o.entity_id for o in decision.opportunities] == ["hyp-b", "hyp-a"]
    assert decision.plan is not None
    assert [h.id for h in decision.plan.hypotheses] == ["hyp-b", "hyp-a"]
    assert "STRATEGIC OPPORTUNITIES" in decision.strategy_prompt
    assert "hyp-b" in decision.strategy_prompt


# ---------------------------------------------------------------------------
# runner wiring: the zero-LLM deterministic path and the strategic path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_single_obvious_action_makes_zero_llm_calls(
    tmp_path: Path,
) -> None:
    """Exactly one obvious action executes deterministically: the
    recording model client is NEVER called and the probe runs once."""
    recording = RecordingModel()
    shell = FakeShell({SERVICE_PROBE: _ok_result(SERVICE_PROBE, stdout="22/tcp open ssh")})
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "svc-2", "service", {"characterized": False})
        runner = _runner(tmp_path, graph, model_service=recording, budgets=budgets, shell=shell)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
        # Zero model completions; the deterministic probe executed.
        assert recording.calls == []
        assert shell.ran == [SERVICE_PROBE]
        actions = await graph.list_entities("action")
        assert len(actions) == 1
        assert actions[0].data["command"] == SERVICE_PROBE
        # The deterministic turn is recorded as such.
    events = _read_events(tmp_path)
    assert any(e["event_type"] == BRAIN_DETERMINISTIC_ACTION for e in events)
    assert any(e["event_type"] == "runner.turn" and e["payload"]["plan_id"] is None for e in events)


@pytest.mark.asyncio
async def test_runner_multi_path_invokes_strategic_planner_and_promotes(
    tmp_path: Path,
) -> None:
    """Two viable paths: the model is called exactly once with the
    ranked opportunities in context, the binding plan is persisted, the
    model-chosen action executes, and the confirmed hypothesis is
    promoted end-to-end."""
    command = "echo strategic-probe"
    recording = RecordingModel(content=json.dumps({"kind": "run", "payload": command}))
    shell = FakeShell({command: _ok_result(command, stdout="probed")})
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8}
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.9}
        )
        runner = _runner(
            tmp_path,
            graph,
            model_service=recording,
            budgets=budgets,
            shell=shell,
            with_evaluator=True,
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED

        # The StrategicPlanner was invoked exactly once, with the
        # ranked opportunities in context.
        assert len(recording.calls) == 1
        prompt = recording.calls[0].messages[0].content
        assert "STRATEGIC OPPORTUNITIES" in prompt
        assert "opportunity-test_hypothesis-hyp-b" in prompt
        assert "opportunity-test_hypothesis-hyp-a" in prompt

        # The executor persisted the binding plan and executed the
        # model-chosen action against the first un-failed step.
        assert await graph.list_entities("plan")
        assert await graph.list_entities("plan_step")
        assert shell.ran == [command]

        # The evaluator confirmed the step's hypothesis (new evidence
        # after plan creation) and the manager promoted it.
        hyp_b = await graph.get_entity("hyp-b")
        assert hyp_b is not None
        assert hyp_b.data["status"] == STATUS_PROMOTED


@pytest.mark.asyncio
async def test_runner_lone_hypothesis_uses_model_fallback(tmp_path: Path) -> None:
    """A lone hypothesis is not an obvious action: the model is called
    once, WITHOUT the strategic context."""
    command = "echo lone-probe"
    recording = RecordingModel(content=json.dumps({"kind": "run", "payload": command}))
    shell = FakeShell({command: _ok_result(command)})
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(graph, "hyp-1", ("a1",), data={"exploitable": True})
        runner = _runner(tmp_path, graph, model_service=recording, budgets=budgets, shell=shell)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED
        assert len(recording.calls) == 1
        assert "STRATEGIC OPPORTUNITIES" not in recording.calls[0].messages[0].content
        assert shell.ran == [command]


# ---------------------------------------------------------------------------
# hypothesis lifecycle manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_manager_lifecycle(tmp_path: Path) -> None:
    """create -> evidence -> promote/abandon, idempotent and mirrored."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    log = EventLog.for_run(state)
    manager = HypothesisManager(event_log=log, run_id=RUN)
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "ev-1", "evidence")
        await manager.create(
            graph,
            hypothesis_id="hyp-1",
            objective="the service exposes an admin panel",
            exploitation_direction="curl /admin",
            confidence=0.6,
            evidence_id="ev-1",
        )
        record = await graph.get_entity("hyp-1")
        assert record is not None and record.type == "hypothesis"
        assert record.data["status"] == STATUS_OPEN
        assert record.data["confidence"] == 0.6
        supports = await graph.neighbors("hyp-1", EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS)
        assert [edge.src_id for edge in supports.incoming] == ["ev-1"]

        # create is idempotent (entity id derives from the fingerprint).
        await manager.create(
            graph,
            hypothesis_id="hyp-1",
            objective="x",
            exploitation_direction="y",
            confidence=0.6,
            evidence_id="ev-1",
        )
        assert len(await graph.list_entities("hypothesis")) == 1

        # New contradicting evidence attaches a contradicts edge.
        await _entity(graph, "ev-2", "evidence")
        await manager.attach_evidence(
            graph, hypothesis_id="hyp-1", evidence_id="ev-2", supports=False
        )
        contradicts = await graph.neighbors("hyp-1", EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS)
        assert [edge.src_id for edge in contradicts.incoming] == ["ev-2"]

        # Promote is terminal; abandon on a promoted hypothesis is a no-op.
        await manager.promote(graph, hypothesis_id="hyp-1")
        record = await graph.get_entity("hyp-1")
        assert record is not None and record.data["status"] == STATUS_PROMOTED
        assert record.data["promoted_at"]
        await manager.abandon(graph, hypothesis_id="hyp-1")
        record = await graph.get_entity("hyp-1")
        assert record is not None and record.data["status"] == STATUS_PROMOTED

        # A fresh hypothesis abandons cleanly.
        await _entity(graph, "ev-3", "evidence")
        await manager.create(
            graph,
            hypothesis_id="hyp-2",
            objective="o",
            exploitation_direction="d",
            confidence=0.5,
            evidence_id="ev-3",
        )
        await manager.abandon(graph, hypothesis_id="hyp-2")
        record = await graph.get_entity("hyp-2")
        assert record is not None and record.data["status"] == STATUS_ABANDONED
        assert record.data["abandoned_at"]

        # Missing entities never fail the loop (defensive no-op).
        await manager.promote(graph, hypothesis_id="hyp-missing")
        await manager.abandon(graph, hypothesis_id="hyp-missing")

    # Every mutation mirrored as a graph.* event; lifecycle run events.
    events = _read_events(tmp_path)
    created = [e for e in events if e["event_type"] == "graph.entity_created"]
    assert any(e["payload"]["entity_id"] == "hyp-1" for e in created)
    assert any(e["event_type"] == BRAIN_HYPOTHESIS_PROMOTED for e in events)
    assert any(e["event_type"] == BRAIN_HYPOTHESIS_ABANDONED for e in events)


# ---------------------------------------------------------------------------
# progress evaluator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_evaluator_decides_continue_pivot_finish() -> None:
    """No objectives is never finish; completed objectives finish;
    every hypothesis resolved with incomplete objectives pivots."""
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "ev-1", "evidence")
        decision = await ProgressEvaluator().evaluate(graph)
    assert decision.verdict is ProgressVerdict.CONTINUE
    assert decision.evidence_count == 1

    async with StateGraph(":memory:") as graph:
        await _entity(graph, "obj-1", "objective", {"completed": True, "description": "x"})
        decision = await ProgressEvaluator().evaluate(graph)
    assert decision.verdict is ProgressVerdict.FINISH
    assert decision.completed_objectives == 1 and decision.total_objectives == 1

    async with StateGraph(":memory:") as graph:
        await _entity(graph, "obj-1", "objective", {"completed": False, "description": "x"})
        await _entity(graph, "hyp-1", "hypothesis", {"status": STATUS_ABANDONED})
        await _entity(graph, "hyp-2", "hypothesis", {"status": STATUS_PROMOTED})
        decision = await ProgressEvaluator().evaluate(graph)
    assert decision.verdict is ProgressVerdict.PIVOT
    assert decision.abandoned_hypotheses == 1 and decision.promoted_hypotheses == 1

    async with StateGraph(":memory:") as graph:
        await _entity(graph, "obj-1", "objective", {"completed": False, "description": "x"})
        await _entity(graph, "hyp-1", "hypothesis", {"status": STATUS_OPEN})
        decision = await ProgressEvaluator().evaluate(graph)
    assert decision.verdict is ProgressVerdict.CONTINUE
    assert decision.open_hypotheses == 1

    # A fresh graph with no hypotheses and incomplete objectives
    # continues (no strategic path is dead — none exists).
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "obj-1", "objective", {"completed": False, "description": "x"})
        decision = await ProgressEvaluator().evaluate(graph)
    assert decision.verdict is ProgressVerdict.CONTINUE


# ---------------------------------------------------------------------------
# task builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_builder_strategic_binding_mirrors_executor() -> None:
    """The strategic task binds the first un-failed plan step, exactly
    like the executor's step selection."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8}
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.9}
        )
        route = await PhaseRouter().route(graph)
        opportunities = await OpportunityGenerator().generate(graph, route)
        strategic = await StrategicPlanner().plan(graph, route, opportunities)
    plan = strategic.plan
    assert plan is not None
    parsed = ParsedAction(
        kind="run",
        payload="echo probe",
        raw='{"kind": "run", "payload": "echo probe"}',
    )
    builder = TaskBuilder()

    first = await builder.build_strategic(route, plan, parsed, [])
    assert first is not None
    assert first.command == "echo probe"
    assert first.plan_id == plan.id
    assert first.plan_step_id == plan.steps[0].id
    assert first.hypothesis_id == plan.steps[0].hypothesis_id
    assert first.skill_id == plan.steps[0].skill_id

    # A failed first step is skipped exactly like the executor skips it.
    failed = [
        FailedAction(
            fingerprint="a" * 64,
            reason="exit_code=1",
            plan_step_id=plan.steps[0].id,
        )
    ]
    second = await builder.build_strategic(route, plan, parsed, failed)
    assert second is not None
    assert second.plan_step_id == plan.steps[1].id

    # Every step failed -> no task (re-plan, never loop).
    all_failed = [
        FailedAction(fingerprint="b" * 64, reason="timeout", plan_step_id=step.id)
        for step in plan.steps
    ]
    assert await builder.build_strategic(route, plan, parsed, all_failed) is None


@pytest.mark.asyncio
async def test_task_builder_deterministic_rejects_judgment_opportunity() -> None:
    """A non-deterministic opportunity cannot build a deterministic task."""
    opportunity = Opportunity(
        id="opportunity-test_hypothesis-hyp-1",
        kind=OpportunityKind.TEST_HYPOTHESIS,
        entity_id="hyp-1",
        objective="resolve hyp-1",
        score=1000.0,
        rationale="r",
        hypothesis_id="hyp-1",
    )
    route = PhaseRoute(phase=Phase.EXPLOITATION, predicate="has_exploitable_hypothesis")
    with pytest.raises(BrainError):
        await TaskBuilder().build_deterministic(route, opportunity)


def _read_events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "state" / "actions.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
