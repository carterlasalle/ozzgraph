"""HAL-006 tests: objective completion is acceptance-gated per environment.

The pre-HAL-006 bug: the runner's ``_evaluate`` completed EVERY
objective on an evaluator ``PlanVerdict.COMPLETE`` (runner.py), so a
validated SQLi hypothesis in a HalCTF run could mark
``objective-halctf-flag`` completed with NO successful submission and
terminate the run COMPLETED unscored.

HAL-006 fixes it at the environment seam
(:meth:`ozzgraph.environments.base.EnvironmentAdapter.verdict_satisfies_objectives`):

- ``HalCTFEnvironment`` only accepts the COMPLETE verdict when the graph
  already holds an accepted submission entity (the router's terminal
  signal) — a validated hypothesis alone never completes the objective;
- ``LocalEnvironment`` keeps the pre-HAL-006 behavior: the deterministic
  COMPLETE verdict IS the completion signal, so it always satisfies the
  objective;
- the runner consults the predicate before ``_complete_objectives()``
  but still produces the evidence-backed Finding unconditionally on a
  COMPLETE verdict;
- the accepted-submission DONE path (the HAL-005 flow) is unchanged.

Style mirrors tests/test_security_brain.py (runner harness with a
scripted strategic turn) and tests/test_flag_loop.py (HalCTF
environment + seeded submission).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments import (
    EnvironmentAdapter,
    HalCTFEnvironment,
    LocalEnvironment,
    Objective,
    Scope,
    Target,
)
from ozzgraph.evaluator import Evaluator
from ozzgraph.events import EventLog
from ozzgraph.model_client import ModelChoice, ModelMessage, ModelRequest, ModelResponse, ModelUsage
from ozzgraph.policy import ScopePolicy
from ozzgraph.profiles import GPT_PROFILE
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
    ENTITY_SUBMISSION,
    InvalidGraphStateError,
)
from ozzgraph.runner import (
    RUNNER_OBJECTIVE_COMPLETED,
    AutonomousRunner,
    RunnerStatus,
)
from ozzgraph.shell import ShellRunner, ToolResult, TruncationState
from ozzgraph.state_graph import StateGraph
from ozzgraph.toolplane import ToolInventory

RUN = "run-obj-accept-1"
CHALLENGE = "web-01"
HALCTF_TARGET_ID = f"halctf-challenge-{CHALLENGE}"


class FakeEnvironment:
    """Local-semantics environment: one target, one incomplete objective."""

    async def discover_scope(self) -> Scope:
        return Scope(name="fake", urls=("http://127.0.0.1:3000",))

    async def discover_targets(self) -> list[Target]:
        return [Target(id="target-fake-1", type="url", address="http://127.0.0.1:3000")]

    async def discover_objectives(self) -> list[Objective]:
        return [Objective(id="objective-fake-1", description="Complete the fake assessment")]

    async def discover_capabilities(self) -> set[str]:
        return {"http.request"}

    async def verdict_satisfies_objectives(self, graph: StateGraph) -> bool:
        # Local semantics: the evaluator COMPLETE verdict satisfies the
        # objective unconditionally (HAL-006 keeps this behavior).
        return True

    async def aclose(self) -> None:
        pass


class RecordingModel:
    """Duck-typed model client recording every completion request."""

    def __init__(self, content: str | None = None) -> None:
        self.content = content or '{"kind": "run", "payload": "echo obj-accept-probe"}'
        self.calls: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            id="chatcmpl-obj-accept",
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


def _halctf_environment(config: OzzGraphConfig) -> HalCTFEnvironment:
    """A HalCTF environment with only the challenge id (no sidecar)."""
    return HalCTFEnvironment(config, environ={"HAL_CHALLENGE_ID": CHALLENGE})


def _runner(
    tmp_path: Path,
    graph: StateGraph,
    *,
    environment: EnvironmentAdapter,
    model_service: RecordingModel,
    budgets: Budgets,
    shell: FakeShell,
) -> AutonomousRunner:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    log = EventLog.for_run(state)
    return AutonomousRunner(
        config=_config(tmp_path),
        graph=graph,
        event_log=log,
        artifacts=ArtifactStore(state / "artifacts"),
        budgets=budgets,
        environment=environment,
        run_id=RUN,
        model_id="test-model",
        profile=GPT_PROFILE,
        model_service=model_service,  # type: ignore[arg-type] - duck-typed fake
        policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
        shell=shell,
        # Hermetic tool plane: an empty search path finds no tools, so
        # no version probe ever spawns a subprocess (deterministic).
        inventory=ToolInventory(paths=()),
        evaluator=Evaluator(run_id=RUN, event_log=log),
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


async def _seed_strategic_graph(graph: StateGraph, *, target_id: str = "target-fake-1") -> None:
    """Seed a branching EXPLOITATION graph: two evidenced hypotheses.

    Mirrors tests/test_security_brain.py::_seed_baseline +
    _seed_hypothesis: the graph routes EXPLOITATION, the brain's
    strategic planner persists a plan, and the executed first step
    confirms hyp-b with NEW evidence -> the evaluator verdict is
    COMPLETE (a ranked hypothesis gained new supporting evidence).
    """
    await _entity(graph, "run-1", "run")
    await _entity(graph, target_id, "target", {"confirmed": True})
    await _entity(graph, "svc-1", "service", {"characterized": True})
    await _seed_hypothesis(graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8})
    await _seed_hypothesis(graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.9})


async def _seed_hypothesis(
    graph: StateGraph,
    hypothesis_id: str,
    supporting: tuple[str, ...],
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


def _read_events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "state" / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# the environment completion predicate (the HAL-006 seam)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halctf_verdict_requires_accepted_submission() -> None:
    """HalCTF: only an accepted submission entity satisfies the objective.

    An empty graph and a rejected submission both leave the objective
    unsatisfied; the accepted submission (the router's terminal signal)
    satisfies it — a validated hypothesis alone never would.
    """
    environment = _halctf_environment(_config(Path("/tmp/x")))
    async with StateGraph(":memory:") as graph:
        assert await environment.verdict_satisfies_objectives(graph) is False

        await _entity(graph, "sub-1", ENTITY_SUBMISSION, {"accepted": False})
        assert await environment.verdict_satisfies_objectives(graph) is False

        await _entity(graph, "sub-2", ENTITY_SUBMISSION, {"accepted": True})
        assert await environment.verdict_satisfies_objectives(graph) is True


@pytest.mark.asyncio
async def test_halctf_verdict_fails_loudly_on_non_bool_accepted() -> None:
    """A non-bool ``accepted`` payload is invalid graph state (rule #9).

    The environment reads the strict-boolean contract exactly like the
    router: a present non-bool value raises instead of being coerced.
    """
    environment = _halctf_environment(_config(Path("/tmp/x")))
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "sub-1", ENTITY_SUBMISSION, {"accepted": "yes"})
        with pytest.raises(InvalidGraphStateError, match="accepted"):
            await environment.verdict_satisfies_objectives(graph)


@pytest.mark.asyncio
async def test_local_verdict_always_satisfies_objectives() -> None:
    """Local: the deterministic COMPLETE verdict is the completion signal.

    The pre-HAL-006 behavior is preserved byte-for-byte: a validated
    finding may satisfy the local assessment objective, so the predicate
    accepts the verdict unconditionally.
    """
    environment = LocalEnvironment(_config(Path("/tmp/x")), environ={})
    async with StateGraph(":memory:") as graph:
        assert await environment.verdict_satisfies_objectives(graph) is True


# ---------------------------------------------------------------------------
# runner integration: COMPLETE verdict + no submission (the HAL-006 bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halctf_complete_verdict_without_submission_leaves_objective_incomplete(
    tmp_path: Path,
) -> None:
    """A validated hypothesis (COMPLETE) never completes the flag objective.

    The strategic turn confirms hyp-b -> the evaluator verdict is
    COMPLETE -> the Finding renders (``_produce_findings`` stays
    unconditional) but ``objective-halctf-flag`` stays incomplete
    because no submission was accepted, and the run continues (budget
    exhaustion, NOT a COMPLETED termination).
    """
    environment = _halctf_environment(_config(tmp_path))
    model = RecordingModel(content=json.dumps({"kind": "run", "payload": "echo strategic-probe"}))
    shell = FakeShell({"echo strategic-probe": _ok_result("echo strategic-probe", stdout="probed")})
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        await _seed_strategic_graph(graph, target_id=HALCTF_TARGET_ID)
        runner = _runner(
            tmp_path,
            graph,
            environment=environment,
            model_service=model,
            budgets=budgets,
            shell=shell,
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED  # not COMPLETED

        # The evaluator DID reach COMPLETE and produced the Finding...
        assert await graph.get_entity("finding-hyp-b") is not None
        # ...but the objective stayed incomplete without a submission.
        objective = await graph.get_entity("objective-halctf-flag")
        assert objective is not None
        assert objective.data["completed"] is False
        assert "completed_at" not in objective.data
        assert await graph.list_entities(ENTITY_SUBMISSION) == []

    events = _read_events(tmp_path)
    assert not any(e["event_type"] == RUNNER_OBJECTIVE_COMPLETED for e in events)


@pytest.mark.asyncio
async def test_local_complete_verdict_still_completes_objectives(tmp_path: Path) -> None:
    """Local mode is byte-for-byte unchanged: COMPLETE completes the run.

    The same strategic turn against a local-semantics environment
    completes the objective on the COMPLETE verdict and the next
    iteration terminates COMPLETED via the progress evaluator's FINISH
    (every objective completed).
    """
    environment = FakeEnvironment()
    model = RecordingModel(content=json.dumps({"kind": "run", "payload": "echo strategic-probe"}))
    shell = FakeShell({"echo strategic-probe": _ok_result("echo strategic-probe", stdout="probed")})
    budgets = _budgets(max_model_calls=2, max_tool_calls=2)
    async with StateGraph(":memory:") as graph:
        await _seed_strategic_graph(graph)
        runner = _runner(
            tmp_path,
            graph,
            environment=environment,
            model_service=model,
            budgets=budgets,
            shell=shell,
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.COMPLETED

        objective = await graph.get_entity("objective-fake-1")
        assert objective is not None
        assert objective.data["completed"] is True
        assert objective.data["completed_at"]

    events = _read_events(tmp_path)
    completed = [e for e in events if e["event_type"] == RUNNER_OBJECTIVE_COMPLETED]
    assert [e["payload"]["objective_id"] for e in completed] == ["objective-fake-1"]


# ---------------------------------------------------------------------------
# the accepted-submission DONE path is unchanged (the HAL-005 flow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halctf_accepted_submission_still_completes_objective(
    tmp_path: Path,
) -> None:
    """An accepted submission routes DONE and completes the objective.

    The HAL-005 terminal flow is preserved with the real HalCTF
    environment: the accepted submission outranks every working phase,
    the runner marks ``objective-halctf-flag`` completed, and the run
    terminates COMPLETED with zero model calls.
    """
    environment = _halctf_environment(_config(tmp_path))
    budgets = _budgets()
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "flag-1", "flag_candidate")
        await _entity(graph, "sub-1", ENTITY_SUBMISSION, {"accepted": True})
        await _edge(
            graph,
            "sub-1->flag-1",
            EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
            "sub-1",
            "flag-1",
        )
        runner = _runner(
            tmp_path,
            graph,
            environment=environment,
            model_service=RecordingModel(),
            budgets=budgets,
            shell=FakeShell({}),
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.COMPLETED
        objective = await graph.get_entity("objective-halctf-flag")
        assert objective is not None
        assert objective.data["completed"] is True
        assert objective.data["completed_at"]
        # DONE outranks working phases: no action ever executed.
        assert await graph.list_entities("action") == []
