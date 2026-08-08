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

from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.phases import Phase
from ozzgraph.scheduler import Task, WorkerRunStatus
from ozzgraph.shell import ToolResult, TruncationState
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
