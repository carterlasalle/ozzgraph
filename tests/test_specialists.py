"""Tests for V07 genuine narrow micro-agents (docs/CHANGES_v2.md milestone 7).

Covers the structured :class:`Verdict` contract (typed verdict, mandatory
non-empty evidence references per AGENTS.md rule #3, the CWE/assets/
confidence impact payload, loud validation), the :class:`MicroAgentTask`
schema (the inherited ``command`` is unused and locked to the empty string,
experiments are non-empty and attributed to the micro task's DAG node), and
the :class:`SpecialistMicroAgent` bounded loop: instance scoping (the
instance ``scope`` kwarg shadows the class default for every gate), the
assignment gate (an experiment outside the instance scope is rejected
loudly before anything is recorded), per-experiment family gates (a
forbidden experiment command raises and nothing executes), the loop bound
(at most ``MAX_MICRO_ITERATIONS`` experiments ever run), the deterministic
decide (confirmed on a clean probe with supporting signal, refuted on
empty clean probes, FAILED — never a finding — when no evidence was
gathered), and the fall-through single-action path for plain
:class:`WorkerTask` assignments.

Every test that rejects an experiment uses an instrumented recording
runner and asserts the runner was never reached, proving the gates fire
fail-closed BEFORE any execution.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.events import EventLog
from ozzgraph.phases import Phase
from ozzgraph.policy import ScopePolicy
from ozzgraph.scheduler import Task, WorkerRunStatus
from ozzgraph.shell import ToolResult, TruncationState
from ozzgraph.specialists import SpecialistError, SpecialistFleet
from ozzgraph.state_graph import StateGraph
from ozzgraph.workers import (
    DEFAULT_MICRO_AGENT_SCOPE,
    MAX_MICRO_ITERATIONS,
    FamilyOutOfScopeError,
    MicroAgentTask,
    SpecialistMicroAgent,
    SpecialistWorker,
    TaskOutOfScopeError,
    Verdict,
    WorkerScope,
    WorkerTask,
)


class RecordingRunner:
    """Fake bounded shell runner: records commands, returns clean output.

    Tests assert ``calls`` stays empty after a rejection, proving the
    worker gate fired BEFORE any execution.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        self.calls.append(command)
        return ToolResult(
            command=command,
            exit_code=0,
            stdout=f"out:{command}",
            stderr="",
            duration=0.01,
            timeout_state=False,
            truncation_state=TruncationState(),
        )


class EmptyOutputRunner:
    """Fake runner: exit 0 with NO output (a clean probe, no signal)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        self.calls.append(command)
        return ToolResult(
            command=command,
            exit_code=0,
            stdout="",
            stderr="",
            duration=0.01,
            timeout_state=False,
            truncation_state=TruncationState(),
        )


class FailingRunner:
    """Fake runner: every experiment exits nonzero (no evidence captured)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        self.calls.append(command)
        return ToolResult(
            command=command,
            exit_code=1,
            stdout="",
            stderr="boom",
            duration=0.01,
            timeout_state=False,
            truncation_state=TruncationState(),
        )


def make_micro_agent(
    tmp_path: Path,
    *,
    scope: WorkerScope | None = None,
    runner: object | None = None,
    artifacts: ArtifactStore | None = None,
) -> SpecialistMicroAgent:
    """A micro agent wired to a recording runner and a fresh artifact store."""
    return SpecialistMicroAgent(
        scope=scope,
        artifacts=artifacts if artifacts is not None else ArtifactStore(tmp_path / "artifacts"),
        runner=runner if runner is not None else RecordingRunner(),  # type: ignore[arg-type]
    )


def experiment(
    task: Task,
    command: str,
    *,
    phase: Phase = Phase.RECON,
    families: tuple[str, ...] = ("recon", "shell"),
    mutating: bool = False,
    targets: tuple[str, ...] = (),
) -> WorkerTask:
    """One bounded experiment whose required scope the default scope covers."""
    return WorkerTask(
        task=task,
        command=command,
        phase=phase,
        required_scope=WorkerScope(
            name=f"required-{task.id}",
            command_families=families,
            phases=(phase,),
            mutating=mutating,
            target_allowlist=targets,
        ),
    )


def micro_task(
    task_id: str = "t-micro-1",
    commands: tuple[str, ...] = ("echo probe-1",),
    *,
    hypothesis_id: str | None = "hyp-1",
    targets: tuple[str, ...] = (),
) -> MicroAgentTask:
    """A MicroAgentTask whose experiments share the micro task's DAG node."""
    task = Task(id=task_id, hypothesis_id=hypothesis_id)
    return MicroAgentTask(
        task=task,
        phase=Phase.RECON,
        required_scope=WorkerScope(
            name=f"required-{task_id}",
            command_families=("recon", "shell"),
            phases=(Phase.RECON,),
            mutating=False,
        ),
        experiments=tuple(experiment(task, command, targets=targets) for command in commands),
    )


# ---------------------------------------------------------------------------
# Verdict schema: typed, evidence-backed, loud
# ---------------------------------------------------------------------------


def test_verdict_valid_structure() -> None:
    verdict = Verdict(
        verdict="confirmed",
        evidence_ids=("artifact-1", "artifact-2"),
        impact={"cwe": None, "assets": ("target-a",), "confidence": 0.7},
        summary="micro agent confirmed: 1 experiment(s), evidence: artifact-1",
    )
    assert verdict.verdict == "confirmed"
    assert verdict.evidence_ids == ("artifact-1", "artifact-2")
    assert verdict.impact["confidence"] == 0.7
    assert verdict.impact["assets"] == ("target-a",)
    assert verdict.summary


def test_verdict_rejects_empty_and_blank_evidence() -> None:
    with pytest.raises(ValidationError, match="at least one evidence"):
        Verdict(
            verdict="confirmed",
            evidence_ids=(),
            impact={"cwe": None, "assets": (), "confidence": 0.7},
            summary="no evidence",
        )
    with pytest.raises(ValidationError):
        Verdict(
            verdict="confirmed",
            evidence_ids=("  ",),
            impact={"cwe": None, "assets": (), "confidence": 0.7},
            summary="blank evidence",
        )


def test_verdict_rejects_unknown_verdict_value() -> None:
    with pytest.raises(ValidationError):
        Verdict(  # type: ignore[call-arg]
            verdict="maybe",
            evidence_ids=("a1",),
            impact={"cwe": None, "assets": (), "confidence": 0.7},
            summary="bad verdict",
        )


def test_verdict_rejects_incomplete_or_mistyped_impact() -> None:
    with pytest.raises(ValidationError, match="missing"):
        Verdict(
            verdict="confirmed",
            evidence_ids=("a1",),
            impact={"cwe": None},  # assets/confidence missing
            summary="incomplete impact",
        )
    with pytest.raises(ValidationError, match="cwe"):
        Verdict(
            verdict="confirmed",
            evidence_ids=("a1",),
            impact={"cwe": " ", "assets": (), "confidence": 0.7},
            summary="blank cwe",
        )
    with pytest.raises(ValidationError, match="assets"):
        Verdict(
            verdict="confirmed",
            evidence_ids=("a1",),
            impact={"cwe": None, "assets": (" ",), "confidence": 0.7},
            summary="blank asset",
        )
    with pytest.raises(ValidationError, match="confidence"):
        Verdict(
            verdict="confirmed",
            evidence_ids=("a1",),
            impact={"cwe": None, "assets": (), "confidence": 1.5},
            summary="out of range confidence",
        )


def test_verdict_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Verdict(  # type: ignore[call-arg]
            verdict="confirmed",
            evidence_ids=("a1",),
            impact={"cwe": None, "assets": (), "confidence": 0.7},
            summary="extra",
            surprise=True,
        )


# ---------------------------------------------------------------------------
# MicroAgentTask schema: command locked to empty, experiments bounded
# ---------------------------------------------------------------------------


def test_micro_agent_task_valid_command_is_locked_empty() -> None:
    work = micro_task()
    assert work.command == ""  # the inherited command is unused (max_length=0)
    assert len(work.experiments) == 1
    assert work.experiments[0].command == "echo probe-1"


def test_micro_agent_task_rejects_nonempty_command() -> None:
    with pytest.raises(ValidationError):
        MicroAgentTask(  # type: ignore[call-arg]
            task=Task(id="t"),
            command="echo smuggled",
            phase=Phase.RECON,
            required_scope=DEFAULT_MICRO_AGENT_SCOPE,
            experiments=(),
        )


def test_micro_agent_task_rejects_empty_experiments() -> None:
    with pytest.raises(ValidationError, match="at least one bounded experiment"):
        MicroAgentTask(
            task=Task(id="t"),
            phase=Phase.RECON,
            required_scope=DEFAULT_MICRO_AGENT_SCOPE,
            experiments=(),
        )


def test_micro_agent_task_rejects_misattributed_experiment() -> None:
    task = Task(id="t-micro")
    other = Task(id="t-other")
    with pytest.raises(ValidationError, match="must carry the micro task's task id"):
        MicroAgentTask(
            task=task,
            phase=Phase.RECON,
            required_scope=DEFAULT_MICRO_AGENT_SCOPE,
            experiments=(experiment(other, "echo stray"),),
        )


# ---------------------------------------------------------------------------
# instance scoping and the assignment gate
# ---------------------------------------------------------------------------


def test_micro_agent_uses_instance_scope(tmp_path: Path) -> None:
    narrow = WorkerScope(
        name="narrow-probe",
        command_families=("shell",),
        phases=(Phase.RECON,),
        mutating=False,
    )
    agent = make_micro_agent(tmp_path, scope=narrow)
    assert agent.scope.name == "narrow-probe"
    assert agent.scope.command_families == ("shell",)

    default = make_micro_agent(tmp_path)
    assert default.scope is DEFAULT_MICRO_AGENT_SCOPE
    assert default.scope.name == "micro-agent"


def test_micro_agent_assign_rejects_experiment_out_of_scope(tmp_path: Path) -> None:
    narrow = WorkerScope(
        name="shell-only",
        command_families=("shell",),
        phases=(Phase.RECON,),
        mutating=False,
    )
    runner = RecordingRunner()
    agent = make_micro_agent(tmp_path, scope=narrow, runner=runner)
    work = micro_task(commands=("nmap -sV 10.0.0.5",))  # recon family, not shell
    with pytest.raises(TaskOutOfScopeError, match="recon"):
        agent.assign(work)
    assert agent.assignments == ()
    assert runner.calls == []  # nothing executed


def test_micro_agent_assign_rejects_task_out_of_scope(tmp_path: Path) -> None:
    agent = make_micro_agent(tmp_path)
    task = Task(id="t-exploit")
    work = MicroAgentTask(
        task=task,
        phase=Phase.RECON,
        required_scope=WorkerScope(
            name="required-exploit",
            command_families=("shell", "exploit"),
            phases=(Phase.RECON,),
            mutating=True,
        ),
        experiments=(experiment(task, "echo probe-1"),),
    )
    with pytest.raises(TaskOutOfScopeError, match="mutating"):
        agent.assign(work)
    assert agent.assignments == ()


# ---------------------------------------------------------------------------
# the bounded loop: deterministic decide, evidence-backed conclusions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_micro_agent_confirms_on_successful_experiment(tmp_path: Path) -> None:
    runner = RecordingRunner()
    agent = make_micro_agent(tmp_path, runner=runner)
    work = micro_task(commands=("echo probe-1",))
    agent.assign(work)
    outcome = await agent.run_task(work.task)
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert outcome.error is None
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    assert finding.task_id == "t-micro-1"  # attributed to the DAG node
    assert finding.source == "micro-agent"
    assert len(finding.evidence_ids) == 1  # the stored artifact (rule #3)
    record = await agent._artifacts.get(finding.evidence_ids[0])
    assert record.hash  # content-addressed evidence exists
    assert "confirmed" in finding.summary
    assert "hyp-1" in finding.summary
    assert runner.calls == ["echo probe-1"]


@pytest.mark.asyncio
async def test_micro_agent_refutes_on_empty_clean_probe(tmp_path: Path) -> None:
    runner = EmptyOutputRunner()
    agent = make_micro_agent(tmp_path, runner=runner)
    work = micro_task(commands=("echo nothing",))
    agent.assign(work)
    outcome = await agent.run_task(work.task)
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    assert len(finding.evidence_ids) == 1  # the clean-but-empty probe is still evidence
    assert "refuted" in finding.summary


@pytest.mark.asyncio
async def test_micro_agent_fails_loudly_without_evidence(tmp_path: Path) -> None:
    runner = FailingRunner()
    agent = make_micro_agent(tmp_path, runner=runner)
    work = micro_task(commands=("echo probe-1", "echo probe-2"))
    agent.assign(work)
    outcome = await agent.run_task(work.task)
    assert outcome.status is WorkerRunStatus.FAILED  # no evidence -> no finding (rule #3)
    assert outcome.findings == ()
    assert "no evidence" in (outcome.error or "")
    assert runner.calls == ["echo probe-1", "echo probe-2"]


@pytest.mark.asyncio
async def test_micro_agent_loop_is_bounded(tmp_path: Path) -> None:
    runner = RecordingRunner()
    agent = make_micro_agent(tmp_path, runner=runner)
    commands = tuple(f"echo probe-{index}" for index in range(1, 6))
    work = micro_task(commands=commands)
    agent.assign(work)
    outcome = await agent.run_task(work.task)
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    # At most MAX_MICRO_ITERATIONS experiments ever execute.
    assert len(runner.calls) == MAX_MICRO_ITERATIONS
    assert runner.calls == list(commands[:MAX_MICRO_ITERATIONS])
    assert len(outcome.findings[0].evidence_ids) == MAX_MICRO_ITERATIONS


@pytest.mark.asyncio
async def test_micro_agent_confirmed_evidence_merges_across_experiments(
    tmp_path: Path,
) -> None:
    """A later successful probe confirms after an earlier failed one."""

    class MixedRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(
            self,
            command: str,
            *,
            timeout_seconds: float,
            stdout_limit: int,
            stderr_limit: int,
            working_directory: Path,
        ) -> ToolResult:
            self.calls.append(command)
            exit_code = 0 if "probe-2" in command else 1
            return ToolResult(
                command=command,
                exit_code=exit_code,
                stdout=f"out:{command}" if exit_code == 0 else "",
                stderr="" if exit_code == 0 else "boom",
                duration=0.01,
                timeout_state=False,
                truncation_state=TruncationState(),
            )

    runner = MixedRunner()
    agent = make_micro_agent(tmp_path, runner=runner)
    work = micro_task(commands=("echo probe-1", "echo probe-2"))
    agent.assign(work)
    outcome = await agent.run_task(work.task)
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    finding = outcome.findings[0]
    assert "confirmed" in finding.summary
    assert len(finding.evidence_ids) == 1  # only the succeeded probe's artifact
    assert runner.calls == ["echo probe-1", "echo probe-2"]


@pytest.mark.asyncio
async def test_micro_agent_per_experiment_family_gate_fails_closed(
    tmp_path: Path,
) -> None:
    """A forbidden experiment command raises BEFORE anything executes."""
    narrow = WorkerScope(
        name="shell-only",
        command_families=("shell",),
        phases=(Phase.RECON,),
        mutating=False,
    )
    runner = RecordingRunner()
    agent = make_micro_agent(tmp_path, scope=narrow, runner=runner)
    task = Task(id="t-micro-1", hypothesis_id="hyp-1")
    # Both experiments declare a shell-only required scope (covered by the
    # instance scope, so the assignment gate passes); the SECOND command
    # itself classifies into recon, and the run-time family gate rejects
    # it before anything executes (the pattern of the ArtifactAnalysisWorker
    # test in test_workers.py).
    work = MicroAgentTask(
        task=task,
        phase=Phase.RECON,
        required_scope=WorkerScope(
            name="required-micro",
            command_families=("shell",),
            phases=(Phase.RECON,),
            mutating=False,
        ),
        experiments=(
            experiment(task, "echo ok", families=("shell",)),
            experiment(task, "nmap -sV 10.0.0.5", families=("shell",)),
        ),
    )
    agent.assign(work)
    with pytest.raises(FamilyOutOfScopeError, match="recon"):
        await agent.run_task(work.task)
    assert runner.calls == ["echo ok"]  # the forbidden experiment never executed


@pytest.mark.asyncio
async def test_micro_agent_runs_plain_task_through_standard_path(
    tmp_path: Path,
) -> None:
    """A plain WorkerTask assignment falls through to the single-action path."""
    runner = RecordingRunner()
    agent = make_micro_agent(tmp_path, runner=runner)
    task = Task(id="t-plain")
    plain = WorkerTask(
        task=task,
        command="echo plain",
        phase=Phase.RECON,
        required_scope=WorkerScope(
            name="required-plain",
            command_families=("recon", "shell"),
            phases=(Phase.RECON,),
            mutating=False,
        ),
    )
    agent.assign(plain)
    outcome = await agent.run_task(task)
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert len(outcome.findings) == 1
    assert runner.calls == ["echo plain"]


def test_micro_agent_error_hierarchy(tmp_path: Path) -> None:
    agent = make_micro_agent(tmp_path)
    assert isinstance(agent, SpecialistWorker)
    assert agent.worker_id == "micro-agent"
    assert agent.default_confidence == 0.7
    assert MAX_MICRO_ITERATIONS == 3


# ---------------------------------------------------------------------------
# SpecialistFleet: bounded parallel hypothesis batches through the Scheduler
# ---------------------------------------------------------------------------


class ScriptedShell:
    """Fake bounded shell: a scripted ToolResult per command, recording calls.

    When ``gated`` is True, every ``run`` blocks on an internal gate until
    the test releases it — the deterministic GateRunner pattern — so tests
    can hold experiments open and prove the batch parallelizes independent
    hypotheses (``max_active`` peaks at the true concurrency).
    """

    def __init__(self, results: dict[str, ToolResult], *, gated: bool = False) -> None:
        self.results = results
        self.calls: list[str] = []
        self.max_active = 0
        self._active = 0
        self._gate = asyncio.Event()
        if not gated:
            self._gate.set()  # non-gated shells proceed immediately
        self._entered = asyncio.Event()
        self._entered_count = 0

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        self.calls.append(command)
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        self._entered_count += 1
        self._entered.set()
        await self._gate.wait()
        self._active -= 1
        result = self.results.get(command)
        if result is None:
            return ToolResult(
                command=command,
                exit_code=0,
                stdout=f"out:{command}",
                stderr="",
                duration=0.01,
                timeout_state=False,
                truncation_state=TruncationState(),
            )
        return result

    async def wait_for_entries(self, count: int) -> None:
        """Block until ``count`` runs have entered ``run``."""
        while self._entered_count < count:
            self._entered.clear()
            await self._entered.wait()
        self._entered.clear()

    def release(self) -> None:
        """Open the gate so every blocked run proceeds."""
        self._gate.set()


def _result(command: str, *, exit_code: int = 0, stdout: str = "probe output") -> ToolResult:
    """One deterministic bounded shell result for a reproduction command."""
    return ToolResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr="" if exit_code == 0 else "boom",
        duration=0.01,
        timeout_state=False,
        truncation_state=TruncationState(),
    )


async def _seed_hypothesis(graph: StateGraph, hypothesis_id: str, direction: str) -> None:
    """One open, evidence-bearing hypothesis entity with a reproduction direction."""
    await graph.create_entity(
        hypothesis_id,
        "hypothesis",
        {
            "objective": f"test {hypothesis_id}",
            "exploitation_direction": direction,
            "confidence": 0.6,
            "status": "open",
        },
    )
    await graph.create_entity(
        f"ev-{hypothesis_id}",
        "evidence",
        {"note": "seed"},
    )
    await graph.create_edge(
        f"{hypothesis_id}-supported-by-ev-{hypothesis_id}",
        "EVIDENCE SUPPORTS HYPOTHESIS",
        f"ev-{hypothesis_id}",
        hypothesis_id,
    )


def _fleet(
    tmp_path: Path,
    *,
    shell: ScriptedShell,
    state_dir: Path | None = None,
    policy: ScopePolicy | None = None,
) -> SpecialistFleet:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return SpecialistFleet(
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        event_log=EventLog.for_run(state),
        run_id="run-fleet-1",
        policy=policy if policy is not None else ScopePolicy(target_allowlist=("127.0.0.1",)),
        shell=shell,
        max_workers=4,
        state_dir=state_dir,
    )


@pytest.mark.asyncio
async def test_fleet_runs_independent_hypotheses_in_parallel_and_merges_facts(
    tmp_path: Path,
) -> None:
    """Two independent hypotheses run concurrently; confirmed -> fact + finding."""
    shell = ScriptedShell(
        {
            "echo probe-h1": _result("echo probe-h1"),
            "echo probe-h2": _result("echo probe-h2"),
        },
        gated=True,  # hold both experiments open to prove parallelism
    )
    async with StateGraph(":memory:") as graph:
        await _seed_hypothesis(graph, "h-1", "echo probe-h1")
        await _seed_hypothesis(graph, "h-2", "echo probe-h2")
        fleet = _fleet(tmp_path, shell=shell)
        batch = asyncio.create_task(
            fleet.run_hypothesis_batch(
                graph, hypothesis_ids=("h-1", "h-2"), phase=Phase.ENUMERATION
            )
        )
        await shell.wait_for_entries(2)
        assert shell.max_active == 2  # both independent hypotheses active at once
        shell.release()
        result = await batch
        assert result.scheduled == 2
        assert result.succeeded == 2
        assert result.failed == 0
        assert set(result.promoted) == {"h-1", "h-2"}  # both confirmed
        assert result.abandoned == ()
        assert result.open_hypotheses == ()
        assert len(result.facts) == 2  # reducer merged the verdicts into facts
        assert len(result.findings) == 2
        # terminal lifecycle: promoted
        h1 = await graph.get_entity("h-1")
        h2 = await graph.get_entity("h-2")
        assert h1 is not None and h1.data["status"] == "promoted"
        assert h2 is not None and h2.data["status"] == "promoted"
        # reducer wrote facts; findings entities + validates edges exist
        facts = await graph.list_entities("fact")
        assert len(facts) == 2
        findings = await graph.list_entities("finding")
        assert len(findings) == 2
    # findings rendered to findings.json when a state_dir is configured
    shell2 = ScriptedShell({"echo probe-h1": _result("echo probe-h1")})
    async with StateGraph(":memory:") as graph:
        await _seed_hypothesis(graph, "h-1", "echo probe-h1")
        fleet2 = _fleet(tmp_path, shell=shell2, state_dir=tmp_path / "state")
        result2 = await fleet2.run_hypothesis_batch(
            graph, hypothesis_ids=("h-1",), phase=Phase.ENUMERATION
        )
        assert len(result2.findings) == 1
        assert (tmp_path / "state" / "findings.json").exists()


@pytest.mark.asyncio
async def test_fleet_promotes_confirmed_and_abandons_refuted(tmp_path: Path) -> None:
    """Confirmed hypotheses promote with a finding; refuted ones abandon."""
    shell = ScriptedShell(
        {
            "echo probe-h1": _result("echo probe-h1"),  # non-empty -> confirmed
            "echo probe-h2": _result("echo probe-h2", stdout=""),  # empty -> refuted
        }
    )
    async with StateGraph(":memory:") as graph:
        await _seed_hypothesis(graph, "h-1", "echo probe-h1")
        await _seed_hypothesis(graph, "h-2", "echo probe-h2")
        fleet = _fleet(tmp_path, shell=shell)
        result = await fleet.run_hypothesis_batch(
            graph, hypothesis_ids=("h-1", "h-2"), phase=Phase.ENUMERATION
        )
        assert result.promoted == ("h-1",)
        assert result.abandoned == ("h-2",)
        assert result.open_hypotheses == ()
        assert len(result.facts) == 2  # both verdicts become facts
        assert len(result.findings) == 1  # only the confirmed one gets a finding
        h1 = await graph.get_entity("h-1")
        h2 = await graph.get_entity("h-2")
        assert h1 is not None and h1.data["status"] == "promoted"
        assert h2 is not None and h2.data["status"] == "abandoned"


@pytest.mark.asyncio
async def test_fleet_skips_mutating_direction_loudly(tmp_path: Path) -> None:
    """A mutating reproduction direction is skipped (stays open), never run."""
    shell = ScriptedShell({})
    async with StateGraph(":memory:") as graph:
        await _seed_hypothesis(graph, "h-1", "hydra -l admin -P /tmp/pass 127.0.0.1")
        await _seed_hypothesis(graph, "h-2", "echo probe-h2")
        fleet = _fleet(tmp_path, shell=shell)
        result = await fleet.run_hypothesis_batch(
            graph, hypothesis_ids=("h-1", "h-2"), phase=Phase.ENUMERATION
        )
        assert result.scheduled == 1  # h-1 (mutating) was skipped
        assert shell.calls == ["echo probe-h2"]  # the mutating direction never ran
        assert result.promoted == ("h-2",)
        h1 = await graph.get_entity("h-1")
        assert h1 is not None and h1.data["status"] == "open"  # left open


@pytest.mark.asyncio
async def test_fleet_failed_run_leaves_hypothesis_open(tmp_path: Path) -> None:
    """A failed experiment gathers no evidence -> the run fails, hypothesis stays open."""
    shell = ScriptedShell({"echo probe-h1": _result("echo probe-h1", exit_code=1)})
    async with StateGraph(":memory:") as graph:
        await _seed_hypothesis(graph, "h-1", "echo probe-h1")
        fleet = _fleet(tmp_path, shell=shell)
        result = await fleet.run_hypothesis_batch(
            graph, hypothesis_ids=("h-1",), phase=Phase.ENUMERATION
        )
        assert result.scheduled == 1
        assert result.succeeded == 0
        assert result.failed == 1
        assert result.promoted == ()
        assert result.abandoned == ()
        assert result.open_hypotheses == ("h-1",)
        assert result.facts == ()  # no evidence -> no fact
        assert result.findings == ()
        h1 = await graph.get_entity("h-1")
        assert h1 is not None and h1.data["status"] == "open"


@pytest.mark.asyncio
async def test_fleet_missing_hypothesis_raises_loudly(tmp_path: Path) -> None:
    """A hypothesis absent from the graph is corrupt kernel state -> SpecialistError."""
    shell = ScriptedShell({})
    async with StateGraph(":memory:") as graph:
        fleet = _fleet(tmp_path, shell=shell)
        with pytest.raises(SpecialistError, match="not in the graph"):
            await fleet.run_hypothesis_batch(
                graph, hypothesis_ids=("ghost-1",), phase=Phase.ENUMERATION
            )


@pytest.mark.asyncio
async def test_fleet_rejects_empty_batch(tmp_path: Path) -> None:
    shell = ScriptedShell({})
    async with StateGraph(":memory:") as graph:
        fleet = _fleet(tmp_path, shell=shell)
        with pytest.raises(ValueError, match="non-empty"):
            await fleet.run_hypothesis_batch(graph, hypothesis_ids=(), phase=Phase.ENUMERATION)


def test_fleet_requires_nonempty_run_id_and_positive_workers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        SpecialistFleet(artifacts=ArtifactStore(tmp_path / "a"), run_id="")
    with pytest.raises(ValueError, match="max_workers"):
        SpecialistFleet(artifacts=ArtifactStore(tmp_path / "a"), run_id="r", max_workers=0)
