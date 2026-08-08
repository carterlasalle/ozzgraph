"""Tests for scope-limited specialist workers (PR25).

Covers the declarative :class:`WorkerScope` contract (loud construction
validation: empty scopes, blank/duplicate/unknown families, read-only
scopes declaring mutating families, target-allowlist validation, and
deterministic canonicalization), scope containment (families, phases,
mutation permission, and CIDR-aware target narrowing), the assignment
gate (out-of-scope tasks, duplicate assignments, the supervisor-serialized
task gate), run-time action enforcement (a read-only worker can never run
a mutating-family command, families outside the declared scope are
rejected loudly, nothing ever executes on a rejection), the bounded
execution pipeline (policy gate, fingerprint duplicate rejection,
content-addressed artifacts as mandatory finding evidence, structured
failed outcomes), and an integration of a TaskDAG of specialist workers
driven through the Scheduler with deterministic results and replay-
consistent graph persistence (in-memory SQLite plus file-backed replay,
following the PR24 pattern).

Every test that rejects a worker action uses an instrumented recording
runner and asserts the runner was never reached, proving scope
violations fail closed BEFORE any execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.events import (
    GRAPH_ENTITY_CREATED,
    EventLog,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.phases import Phase
from ozzgraph.policy import (
    AllowlistViolationError,
    DuplicateActionError,
    ScopePolicy,
)
from ozzgraph.replay import replay_graph
from ozzgraph.scheduler import (
    SCHEDULER_PRODUCER,
    SERIALIZED_CONFLICT_KEY,
    Scheduler,
    Task,
    TaskDAG,
    TaskOutcome,
    WorkerRunStatus,
    serialized_task,
)
from ozzgraph.shell import ShellRunner, ToolResult, TruncationState
from ozzgraph.state_graph import StateGraph
from ozzgraph.workers import (
    ArtifactAnalysisWorker,
    DuplicateAssignmentError,
    EmptyWorkerScopeError,
    FamilyOutOfScopeError,
    ReadOnlyScopeError,
    ReadOnlyViolationError,
    ReconWorker,
    SerializationRequiredError,
    SpecialistWorker,
    SubmissionWorker,
    TaskOutOfScopeError,
    UnknownCommandFamilyError,
    WorkerError,
    WorkerScope,
    WorkerScopeError,
    WorkerTask,
)

RUN = "run-1"


# ---------------------------------------------------------------------------
# instrumented runner: records calls, never executes anything real
# ---------------------------------------------------------------------------


class RecordingRunner:
    """Fake bounded shell runner: records commands, returns a clean result.

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


def make_worker(
    worker_type: type[SpecialistWorker],
    tmp_path: Path,
    *,
    runner: object | None = None,
    policy: ScopePolicy | None = None,
    artifacts: ArtifactStore | None = None,
) -> SpecialistWorker:
    """A worker wired to a recording runner, a policy, and a fresh store."""
    return worker_type(  # type: ignore[call-arg]
        artifacts=artifacts if artifacts is not None else ArtifactStore(tmp_path / "artifacts"),
        runner=runner if runner is not None else RecordingRunner(),  # type: ignore[arg-type]
        policy=policy,
    )


def recon_assignment(
    task_id: str = "t-1",
    command: str = "echo probe",
    *,
    phase: Phase = Phase.RECON,
    families: tuple[str, ...] = ("recon", "shell"),
    mutating: bool = False,
    targets: tuple[str, ...] = (),
    working_directory: str = ".",
) -> WorkerTask:
    """A WorkerTask whose required scope matches the recon worker's."""
    return WorkerTask(
        task=Task(id=task_id),
        command=command,
        phase=phase,
        required_scope=WorkerScope(
            name=f"required-{task_id}",
            command_families=families,
            phases=(phase,),
            mutating=mutating,
            target_allowlist=targets,
        ),
        working_directory=working_directory,
    )


# ---------------------------------------------------------------------------
# scope construction: declarative, validated loudly
# ---------------------------------------------------------------------------


def test_worker_scope_valid_declaration() -> None:
    scope = WorkerScope(
        name="recon",
        command_families=("Recon", "SHELL"),
        phases=("POST_EXPLOITATION", "RECON", "RECON"),
        mutating=False,
        target_allowlist=("10.0.0.0/24",),
    )
    assert scope.name == "recon"
    assert scope.command_families == ("recon", "shell")  # casefolded
    assert [phase.value for phase in scope.phases] == ["RECON", "POST_EXPLOITATION"]  # canonical
    assert scope.mutating is False
    assert scope.target_allowlist == ("10.0.0.0/24",)


def test_worker_scope_rejects_empty_scopes() -> None:
    with pytest.raises(EmptyWorkerScopeError, match="command family"):
        WorkerScope(name="e", command_families=(), phases=(Phase.RECON,), mutating=False)
    with pytest.raises(EmptyWorkerScopeError, match="phase"):
        WorkerScope(name="e", command_families=("recon",), phases=(), mutating=False)


def test_worker_scope_rejects_blank_and_duplicate_families() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        WorkerScope(name="b", command_families=("  ",), phases=(Phase.RECON,), mutating=False)
    with pytest.raises(ValidationError, match="unique"):
        WorkerScope(
            name="d", command_families=("recon", "recon"), phases=(Phase.RECON,), mutating=False
        )


def test_worker_scope_rejects_unknown_family() -> None:
    with pytest.raises(UnknownCommandFamilyError, match="unknown command family"):
        WorkerScope(name="u", command_families=("exploitz",), phases=(Phase.RECON,), mutating=False)


def test_worker_scope_read_only_cannot_declare_mutating_family() -> None:
    with pytest.raises(ReadOnlyScopeError, match="mutating"):
        WorkerScope(
            name="r",
            command_families=("recon", "exploit"),
            phases=(Phase.RECON,),
            mutating=False,
        )
    # A mutating scope may declare the mutating family.
    WorkerScope(
        name="m",
        command_families=("shell", "exploit"),
        phases=(Phase.EXPLOITATION,),
        mutating=True,
    )


def test_worker_scope_rejects_blank_and_duplicate_targets() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        WorkerScope(
            name="t",
            command_families=("recon",),
            phases=(Phase.RECON,),
            mutating=False,
            target_allowlist=(" ",),
        )
    with pytest.raises(ValidationError, match="unique"):
        WorkerScope(
            name="t",
            command_families=("recon",),
            phases=(Phase.RECON,),
            mutating=False,
            target_allowlist=("10.0.0.5", "10.0.0.5"),
        )


def test_worker_scope_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkerScope(  # type: ignore[call-arg]
            name="x",
            command_families=("recon",),
            phases=(Phase.RECON,),
            mutating=False,
            surprise=True,
        )


def test_worker_scope_covers_containment() -> None:
    provided = WorkerScope(
        name="recon",
        command_families=("recon", "shell"),
        phases=(Phase.RECON, Phase.ENUMERATION),
        mutating=False,
        target_allowlist=("10.0.0.0/24",),
    )
    assert provided.covers(provided)  # a scope covers itself

    covered = WorkerScope(
        name="task",
        command_families=("shell",),
        phases=(Phase.RECON,),
        mutating=False,
        target_allowlist=("10.0.0.5",),  # covered by the 10.0.0.0/24 CIDR
    )
    assert provided.covers(covered)
    assert provided.gaps(covered) == ()

    # missing family
    missing_family = WorkerScope(
        name="task", command_families=("exploit",), phases=(Phase.RECON,), mutating=True
    )
    assert not provided.covers(missing_family)
    assert "command families" in provided.gaps(missing_family)[0]

    # missing phase
    missing_phase = WorkerScope(
        name="task",
        command_families=("recon",),
        phases=(Phase.EXPLOITATION,),
        mutating=False,
    )
    assert not provided.covers(missing_phase)
    assert "phases" in provided.gaps(missing_phase)[0]

    # mutating work on a read-only worker
    mutating_work = WorkerScope(
        name="task", command_families=("recon",), phases=(Phase.RECON,), mutating=True
    )
    assert not provided.covers(mutating_work)
    assert "mutating" in provided.gaps(mutating_work)[0]

    # target outside the narrowing (string match against a CIDR entry)
    outside_target = WorkerScope(
        name="task",
        command_families=("recon",),
        phases=(Phase.RECON,),
        mutating=False,
        target_allowlist=("192.168.1.1",),
    )
    assert not provided.covers(outside_target)
    assert "targets" in provided.gaps(outside_target)[0]

    # an empty provided allowlist means no narrowing: any target is covered
    unbounded = WorkerScope(
        name="unbounded",
        command_families=("recon", "shell"),
        phases=(Phase.RECON,),
        mutating=False,
    )
    assert unbounded.covers(outside_target)


def test_worker_error_hierarchy() -> None:
    assert issubclass(WorkerScopeError, WorkerError)
    assert issubclass(WorkerError, RuntimeError)
    for error in (
        EmptyWorkerScopeError,
        UnknownCommandFamilyError,
        ReadOnlyScopeError,
        TaskOutOfScopeError,
        FamilyOutOfScopeError,
        ReadOnlyViolationError,
        SerializationRequiredError,
    ):
        assert issubclass(error, WorkerScopeError)
    assert issubclass(DuplicateAssignmentError, WorkerError)


# ---------------------------------------------------------------------------
# worker construction and the assignment gate
# ---------------------------------------------------------------------------


def test_worker_requires_declared_scope_worker_id_and_confidence(tmp_path: Path) -> None:
    class BareWorker(SpecialistWorker):
        pass

    class NoIdWorker(SpecialistWorker):
        scope = WorkerScope(
            name="noid", command_families=("recon",), phases=(Phase.RECON,), mutating=False
        )

    class NoConfidenceWorker(SpecialistWorker):
        scope = WorkerScope(
            name="noconf", command_families=("recon",), phases=(Phase.RECON,), mutating=False
        )
        worker_id = "noconf"

    with pytest.raises(WorkerError, match="WorkerScope"):
        BareWorker(artifacts=ArtifactStore(tmp_path / "a"))
    with pytest.raises(WorkerError, match="worker_id"):
        NoIdWorker(artifacts=ArtifactStore(tmp_path / "b"))
    with pytest.raises(WorkerError, match="default_confidence"):
        NoConfidenceWorker(artifacts=ArtifactStore(tmp_path / "c"))


def test_assign_rejects_out_of_scope_task(tmp_path: Path) -> None:
    runner = RecordingRunner()
    worker = make_worker(ReconWorker, tmp_path, runner=runner)
    # The required scope needs the mutating exploit family: a read-only
    # recon worker cannot cover it, and the assignment is rejected loudly.
    with pytest.raises(TaskOutOfScopeError, match="exploit"):
        worker.assign(
            WorkerTask(
                task=Task(id="t-exploit"),
                command="hydra -l admin -P /tmp/pass 10.0.0.5",
                phase=Phase.EXPLOITATION,
                required_scope=WorkerScope(
                    name="required-t-exploit",
                    command_families=("shell", "exploit"),
                    phases=(Phase.EXPLOITATION,),
                    mutating=True,
                ),
            )
        )
    assert worker.assignments == ()
    assert runner.calls == []


def test_assign_rejects_duplicate_assignment(tmp_path: Path) -> None:
    worker = make_worker(ReconWorker, tmp_path)
    worker.assign(recon_assignment("t-1"))
    with pytest.raises(DuplicateAssignmentError, match="already has an assignment"):
        worker.assign(recon_assignment("t-1", command="echo second"))


def test_assign_rejects_nonserialized_task_for_submission_worker(tmp_path: Path) -> None:
    worker = make_worker(SubmissionWorker, tmp_path)
    with pytest.raises(SerializationRequiredError, match=SERIALIZED_CONFLICT_KEY):
        worker.assign(
            WorkerTask(
                task=Task(id="t-submit"),
                command="halctl submit --challenge-id c1 --flag FLAG{x}",
                phase=Phase.POST_EXPLOITATION,
                required_scope=WorkerScope(
                    name="required-submit",
                    command_families=("shell",),
                    phases=(Phase.POST_EXPLOITATION,),
                    mutating=True,
                ),
            )
        )
    assert worker.assignments == ()


def test_run_task_unassigned_task_rejected(tmp_path: Path) -> None:
    runner = RecordingRunner()
    worker = make_worker(ReconWorker, tmp_path, runner=runner)
    with pytest.raises(TaskOutOfScopeError, match="no assignment"):
        await_run(worker.run_task(Task(id="t-unknown")))
    assert runner.calls == []


def test_assignments_are_listed_in_deterministic_order(tmp_path: Path) -> None:
    worker = make_worker(ReconWorker, tmp_path)
    worker.assign(recon_assignment("t-b"))
    worker.assign(recon_assignment("t-a"))
    assert [work.task.id for work in worker.assignments] == ["t-a", "t-b"]


# ---------------------------------------------------------------------------
# run-time action enforcement: rejected loudly, nothing executes
# ---------------------------------------------------------------------------


def test_read_only_worker_rejects_mutating_family_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    worker = make_worker(ReconWorker, tmp_path, runner=runner)
    # The required scope declares only read-only families (recon, shell),
    # so the assignment passes — but the ACTUAL command classifies into the
    # mutating exploit family, and the worker rejects it before any run.
    worker.assign(
        WorkerTask(
            task=Task(id="t-hydra"),
            command="hydra -l admin -P /tmp/pass 10.0.0.5",
            phase=Phase.RECON,
            required_scope=WorkerScope(
                name="required-t-hydra",
                command_families=("recon", "shell"),
                phases=(Phase.RECON,),
                mutating=False,
            ),
        )
    )
    with pytest.raises(ReadOnlyViolationError, match="mutating"):
        await_run(worker.run_task(Task(id="t-hydra")))
    assert runner.calls == []  # nothing executed


def test_worker_rejects_family_outside_declared_scope(tmp_path: Path) -> None:
    runner = RecordingRunner()
    worker = make_worker(ArtifactAnalysisWorker, tmp_path, runner=runner)
    worker.assign(
        WorkerTask(
            task=Task(id="t-nmap"),
            command="nmap -sV 10.0.0.5",
            phase=Phase.ENUMERATION,
            required_scope=WorkerScope(
                name="required-t-nmap",
                command_families=("shell",),
                phases=(Phase.ENUMERATION,),
                mutating=False,
            ),
        )
    )
    with pytest.raises(FamilyOutOfScopeError, match="recon"):
        await_run(worker.run_task(Task(id="t-nmap")))
    assert runner.calls == []  # nothing executed


def test_run_task_rejects_duplicate_command_fingerprint(tmp_path: Path) -> None:
    runner = RecordingRunner()
    worker = make_worker(ReconWorker, tmp_path, runner=runner)
    worker.assign(recon_assignment("t-1", command="echo once"))
    outcome = await_run(worker.run_task(Task(id="t-1")))
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    with pytest.raises(DuplicateActionError, match="duplicate action"):
        await_run(worker.run_task(Task(id="t-1")))
    assert runner.calls == ["echo once"]  # executed exactly once


# ---------------------------------------------------------------------------
# bounded execution: evidence artifacts, structured outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_success_stores_artifact_and_finding(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    worker = make_worker(ReconWorker, tmp_path, runner=ShellRunner(), artifacts=artifacts)
    worker.assign(recon_assignment("t-1", command="echo probe", working_directory=str(tmp_path)))
    outcome = await worker.run_task(Task(id="t-1"))
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert outcome.error is None
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    # provenance: the task that produced it and the worker that ran it
    assert finding.task_id == "t-1"
    assert finding.source == "recon"
    # mandatory evidence: the stored output artifact
    assert len(finding.evidence_ids) == 1
    record = await artifacts.get(finding.evidence_ids[0])
    assert record.hash  # content-addressed artifact exists
    assert finding.confidence == 0.7
    assert "echo probe" in finding.summary


@pytest.mark.asyncio
async def test_run_task_failed_command_returns_structured_outcome(tmp_path: Path) -> None:
    worker = make_worker(ReconWorker, tmp_path, runner=ShellRunner())
    worker.assign(recon_assignment("t-1", command="false", working_directory=str(tmp_path)))
    outcome = await worker.run_task(Task(id="t-1"))
    assert outcome.status is WorkerRunStatus.FAILED
    assert "exit" in (outcome.error or "")
    assert "code 1" in (outcome.error or "")
    assert outcome.findings == ()  # a failed run carries no findings


def test_run_task_target_narrowing_rejects_outside_target(tmp_path: Path) -> None:
    class NarrowedReconWorker(ReconWorker):
        scope = WorkerScope(
            name="narrowed-recon",
            command_families=("recon", "shell"),
            phases=(Phase.RECON,),
            mutating=False,
            target_allowlist=("10.0.0.0/24",),
        )

    # The operator policy allows both targets; the worker's own narrowing
    # only covers 10.0.0.0/24. The required scope declares a covered
    # target, so the assignment passes — but the ACTUAL command addresses
    # an uncovered destination, and the worker's narrowed policy gate must
    # reject it loudly before anything runs.
    policy = ScopePolicy(target_allowlist=("10.0.0.5", "192.168.1.1"))
    runner = RecordingRunner()
    worker = make_worker(NarrowedReconWorker, tmp_path, runner=runner, policy=policy)
    worker.assign(
        WorkerTask(
            task=Task(id="t-scan"),
            command="curl http://192.168.1.1/",
            phase=Phase.RECON,
            required_scope=WorkerScope(
                name="required-t-scan",
                command_families=("recon", "shell"),
                phases=(Phase.RECON,),
                mutating=False,
                target_allowlist=("10.0.0.5",),
            ),
        )
    )
    with pytest.raises(AllowlistViolationError, match="192.168.1.1"):
        await_run(worker.run_task(Task(id="t-scan")))
    assert runner.calls == []  # nothing executed

    # A command inside the narrowing executes normally.
    worker.assign(
        WorkerTask(
            task=Task(id="t-scan2"),
            command="curl http://10.0.0.5/",
            phase=Phase.RECON,
            required_scope=WorkerScope(
                name="required-t-scan2",
                command_families=("recon", "shell"),
                phases=(Phase.RECON,),
                mutating=False,
                target_allowlist=("10.0.0.5",),
            ),
        )
    )
    outcome = await_run(worker.run_task(Task(id="t-scan2")))
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert runner.calls == ["curl http://10.0.0.5/"]

    # An assignment whose required targets are NOT covered by the worker's
    # narrowing is rejected at the assignment gate itself (fail closed,
    # before any execution).
    with pytest.raises(TaskOutOfScopeError, match="targets"):
        worker.assign(
            WorkerTask(
                task=Task(id="t-scan3"),
                command="curl http://192.168.1.1/",
                phase=Phase.RECON,
                required_scope=WorkerScope(
                    name="required-t-scan3",
                    command_families=("recon", "shell"),
                    phases=(Phase.RECON,),
                    mutating=False,
                    target_allowlist=("192.168.1.1",),
                ),
            )
        )
    assert runner.calls == ["curl http://10.0.0.5/"]  # nothing else executed


def test_submission_worker_runs_serialized_task(tmp_path: Path) -> None:
    runner = RecordingRunner()
    worker = make_worker(SubmissionWorker, tmp_path, runner=runner)
    task = serialized_task("t-submit")
    assert task.conflict_keys == (SERIALIZED_CONFLICT_KEY,)
    worker.assign(
        WorkerTask(
            task=task,
            command="halctl submit --challenge-id c1 --flag FLAG{x}",
            phase=Phase.POST_EXPLOITATION,
            required_scope=WorkerScope(
                name="required-submit",
                command_families=("shell",),
                phases=(Phase.POST_EXPLOITATION,),
                mutating=True,
            ),
        )
    )
    outcome = await_run(worker.run_task(task))
    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert len(outcome.findings) == 1
    assert outcome.findings[0].source == "flag-submission"
    assert outcome.findings[0].confidence == 1.0
    assert runner.calls == ["halctl submit --challenge-id c1 --flag FLAG{x}"]


# ---------------------------------------------------------------------------
# integration: a DAG of specialist workers through the scheduler
# ---------------------------------------------------------------------------


class DispatcherRunner:
    """Routes each DAG task to its assigned specialist worker.

    The composition the supervisor will build (a later PR): one
    TaskRunner that forwards every task to the worker that owns its
    assignment.
    """

    def __init__(self, workers: dict[str, SpecialistWorker]) -> None:
        self.workers = workers

    async def run_task(self, task: Task) -> TaskOutcome:
        return await self.workers[task.id].run_task(task)


def build_fleet(tmp_path: Path) -> tuple[DispatcherRunner, dict[str, Task]]:
    """A recon/analysis/submission fleet over deterministic echo commands."""
    runner = ShellRunner()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    recon = make_worker(ReconWorker, tmp_path, runner=runner, artifacts=artifacts)
    analyze = make_worker(ArtifactAnalysisWorker, tmp_path, runner=runner, artifacts=artifacts)
    submit = make_worker(SubmissionWorker, tmp_path, runner=runner, artifacts=artifacts)

    tasks = {
        "t-recon-a": Task(id="t-recon-a", hypothesis_id="hyp-1"),
        "t-recon-b": Task(id="t-recon-b"),
        "t-analyze": Task(id="t-analyze", depends_on=("t-recon-a",)),
        "t-submit": serialized_task("t-submit", depends_on=("t-analyze",)),
    }
    recon.assign(
        WorkerTask(
            task=tasks["t-recon-a"],
            command="echo recon-a",
            phase=Phase.RECON,
            required_scope=WorkerScope(
                name="required-recon-a",
                command_families=("recon", "shell"),
                phases=(Phase.RECON,),
                mutating=False,
            ),
            working_directory=str(tmp_path),
        )
    )
    recon.assign(
        WorkerTask(
            task=tasks["t-recon-b"],
            command="echo recon-b",
            phase=Phase.RECON,
            required_scope=WorkerScope(
                name="required-recon-b",
                command_families=("recon", "shell"),
                phases=(Phase.RECON,),
                mutating=False,
            ),
            working_directory=str(tmp_path),
        )
    )
    analyze.assign(
        WorkerTask(
            task=tasks["t-analyze"],
            command="echo analyze",
            phase=Phase.ENUMERATION,
            required_scope=WorkerScope(
                name="required-analyze",
                command_families=("shell",),
                phases=(Phase.ENUMERATION,),
                mutating=False,
            ),
            working_directory=str(tmp_path),
        )
    )
    submit.assign(
        WorkerTask(
            task=tasks["t-submit"],
            command="echo submit-ok",
            phase=Phase.POST_EXPLOITATION,
            required_scope=WorkerScope(
                name="required-submit",
                command_families=("shell",),
                phases=(Phase.POST_EXPLOITATION,),
                mutating=True,
            ),
            working_directory=str(tmp_path),
        )
    )
    dispatcher = DispatcherRunner(
        {"t-recon-a": recon, "t-recon-b": recon, "t-analyze": analyze, "t-submit": submit}
    )
    return dispatcher, tasks


async def _seed_graph_targets(graph: StateGraph, log: EventLog | None = None) -> None:
    """The hypothesis endpoint the schedule's edges reference."""
    at = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    await graph.create_entity("hyp-1", "hypothesis", {"confidence": 0.7}, at=at)
    if log is not None:
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                SCHEDULER_PRODUCER,
                GraphEntityCreated(
                    entity_id="hyp-1",
                    entity_type="hypothesis",
                    data={"confidence": 0.7},
                    at=at,
                ),
            )
        )


@pytest.mark.asyncio
async def test_scheduler_records_scope_violation_as_failed_run(tmp_path: Path) -> None:
    """A run-time scope rejection becomes a structured failed worker run."""
    runner = RecordingRunner()
    worker = make_worker(ArtifactAnalysisWorker, tmp_path, runner=runner)
    task = Task(id="t-bad")
    # Assignment passes (the required scope is fine); the command itself
    # classifies into recon, which the shell-only worker cannot run.
    worker.assign(
        WorkerTask(
            task=task,
            command="nmap -sV 10.0.0.5",
            phase=Phase.ENUMERATION,
            required_scope=WorkerScope(
                name="required-bad",
                command_families=("shell",),
                phases=(Phase.ENUMERATION,),
                mutating=False,
            ),
        )
    )
    dag = TaskDAG([task])
    async with StateGraph(":memory:") as graph:
        result = await Scheduler(
            dag=dag,
            runner=DispatcherRunner({"t-bad": worker}),  # type: ignore[arg-type]
            max_workers=1,
            run_id=RUN,
        ).run(graph)
    assert result.failed == 1
    assert result.succeeded == 0
    failed_run = result.worker_runs[0]
    assert failed_run.status is WorkerRunStatus.FAILED
    assert "FamilyOutOfScopeError" in (failed_run.error or "")
    assert runner.calls == []  # the command never executed


@pytest.mark.asyncio
async def test_worker_dag_through_scheduler_is_deterministic_and_replays(
    tmp_path: Path,
) -> None:
    """A specialist-worker DAG schedules deterministically and replays."""
    log = EventLog.for_run(tmp_path)
    dispatcher, tasks = build_fleet(tmp_path)
    dag = TaskDAG(list(tasks.values()))

    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_graph_targets(live, log=log)
        result = await Scheduler(
            dag=dag,
            runner=dispatcher,  # type: ignore[arg-type]
            max_workers=3,
            run_id=RUN,
            event_log=log,
        ).run(live)
        live_hash = await live.graph_hash()

    assert result.succeeded == 4
    assert result.failed == 0
    # deterministic dispatch order: independent recon tasks first (sorted
    # by id), then the dependent analysis, then the serialized submission.
    assert [run.task_id for run in result.worker_runs] == [
        "t-recon-a",
        "t-recon-b",
        "t-analyze",
        "t-submit",
    ]
    # every run carries exactly one evidence-referencing finding with the
    # right provenance
    expected_sources = {
        "t-recon-a": "recon",
        "t-recon-b": "recon",
        "t-analyze": "artifact-analysis",
        "t-submit": "flag-submission",
    }
    for run in result.worker_runs:
        assert run.status is WorkerRunStatus.SUCCEEDED
        assert len(run.findings) == 1
        finding = run.findings[0]
        assert finding.task_id == run.task_id
        assert finding.source == expected_sources[run.task_id]
        assert len(finding.evidence_ids) >= 1

    # deterministic: a second schedule of the same DAG produces identical
    # worker-run ids and identical findings (evidence ids are
    # content-addressed, summaries derive from command + exit + evidence)
    dispatcher2, _ = build_fleet(tmp_path)
    async with StateGraph(":memory:") as fresh:
        await _seed_graph_targets(fresh)
        result2 = await Scheduler(
            dag=dag,
            runner=dispatcher2,  # type: ignore[arg-type]
            max_workers=3,
            run_id=RUN,
        ).run(fresh)
    assert [run.id for run in result.worker_runs] == [run.id for run in result2.worker_runs]
    assert [run.task_id for run in result.worker_runs] == [
        run.task_id for run in result2.worker_runs
    ]
    assert [run.findings[0].evidence_ids for run in result.worker_runs] == [
        run.findings[0].evidence_ids for run in result2.worker_runs
    ]
    assert [run.findings[0].summary for run in result.worker_runs] == [
        run.findings[0].summary for run in result2.worker_runs
    ]

    # replay consistency: replaying the event log reconstructs the
    # identical graph hash (PR24 pattern)
    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


def await_run(coro):
    """Run one async worker call in a fresh event loop (sync tests)."""
    import asyncio

    return asyncio.run(coro)
