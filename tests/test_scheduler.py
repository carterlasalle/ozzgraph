"""Tests for the task DAG and bounded-parallel scheduler (PR24).

Covers DAG construction (valid, duplicate id, missing dependency, cycle,
self-dependency), deterministic ready order and topological order,
conflict-key mutual exclusion (two tasks sharing a key never run
concurrently — verified with an instrumented gate runner recording
execution intervals), dependency ordering, bounded parallelism (never
more than ``max_workers`` concurrent), deterministic scheduling order
(two schedules of the same DAG produce the same start sequence),
supervisor-only serialization (a task carrying the reserved
``serialized`` key never overlaps any other task), the structured
finding schema (provenance + mandatory evidence references), the
worker-run contract (attribution and failure errors), and graph/event
persistence with replay consistency (replaying the log reconstructs the
identical graph hash, following the PR20 executor pattern).

Every test uses its own in-memory SQLite graph (``":memory:"``); replay
tests use a file-backed live graph plus a fresh replay database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_UPDATED,
    Event,
    EventLog,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.replay import replay_graph
from ozzgraph.scheduler import (
    EDGE_TASK_IMPLEMENTS_PLANSTEP,
    EDGE_WORKER_RUN_EXPLORED_HYPOTHESIS,
    ENTITY_TASK,
    ENTITY_WORKER_RUN,
    MUTATION_CONFLICT_KEY,
    SCHEDULER_PRODUCER,
    SCHEDULER_RUN_COMPLETED,
    SCHEDULER_RUN_STARTED,
    SCHEDULER_TASK_COMPLETED,
    SCHEDULER_TASK_FAILED,
    SCHEDULER_TASK_STARTED,
    SERIALIZED_CONFLICT_KEY,
    DuplicateTaskError,
    Finding,
    MissingDependencyError,
    Scheduler,
    SchedulerError,
    Task,
    TaskCycleError,
    TaskDAG,
    TaskDAGError,
    TaskNotFoundError,
    TaskOutcome,
    WorkerRunStatus,
    serialized_task,
    worker_run_id,
)
from ozzgraph.state_graph import StateGraph

RUN = "run-1"


# ---------------------------------------------------------------------------
# instrumented runners
# ---------------------------------------------------------------------------


class GateRunner:
    """Records execution intervals and blocks each task on its own gate.

    Each task entry/exit advances a monotonic counter, so the recorded
    intervals are deterministic: overlapping intervals mean the tasks ran
    concurrently, and ``max_active`` is the peak concurrency. A task with a
    gate event in ``gates`` (keyed by task id) blocks until the test opens
    it, so tests can hold tasks open and observe exactly what is running.
    """

    def __init__(self, gates: dict[str, asyncio.Event] | None = None) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self.active: set[str] = set()
        self.max_active = 0
        self.intervals: dict[str, tuple[int, int]] = {}
        self.gates = gates if gates is not None else {}
        self._clock = 0
        self._entered = asyncio.Event()
        self._entered_count = 0

    async def run_task(self, task: Task) -> TaskOutcome:
        self._clock += 1
        start = self._clock
        self.started.append(task.id)
        self.active.add(task.id)
        self.max_active = max(self.max_active, len(self.active))
        self._entered_count += 1
        self._entered.set()
        gate = self.gates.get(task.id)
        if gate is not None:
            await gate.wait()
        self._clock += 1
        self.intervals[task.id] = (start, self._clock)
        self.active.remove(task.id)
        self.finished.append(task.id)
        return TaskOutcome(task_id=task.id, status=WorkerRunStatus.SUCCEEDED)

    async def wait_for_entries(self, count: int) -> None:
        """Block until ``count`` tasks have entered run_task."""
        while self._entered_count < count:
            self._entered.clear()
            await self._entered.wait()
        self._entered.clear()


def closed_gates(task_ids: tuple[str, ...]) -> dict[str, asyncio.Event]:
    """One closed gate per task id — every task blocks until opened."""
    return {task_id: asyncio.Event() for task_id in task_ids}


class InstantRunner:
    """Completes immediately with a scripted succeeded outcome."""

    def __init__(self, findings: bool = True) -> None:
        self.calls: list[str] = []
        self._findings = findings

    async def run_task(self, task: Task) -> TaskOutcome:
        self.calls.append(task.id)
        findings = ()
        if self._findings:
            findings = (
                Finding(
                    task_id=task.id,
                    source="probe",
                    evidence_ids=("ev-1",),
                    summary=f"scanned {task.id}",
                    confidence=0.8,
                ),
            )
        return TaskOutcome(task_id=task.id, status=WorkerRunStatus.SUCCEEDED, findings=findings)


class FailingRunner:
    """Fails scripted tasks with a structured error, succeeds the rest."""

    def __init__(self, fail: set[str]) -> None:
        self.fail = fail

    async def run_task(self, task: Task) -> TaskOutcome:
        if task.id in self.fail:
            return TaskOutcome(
                task_id=task.id,
                status=WorkerRunStatus.FAILED,
                error="exploit attempt timed out",
            )
        return TaskOutcome(task_id=task.id, status=WorkerRunStatus.SUCCEEDED)


class CrashingRunner:
    """Raises from run_task — the scheduler must convert it structurally."""

    async def run_task(self, task: Task) -> TaskOutcome:
        raise RuntimeError(f"boom in {task.id}")


def _scheduler(
    dag: TaskDAG,
    runner: object,
    *,
    max_workers: int = 4,
    run_id: str = RUN,
    event_log: EventLog | None = None,
) -> Scheduler:
    return Scheduler(
        dag=dag,
        runner=runner,  # type: ignore[arg-type]
        max_workers=max_workers,
        run_id=run_id,
        event_log=event_log,
    )


# ---------------------------------------------------------------------------
# DAG construction: valid, typed errors
# ---------------------------------------------------------------------------


def test_dag_valid_construction_preserves_declared_order() -> None:
    dag = TaskDAG(
        [
            Task(id="b", depends_on=("a",)),
            Task(id="a"),
            Task(id="c", conflict_keys=("recon",), plan_step_id="plan-1-step-1"),
        ]
    )
    assert [task.id for task in dag.tasks] == ["b", "a", "c"]
    assert len(dag) == 3
    assert dag.task("a").conflict_keys == ()


def test_dag_duplicate_task_id_raises() -> None:
    with pytest.raises(DuplicateTaskError, match="duplicate task id"):
        TaskDAG([Task(id="a"), Task(id="a")])


def test_dag_missing_dependency_raises() -> None:
    with pytest.raises(MissingDependencyError, match="unknown task"):
        TaskDAG([Task(id="a", depends_on=("ghost",))])


def test_dag_cycle_raises() -> None:
    with pytest.raises(TaskCycleError, match="cycle"):
        TaskDAG([Task(id="a", depends_on=("b",)), Task(id="b", depends_on=("a",))])


def test_dag_self_dependency_raises() -> None:
    with pytest.raises(TaskCycleError, match="cycle"):
        TaskDAG([Task(id="a", depends_on=("a",))])


def test_dag_task_lookup_unknown_id_raises() -> None:
    dag = TaskDAG([Task(id="a")])
    with pytest.raises(TaskNotFoundError):
        dag.task("missing")


def test_dag_errors_share_the_scheduler_hierarchy() -> None:
    assert issubclass(TaskDAGError, SchedulerError)
    for error in (DuplicateTaskError, MissingDependencyError, TaskCycleError):
        assert issubclass(error, TaskDAGError)


# ---------------------------------------------------------------------------
# deterministic ready order and topological order
# ---------------------------------------------------------------------------


def test_ready_order_only_dependency_complete_tasks_sorted_by_id() -> None:
    dag = TaskDAG(
        [
            Task(id="b", depends_on=("a",)),
            Task(id="a"),
            Task(id="c", depends_on=("a",)),
            Task(id="d"),
        ]
    )
    assert dag.ready_order(()) == ("a", "d")
    assert dag.ready_order({"a"}) == ("b", "c", "d")
    assert dag.ready_order({"a", "b"}) == ("c", "d")
    assert dag.ready_order({"a", "b", "c", "d"}) == ()


def test_topological_order_is_deterministic() -> None:
    dag = TaskDAG(
        [
            Task(id="c", depends_on=("a",)),
            Task(id="a"),
            Task(id="b", depends_on=("a",)),
        ]
    )
    first = dag.topological_order()
    second = TaskDAG(
        [
            Task(id="b", depends_on=("a",)),
            Task(id="a"),
            Task(id="c", depends_on=("a",)),
        ]
    ).topological_order()
    assert [task.id for task in first] == ["a", "b", "c"]
    assert [task.id for task in first] == [task.id for task in second]


# ---------------------------------------------------------------------------
# task schema: conflict-key validation and the serialized hook
# ---------------------------------------------------------------------------


def test_task_rejects_blank_and_duplicate_references() -> None:
    with pytest.raises(ValidationError):
        Task(id="a", conflict_keys=("",))
    with pytest.raises(ValidationError):
        Task(id="a", conflict_keys=("k", "k"))
    with pytest.raises(ValidationError):
        Task(id="a", depends_on=("",))
    with pytest.raises(ValidationError):
        Task(id="")


def test_serialized_key_must_stand_alone() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        Task(id="a", conflict_keys=(SERIALIZED_CONFLICT_KEY, "recon"))
    # The reserved key alone is valid.
    Task(id="a", conflict_keys=(SERIALIZED_CONFLICT_KEY,))


def test_mutation_key_must_stand_alone_on_mutating_tasks() -> None:
    # A mutating task carries exactly the reserved mutation key.
    task = Task(id="a", mutating=True, conflict_keys=(MUTATION_CONFLICT_KEY,))
    assert task.mutating is True
    assert task.conflict_keys == (MUTATION_CONFLICT_KEY,)
    # A mutating task without the key is rejected loudly.
    with pytest.raises(ValidationError, match="exactly the reserved"):
        Task(id="a", mutating=True)
    with pytest.raises(ValidationError, match="exactly the reserved"):
        Task(id="a", mutating=True, conflict_keys=("recon",))
    with pytest.raises(ValidationError, match="exactly the reserved"):
        Task(id="a", mutating=True, conflict_keys=(MUTATION_CONFLICT_KEY, "recon"))


def test_read_only_task_cannot_claim_mutation_key() -> None:
    with pytest.raises(ValidationError, match="read-only"):
        Task(id="a", conflict_keys=(MUTATION_CONFLICT_KEY,))
    # The default read-only task stays parallel-eligible.
    Task(id="a", conflict_keys=("recon",))


def test_serialized_task_hook_carries_the_reserved_key() -> None:
    task = serialized_task("flag-submit", plan_step_id="plan-1-step-1")
    assert task.conflict_keys == (SERIALIZED_CONFLICT_KEY,)
    assert task.plan_step_id == "plan-1-step-1"
    # A DAG of a serialized task plus a normal task is constructible.
    TaskDAG([task, Task(id="other")])


# ---------------------------------------------------------------------------
# findings and worker-run contracts
# ---------------------------------------------------------------------------


def test_finding_valid_with_evidence() -> None:
    finding = Finding(
        task_id="t-1",
        source="probe",
        evidence_ids=("ev-1", "art-2"),
        summary="port 22 open on target",
        confidence=0.9,
    )
    assert finding.evidence_ids == ("ev-1", "art-2")
    assert finding.confidence == 0.9


def test_finding_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one evidence"):
        Finding(task_id="t-1", source="probe", evidence_ids=(), summary="trust me")


def test_finding_rejects_blank_evidence_id() -> None:
    with pytest.raises(ValidationError):
        Finding(task_id="t-1", source="probe", evidence_ids=("  ",), summary="x")


def test_finding_rejects_extra_fields_and_bad_range() -> None:
    with pytest.raises(ValidationError):
        Finding(
            task_id="t-1",
            source="probe",
            evidence_ids=("ev-1",),
            summary="x",
            extra="forbidden",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        Finding(
            task_id="t-1",
            source="probe",
            evidence_ids=("ev-1",),
            summary="x",
            confidence=1.5,
        )
    with pytest.raises(ValidationError):
        Finding(task_id="t-1", source="probe", evidence_ids=("ev-1",), summary="")


def test_worker_run_id_is_stable_and_namespaced() -> None:
    assert worker_run_id("run-1", "t-1") == worker_run_id("run-1", "t-1")
    assert worker_run_id("run-1", "t-1") != worker_run_id("run-1", "t-2")
    assert worker_run_id("run-1", "t-1") != worker_run_id("run-2", "t-1")
    assert worker_run_id("run-1", "t-1").startswith("worker-run-")


def test_task_outcome_rejects_misattributed_findings() -> None:
    with pytest.raises(ValidationError, match="attributed"):
        TaskOutcome(
            task_id="t-1",
            status=WorkerRunStatus.SUCCEEDED,
            findings=(Finding(task_id="t-2", source="probe", evidence_ids=("ev-1",), summary="x"),),
        )


def test_task_outcome_rejects_invalid_status_and_error_shape() -> None:
    with pytest.raises(ValidationError, match="SUCCEEDED or FAILED"):
        TaskOutcome(task_id="t-1", status=WorkerRunStatus.RUNNING)
    with pytest.raises(ValidationError, match="structured error"):
        TaskOutcome(task_id="t-1", status=WorkerRunStatus.FAILED)
    with pytest.raises(ValidationError, match="cannot carry an error"):
        TaskOutcome(task_id="t-1", status=WorkerRunStatus.SUCCEEDED, error="nope")


# ---------------------------------------------------------------------------
# conflict-key mutual exclusion (never concurrent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflicting_tasks_never_run_concurrently() -> None:
    """Two tasks sharing a conflict key serialize; intervals never overlap."""
    dag = TaskDAG([Task(id="a", conflict_keys=("recon",)), Task(id="b", conflict_keys=("recon",))])
    runner = GateRunner(closed_gates(("a", "b")))
    async with StateGraph(":memory:") as graph:
        task = asyncio.create_task(_scheduler(dag, runner, max_workers=2).run(graph))
        await runner.wait_for_entries(1)
        assert runner.active == {"a"}  # b is excluded: it conflicts with a
        assert runner.started == ["a"]
        runner.gates["a"].set()
        await runner.wait_for_entries(2)
        # b started only after a finished.
        assert runner.finished == ["a"]
        assert runner.active == {"b"}
        runner.gates["b"].set()
        result = await task
    assert runner.max_active == 1
    assert runner.intervals["a"][1] < runner.intervals["b"][0]
    assert [run.task_id for run in result.worker_runs] == ["a", "b"]


@pytest.mark.asyncio
async def test_independent_tasks_run_concurrently() -> None:
    """Tasks with disjoint (empty) conflict keys overlap freely."""
    dag = TaskDAG([Task(id="a"), Task(id="b")])
    runner = GateRunner(closed_gates(("a", "b")))
    async with StateGraph(":memory:") as graph:
        task = asyncio.create_task(_scheduler(dag, runner, max_workers=2).run(graph))
        await runner.wait_for_entries(2)
        assert runner.active == {"a", "b"}
        runner.gates["a"].set()
        runner.gates["b"].set()
        result = await task
    assert runner.max_active == 2
    assert [run.task_id for run in result.worker_runs] == ["a", "b"]


@pytest.mark.asyncio
async def test_mutating_tasks_serialize_but_independent_tasks_overlap() -> None:
    """Mutation tasks serialize among themselves; hypothesis tasks stay parallel."""
    dag = TaskDAG(
        [
            Task(id="m-1", mutating=True, conflict_keys=(MUTATION_CONFLICT_KEY,)),
            Task(id="m-2", mutating=True, conflict_keys=(MUTATION_CONFLICT_KEY,)),
            Task(id="h-1", conflict_keys=("hyp-1",)),
        ]
    )
    runner = GateRunner(closed_gates(("m-1", "m-2", "h-1")))
    async with StateGraph(":memory:") as graph:
        task = asyncio.create_task(_scheduler(dag, runner, max_workers=3).run(graph))
        await runner.wait_for_entries(2)
        # Deterministic dispatch (sorted ids): h-1 and m-1 start together —
        # the hypothesis task overlaps the mutation task freely — while m-2
        # is excluded by the mutation conflict key (never two mutations).
        assert runner.active == {"h-1", "m-1"}
        assert runner.started == ["h-1", "m-1"]
        runner.gates["h-1"].set()
        runner.gates["m-1"].set()
        await runner.wait_for_entries(3)
        # m-2 started only after m-1 finished (serialized among mutations).
        assert runner.active == {"m-2"}
        assert "m-1" in runner.finished
        runner.gates["m-2"].set()
        result = await task
    assert runner.max_active == 2  # hypothesis + one mutation, never two mutations
    assert [run.task_id for run in result.worker_runs] == ["h-1", "m-1", "m-2"]


# ---------------------------------------------------------------------------
# dependency ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_runs_only_after_dependencies_complete() -> None:
    dag = TaskDAG([Task(id="a"), Task(id="b", depends_on=("a",)), Task(id="c", depends_on=("b",))])
    runner = GateRunner(closed_gates(("a", "b", "c")))
    async with StateGraph(":memory:") as graph:
        task = asyncio.create_task(_scheduler(dag, runner, max_workers=3).run(graph))
        await runner.wait_for_entries(1)
        assert runner.started == ["a"]
        assert runner.active == {"a"}  # b and c are not dependency-complete
        runner.gates["a"].set()
        await runner.wait_for_entries(2)
        assert runner.finished == ["a"]
        assert runner.active == {"b"}  # only b is ready now
        runner.gates["b"].set()
        await runner.wait_for_entries(3)
        assert runner.finished == ["a", "b"]
        assert runner.active == {"c"}
        runner.gates["c"].set()
        result = await task
    assert runner.max_active == 1
    assert runner.intervals["a"][1] < runner.intervals["b"][0]
    assert runner.intervals["b"][1] < runner.intervals["c"][0]
    assert [run.task_id for run in result.worker_runs] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# bounded parallelism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_more_than_max_workers_concurrent() -> None:
    """Six independent tasks under max_workers=2 never exceed 2 active."""
    dag = TaskDAG([Task(id=f"t-{i}") for i in range(6)])
    runner = GateRunner(closed_gates(tuple(f"t-{i}" for i in range(6))))
    async with StateGraph(":memory:") as graph:
        task = asyncio.create_task(_scheduler(dag, runner, max_workers=2).run(graph))
        await runner.wait_for_entries(2)
        assert runner.active == {"t-0", "t-1"}
        runner.gates["t-0"].set()
        runner.gates["t-1"].set()
        await runner.wait_for_entries(4)
        assert runner.active == {"t-2", "t-3"}
        runner.gates["t-2"].set()
        runner.gates["t-3"].set()
        await runner.wait_for_entries(6)
        assert runner.active == {"t-4", "t-5"}
        runner.gates["t-4"].set()
        runner.gates["t-5"].set()
        result = await task
    assert runner.max_active == 2  # the peak never exceeded max_workers
    assert result.succeeded == 6
    assert [run.task_id for run in result.worker_runs] == [f"t-{i}" for i in range(6)]


# ---------------------------------------------------------------------------
# supervisor-only serialization (AGENTS.md rule #7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serialized_task_never_runs_concurrently_with_anything() -> None:
    """A serialized task conflicts with a plain task in both directions."""
    for ids in (("a", "s"), ("s", "z")):
        plain, serialized = sorted(ids)
        dag = TaskDAG(
            [
                Task(id=plain),
                serialized_task(serialized),
            ]
        )
        runner = GateRunner(closed_gates((plain, serialized)))
        async with StateGraph(":memory:") as graph:
            task = asyncio.create_task(_scheduler(dag, runner, max_workers=2).run(graph))
            await runner.wait_for_entries(1)
            assert runner.active == {plain}  # the serialized task is excluded
            runner.gates[plain].set()
            await runner.wait_for_entries(2)
            assert runner.finished == [plain]
            assert runner.active == {serialized}
            runner.gates[serialized].set()
            result = await task
        assert runner.max_active == 1
        assert runner.intervals[plain][1] < runner.intervals[serialized][0]
        assert [run.task_id for run in result.worker_runs] == [plain, serialized]


@pytest.mark.asyncio
async def test_two_serialized_tasks_never_run_concurrently() -> None:
    dag = TaskDAG([serialized_task("flag-a"), serialized_task("flag-b")])
    runner = GateRunner(closed_gates(("flag-a", "flag-b")))
    async with StateGraph(":memory:") as graph:
        task = asyncio.create_task(_scheduler(dag, runner, max_workers=2).run(graph))
        await runner.wait_for_entries(1)
        assert runner.active == {"flag-a"}
        runner.gates["flag-a"].set()
        await runner.wait_for_entries(2)
        assert runner.active == {"flag-b"}
        runner.gates["flag-b"].set()
        await task
    assert runner.max_active == 1


# ---------------------------------------------------------------------------
# deterministic scheduling order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedules_are_deterministic_and_reproducible() -> None:
    """Two schedules of the same DAG produce the identical start sequence."""
    dag = TaskDAG(
        [
            Task(id="d", depends_on=("a",)),
            Task(id="a", conflict_keys=("k1",)),
            Task(id="c", depends_on=("b",), conflict_keys=("k1",)),
            Task(id="b", conflict_keys=("k2",)),
            Task(id="e"),
        ]
    )
    sequences: list[list[str]] = []
    worker_run_ids: list[list[str]] = []
    for _ in range(2):
        runner = InstantRunner()
        async with StateGraph(":memory:") as graph:
            result = await _scheduler(dag, runner).run(graph)
        sequences.append(runner.calls)
        worker_run_ids.append([run.id for run in result.worker_runs])
    assert sequences[0] == sequences[1]
    assert worker_run_ids[0] == worker_run_ids[1]


# ---------------------------------------------------------------------------
# failure paths: structured failed runs, schedule continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_outcome_is_recorded_and_schedule_continues(
    tmp_path: Path,
) -> None:
    log = EventLog.for_run(tmp_path)
    dag = TaskDAG([Task(id="a"), Task(id="b", depends_on=("a",))])
    async with StateGraph(":memory:") as graph:
        result = await _scheduler(dag, FailingRunner(fail={"a"}), event_log=log).run(graph)
    assert result.failed == 1
    assert result.succeeded == 1
    failed_run = result.worker_runs[0]
    assert failed_run.status is WorkerRunStatus.FAILED
    assert failed_run.error == "exploit attempt timed out"
    assert failed_run.findings == ()
    # The dependent still ran: dependencies are satisfied on completion,
    # success or failure (the reducer, PR26, interprets failed findings).
    assert [run.task_id for run in result.worker_runs] == ["a", "b"]
    assert [run.status for run in result.worker_runs] == [
        WorkerRunStatus.FAILED,
        WorkerRunStatus.SUCCEEDED,
    ]
    events = [line.event_type for line in _read_log(log)]
    assert SCHEDULER_TASK_FAILED in events
    assert SCHEDULER_TASK_COMPLETED in events


@pytest.mark.asyncio
async def test_runner_crash_becomes_a_structured_failed_run() -> None:
    """A raising runner fails loudly as a failed worker run, never silently."""
    dag = TaskDAG([Task(id="a"), Task(id="b")])
    async with StateGraph(":memory:") as graph:
        result = await _scheduler(dag, CrashingRunner(), max_workers=2).run(graph)
    assert result.failed == 2
    assert all(run.status is WorkerRunStatus.FAILED for run in result.worker_runs)
    assert "RuntimeError: boom in a" in (result.worker_runs[0].error or "")


def test_scheduler_rejects_invalid_max_workers() -> None:
    with pytest.raises(ValueError, match="max_workers"):
        Scheduler(
            TaskDAG([Task(id="a")]),
            runner=InstantRunner(),  # type: ignore[arg-type]
            max_workers=0,
            run_id=RUN,
        )


# ---------------------------------------------------------------------------
# graph persistence and replay consistency
# ---------------------------------------------------------------------------


async def _seed_graph_targets(
    graph: StateGraph,
    log: EventLog | None = None,
    run_id: str = RUN,
) -> None:
    """The edge endpoints the schedule references (plan step, hypothesis).

    When a log is given, the seeding is mirrored as ``graph.*`` events (the
    same same-timestamp pattern the scheduler uses) so replay reconstructs
    the endpoints before the edges that reference them.
    """
    at = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    await graph.create_entity("plan-1-step-1", "plan_step", {"objective": "probe svc"}, at=at)
    at2 = datetime(2026, 8, 7, 12, 0, 1, tzinfo=UTC)
    await graph.create_entity("hyp-1", "hypothesis", {"confidence": 0.7}, at=at2)
    if log is not None:
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                run_id,
                SCHEDULER_PRODUCER,
                GraphEntityCreated(
                    entity_id="plan-1-step-1",
                    entity_type="plan_step",
                    data={"objective": "probe svc"},
                    at=at,
                ),
            )
        )
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                run_id,
                SCHEDULER_PRODUCER,
                GraphEntityCreated(
                    entity_id="hyp-1",
                    entity_type="hypothesis",
                    data={"confidence": 0.7},
                    at=at2,
                ),
            )
        )


@pytest.mark.asyncio
async def test_schedule_persists_entities_edges_and_replays_identically(
    tmp_path: Path,
) -> None:
    log = EventLog.for_run(tmp_path)
    dag = TaskDAG(
        [
            Task(id="t-a", plan_step_id="plan-1-step-1", hypothesis_id="hyp-1"),
            Task(id="t-b", depends_on=("t-a",), hypothesis_id="hyp-1"),
            Task(id="t-c", conflict_keys=("recon",)),
        ]
    )
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_graph_targets(live, log=log)
        result = await _scheduler(dag, InstantRunner(), max_workers=2, event_log=log).run(live)
        live_hash = await live.graph_hash()

        # task entities carry the DAG definition
        tasks = {record.id: record for record in await live.list_entities(ENTITY_TASK)}
        assert set(tasks) == {"t-a", "t-b", "t-c"}
        assert tasks["t-a"].data["plan_step_id"] == "plan-1-step-1"
        assert tasks["t-a"].data["depends_on"] == []
        assert tasks["t-b"].data["depends_on"] == ["t-a"]
        assert tasks["t-c"].data["conflict_keys"] == ["recon"]

        # worker_run entities record status, findings, and error
        runs = {record.id: record for record in await live.list_entities(ENTITY_WORKER_RUN)}
        assert set(runs) == {run.id for run in result.worker_runs}
        for run in runs.values():
            assert run.data["status"] == "succeeded"
            assert run.data["finished_at"] is not None
            findings = run.data["findings"]
            assert isinstance(findings, list)
            assert len(findings) == 1
            record = findings[0]
            assert isinstance(record, dict)
            assert record["evidence_ids"] == ["ev-1"]
            assert record["task_id"] == run.data["task_id"]

        # TASK IMPLEMENTS PLANSTEP edge (plan step endpoint must exist)
        implements = await live.neighbors("plan-1-step-1")
        assert [edge.src_id for edge in implements.incoming] == ["t-a"]
        assert {edge.type for edge in implements.incoming} == {EDGE_TASK_IMPLEMENTS_PLANSTEP}

        # WORKER_RUN EXPLORED HYPOTHESIS edges for hypothesis-exploring tasks
        explored = await live.neighbors("hyp-1")
        assert {edge.type for edge in explored.incoming} == {EDGE_WORKER_RUN_EXPLORED_HYPOTHESIS}
        assert len(explored.incoming) == 2  # t-a and t-b explored hyp-1

    # Replaying the event log reconstructs the identical graph hash.
    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


@pytest.mark.asyncio
async def test_scheduler_emits_graph_and_run_events(tmp_path: Path) -> None:
    log = EventLog.for_run(tmp_path)
    dag = TaskDAG([Task(id="t-a", plan_step_id="plan-1-step-1")])
    async with StateGraph(":memory:") as graph:
        await _seed_graph_targets(graph)
        await _scheduler(dag, InstantRunner(), event_log=log).run(graph)
    events = _read_log(log)
    graph_events = [event for event in events if event.event_type.startswith("graph.")]
    # 1 task entity + 1 implements edge + 1 worker_run created + 1 updated
    assert len(graph_events) == 4
    assert {event.event_type for event in graph_events} == {
        GRAPH_ENTITY_CREATED,
        GRAPH_EDGE_CREATED,
        GRAPH_ENTITY_UPDATED,
    }
    run_events = [event.event_type for event in events]
    assert run_events.count(SCHEDULER_RUN_STARTED) == 1
    assert run_events.count(SCHEDULER_TASK_STARTED) == 1
    assert run_events.count(SCHEDULER_TASK_COMPLETED) == 1
    assert run_events.count(SCHEDULER_RUN_COMPLETED) == 1
    assert all(event.producer == SCHEDULER_PRODUCER for event in events)
    started = next(event for event in events if event.event_type == SCHEDULER_TASK_STARTED)
    assert started.task_id == "t-a"
    assert started.worker_id == worker_run_id(RUN, "t-a")


@pytest.mark.asyncio
async def test_task_persistence_is_idempotent(tmp_path: Path) -> None:
    """A pre-persisted DAG (same task entities and edges) is not re-created."""
    dag = TaskDAG([Task(id="t-a", plan_step_id="plan-1-step-1")])
    async with StateGraph(":memory:") as graph:
        await _seed_graph_targets(graph)
        # Simulate an earlier schedule of the same DAG: entity + edge exist.
        await graph.create_entity("t-a", ENTITY_TASK, {"depends_on": []})
        await graph.create_edge(
            "t-a-implements-plan-1-step-1",
            EDGE_TASK_IMPLEMENTS_PLANSTEP,
            "t-a",
            "plan-1-step-1",
        )
        result = await _scheduler(dag, InstantRunner()).run(graph)
        assert result.succeeded == 1
        # Still exactly one task entity and one implements edge.
        assert len(await graph.list_entities(ENTITY_TASK)) == 1
        assert len((await graph.neighbors("plan-1-step-1")).incoming) == 1


def _read_log(log: EventLog) -> list[Event]:
    """Parse the JSONL log back into structured events."""
    events: list[Event] = []
    with log.path.open(encoding="utf-8") as handle:
        for line in handle:
            events.append(Event.model_validate_json(line))
    return events
