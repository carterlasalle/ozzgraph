"""Task DAG and bounded-parallel scheduler for OzzGraph (PR24).

Implements the first slice of Phase 9 "Workers" (docs/IMPLEMENTATION_PLAN.md,
PR step 24; docs/ARCHITECTURE.md, "Scheduler"): the task DAG, the scheduler,
conflict keys, and the structured-findings / worker-run contracts. The
reducer (PR step 26) and specialist-worker scoping (PR step 25) are separate
later PRs and are NOT implemented here — findings stay embedded in their
``worker_run`` records until the reducer promotes them into graph evidence.

Design rules:

- Explicit dependencies and conflict keys (AGENTS.md rule #7): every
  :class:`Task` carries explicit ``depends_on`` references and a conflict-key
  set. Tasks with overlapping conflict keys are mutually exclusive and may
  never run concurrently; tasks with no keys conflict with nothing and run
  concurrently. Construction validates the whole DAG and fails loudly
  (:class:`TaskDAGError` hierarchy) on duplicate ids, missing dependencies,
  and cycles (AGENTS.md rule #9).

- Deterministic ready order: :meth:`TaskDAG.ready_order` returns the
  dependency-complete tasks sorted by stable id, and the scheduler dispatches
  in exactly that order, so the same DAG always yields the same start
  sequence. Schedules are reproducible: no randomness, no wall-clock ordering
  decisions.

- Bounded parallelism: :meth:`Scheduler.run` runs at most ``max_workers``
  tasks concurrently. Only tasks that are (a) dependency-complete and (b)
  non-conflicting with every currently running task are started; conflicting
  tasks serialize, independent tasks run concurrently. ``max_workers`` is the
  existing config knob (``OZZGRAPH_MAX_WORKERS``, default 4) — nothing new is
  wired into config.

- Supervisor-only serialization (AGENTS.md rule #7): flag submission and paid
  hints are ALWAYS serialized. A task carrying the reserved
  :data:`SERIALIZED_CONFLICT_KEY` conflicts with EVERY other task (including
  other serialized tasks); :func:`serialized_task` is the dedicated hook the
  supervisor uses. The gate is deterministic and fail-closed: a serialized
  task never starts while anything else runs.

- Mutation serialization (AGENTS.md rule #7, V07): a task whose action
  mutates state (``mutating=True``) must carry exactly the reserved
  :data:`MUTATION_CONFLICT_KEY`, so mutation/strategy tasks serialize among
  themselves while independent hypothesis tasks stay parallel through their
  own conflict keys. The reserved key stands alone (mirroring the serialized
  key) and is rejected on a read-only task — a task that mutates state can
  never hide that fact, and a task that does not mutate can never claim the
  key.

- Structured findings (AGENTS.md rule #3): a model claim is a hypothesis,
  never authoritative state. A :class:`Finding` is a typed record with
  provenance (task id, source) that MUST carry at least one evidence/artifact
  reference — a finding without evidence is free-form model prose and is
  rejected loudly at validation time. Each scheduled task produces one
  :class:`WorkerRun` with a stable id (``worker-run-<fingerprint>``, derived
  from the run id and task id), a status, and its findings.

- Graph persistence (AGENTS.md rule #1): the scheduler persists ``task`` and
  ``worker_run`` entities, ``TASK IMPLEMENTS PLANSTEP`` edges (when a task
  implements a plan step) and ``WORKER_RUN EXPLORED HYPOTHESIS`` edges (when
  a task explores a hypothesis), mirroring every mutation to the append-only
  event log as a ``graph.*`` event with the same timestamp (the PR20 executor
  pattern), so replay reconstructs the identical graph hash.

- Small kernel (AGENTS.md rule #10): the scheduler only schedules; the runner
  is injected and nothing is wired into the supervisor here. The scheduler is
  a component plus its contracts, delivered standalone (PR step 24).

V07 (docs/CHANGES_v2.md milestone 7): specialists. Independent hypotheses
parallelize and global strategy serializes through the SAME two mechanisms
that already exist here — per-hypothesis conflict keys and the reserved
:data:`SERIALIZED_CONFLICT_KEY`. :func:`hypothesis_task` is the dedicated
hook: a task testing one hypothesis carries the hypothesis id AS its
conflict key, so two tasks exploring the SAME hypothesis are mutually
exclusive (never concurrent) while tasks exploring DIFFERENT hypotheses
carry disjoint keys and run concurrently under ``max_workers`` — the
AGENTS.md rule #7 partition (parallelize evidence gathering, not mutable
exploit chains). Global-strategy tasks stay supervisor-serialized through
:func:`serialized_task`: the reserved key conflicts with every other task,
including hypothesis tasks. The structured conclusion of a specialist run
travels through :class:`Finding` as an optional ``verdict`` (``confirmed``
/ ``refuted`` / ``inconclusive``) plus an ``impact`` payload (CWE /
assets / confidence), so the reducer can merge conclusions as structured
verdicts into graph facts unchanged (additive optional fields; every
existing finding shape stays valid).
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_UPDATED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    GraphEntityUpdated,
    graph_event,
)
from ozzgraph.state_graph import StateGraph

#: Producer name on every scheduler event.
SCHEDULER_PRODUCER = "scheduler"

#: Run-log event emitted when a schedule starts.
SCHEDULER_RUN_STARTED = "scheduler.run_started"
#: Run-log event emitted when a schedule finishes (successfully or not).
SCHEDULER_RUN_COMPLETED = "scheduler.run_completed"
#: Run-log event emitted when one task is dispatched (recorded before the
#: runner executes it — the executor's "record the attempt before
#: execution" boundary).
SCHEDULER_TASK_STARTED = "scheduler.task_started"
#: Run-log event emitted when one task finishes with a succeeded outcome.
SCHEDULER_TASK_COMPLETED = "scheduler.task_completed"
#: Run-log event emitted when one task finishes with a failed outcome.
SCHEDULER_TASK_FAILED = "scheduler.task_failed"

#: Entity types the scheduler writes (docs/DATA_STRATEGY.md, lowercase by
#: convention). Task ids are caller-supplied stable ids and are used as the
#: entity ids directly.
ENTITY_TASK = "task"
ENTITY_WORKER_RUN = "worker_run"

#: Edge type linking a task to the plan step it implements
#: (docs/DATA_STRATEGY.md, uppercase by convention).
EDGE_TASK_IMPLEMENTS_PLANSTEP = "TASK IMPLEMENTS PLANSTEP"
#: Edge type linking a worker run to the hypothesis it explored.
EDGE_WORKER_RUN_EXPLORED_HYPOTHESIS = "WORKER_RUN EXPLORED HYPOTHESIS"

#: Reserved conflict key marking a task as supervisor-serialized (AGENTS.md
#: rule #7: flag submission and paid hints are always serialized). A task
#: carrying this key conflicts with EVERY other task — serialized tasks never
#: run concurrently with anything, and a serialized key must be the task's
#: only conflict key.
SERIALIZED_CONFLICT_KEY = "serialized"

#: Reserved conflict key marking a task as state-mutating (AGENTS.md rule
#: #7, V07: parallelize evidence gathering, not mutable exploit chains). A
#: mutating task (``mutating=True``) must carry exactly this key, so
#: mutation/strategy tasks serialize among themselves while independent
#: hypothesis tasks stay parallel through their own conflict keys. Unlike
#: :data:`SERIALIZED_CONFLICT_KEY` it does NOT conflict with unrelated
#: tasks — only with other mutating tasks.
MUTATION_CONFLICT_KEY = "mutation"


class SchedulerError(RuntimeError):
    """Base error for the scheduler layer (AGENTS.md rule #9)."""


class TaskDAGError(SchedulerError):
    """Base error for task-DAG construction failures."""


class DuplicateTaskError(TaskDAGError):
    """Two tasks in the DAG share one id."""


class MissingDependencyError(TaskDAGError):
    """A task depends on a task that is not in the DAG."""


class TaskCycleError(TaskDAGError):
    """The dependency graph contains a cycle (including self-dependencies)."""


class TaskNotFoundError(TaskDAGError):
    """A task id is not in the DAG."""


class Finding(BaseModel):
    """One structured worker finding with provenance.

    AGENTS.md rule #3: a model claim is a hypothesis, never authoritative
    state. A finding is a typed record with provenance — the task that
    produced it and the source it came from — that MUST carry at least one
    evidence/artifact reference. A finding without evidence is free-form
    model prose and is rejected loudly at validation time.

    Attributes:
        task_id: The task that produced the finding (provenance).
        source: Where the finding came from (e.g. the worker or a
            parser/tool that produced it).
        evidence_ids: Evidence entity ids and/or artifact ids the finding
            references; must be non-empty.
        summary: Bounded prose summary of the finding — never
            authoritative state by itself.
        confidence: The worker's confidence in [0.0, 1.0]; defaults to
            0.0 (weak).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    summary: str = Field(min_length=1, max_length=512)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("evidence_ids")
    @classmethod
    def _evidence_required(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject empty or blank evidence references (rule #3)."""
        for item in value:
            if not item or not item.strip():
                raise ValueError("evidence ids must be non-empty strings")
        if not value:
            raise ValueError(
                "a finding must reference at least one evidence/artifact id; "
                "a finding without evidence is model prose, not a finding"
            )
        return value


class WorkerRunStatus(str, Enum):
    """Lifecycle status of one scheduled task execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkerRun(BaseModel):
    """One scheduled task execution, with its structured findings.

    Attributes:
        id: Stable id ``worker-run-<fingerprint>``, deterministic per
            (run id, task id) via :func:`worker_run_id`.
        task_id: The DAG task this run executes.
        status: Lifecycle status; ``running`` until the run finishes.
        started_at: UTC dispatch time.
        finished_at: UTC completion time; ``None`` while running.
        findings: Structured findings the task produced; a failed run
            carries none (its ``error`` carries the failure).
        error: Structured failure text for a failed run; ``None`` on
            success.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: WorkerRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    findings: tuple[Finding, ...] = ()
    error: str | None = None


class Task(BaseModel):
    """One node of a task DAG.

    Attributes:
        id: Caller-supplied stable task id; used as the ``task`` entity
            id in the graph.
        depends_on: Explicit dependency references — task ids that must
            complete before this task may start.
        mutating: True when the task's action mutates state (an exploit
            chain, a strategy change); a mutating task must carry
            exactly the reserved :data:`MUTATION_CONFLICT_KEY`, so
            mutation/strategy tasks serialize among themselves
            (AGENTS.md rule #7). Read-only evidence gathering stays
            False and parallelizes through its own conflict keys.
        conflict_keys: Explicit conflict-key set. Tasks with overlapping
            keys are mutually exclusive and may never run concurrently;
            the reserved :data:`SERIALIZED_CONFLICT_KEY` conflicts with
            every task and :data:`MUTATION_CONFLICT_KEY` with every
            other mutating task.
        plan_step_id: The plan step this task implements, when the task
            serves a plan step (``TASK IMPLEMENTS PLANSTEP`` edge).
        hypothesis_id: The hypothesis this task explores, when it tests
            one (``WORKER_RUN EXPLORED HYPOTHESIS`` edge).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    mutating: bool = False
    conflict_keys: tuple[str, ...] = ()
    plan_step_id: str | None = None
    hypothesis_id: str | None = None

    @field_validator("depends_on", "conflict_keys")
    @classmethod
    def _references_nonempty_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank references and duplicates within one field."""
        for item in value:
            if not item or not item.strip():
                raise ValueError("dependencies and conflict keys must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("dependencies and conflict keys must be unique")
        return value

    @field_validator("conflict_keys")
    @classmethod
    def _serialized_key_stands_alone(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """The reserved serialized key must be a task's only conflict key.

        A serialized task already conflicts with every other task, so any
        additional key would be meaningless; reject the ambiguity loudly
        instead of silently dropping it.
        """
        if SERIALIZED_CONFLICT_KEY in value and len(value) != 1:
            raise ValueError(
                f"conflict key {SERIALIZED_CONFLICT_KEY!r} is reserved for "
                "supervisor-serialized tasks and must be the only conflict key"
            )
        return value

    @model_validator(mode="after")
    def _mutation_conflict_contract(self) -> Task:
        """A mutating task carries exactly the mutation key, and only it does.

        Mirrors the serialized-key semantics (AGENTS.md rule #7): a task
        that mutates state declares it loudly with
        :data:`MUTATION_CONFLICT_KEY` as its ONLY conflict key, so
        mutation/strategy tasks serialize among themselves while
        independent hypothesis tasks stay parallel. A read-only task
        claiming the reserved key is a contradiction and is rejected.
        """
        if self.mutating and self.conflict_keys != (MUTATION_CONFLICT_KEY,):
            raise ValueError(
                f"mutating task {self.id!r} must carry exactly the reserved "
                f"{MUTATION_CONFLICT_KEY!r} conflict key, got {self.conflict_keys}"
            )
        if not self.mutating and MUTATION_CONFLICT_KEY in self.conflict_keys:
            raise ValueError(
                f"conflict key {MUTATION_CONFLICT_KEY!r} is reserved for mutating "
                f"tasks; task {self.id!r} is read-only and cannot claim it"
            )
        return self


class TaskOutcome(BaseModel):
    """The typed result of one task run (the runner's contract).

    A runner returns exactly one of these per task: a succeeded outcome with
    its findings, or a failed outcome with a structured error. Findings must
    be attributed to the task that produced them — a finding whose
    ``task_id`` differs is a scope violation and is rejected loudly
    (AGENTS.md data invariant: a worker cannot mutate state outside its
    declared task scope).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    status: WorkerRunStatus
    findings: tuple[Finding, ...] = ()
    error: str | None = None

    @field_validator("status")
    @classmethod
    def _terminal_status(cls, value: WorkerRunStatus) -> WorkerRunStatus:
        """A task outcome is a verdict: succeeded or failed, never pending."""
        if value not in (WorkerRunStatus.SUCCEEDED, WorkerRunStatus.FAILED):
            raise ValueError(f"task outcome status must be SUCCEEDED or FAILED, got {value!r}")
        return value

    @field_validator("findings")
    @classmethod
    def _findings_attributed(
        cls, value: tuple[Finding, ...], info: ValidationInfo
    ) -> tuple[Finding, ...]:
        """Reject findings attributed to a different task (scope rule)."""
        task_id = info.data.get("task_id")
        if task_id is not None:
            for finding in value:
                if finding.task_id != task_id:
                    raise ValueError(
                        f"finding is attributed to task {finding.task_id!r} "
                        f"but the outcome is for task {task_id!r}"
                    )
        return value

    @model_validator(mode="after")
    def _failure_carries_error(self) -> TaskOutcome:
        """A failed outcome must carry a structured error (rule #9)."""
        if self.status is WorkerRunStatus.FAILED and not self.error:
            raise ValueError("a failed outcome must carry a structured error")
        if self.status is WorkerRunStatus.SUCCEEDED and self.error is not None:
            raise ValueError("a succeeded outcome cannot carry an error")
        return self


class SchedulerResult(BaseModel):
    """The typed result of one schedule: every worker run, in start order.

    Attributes:
        run_id: The run the schedule served.
        worker_runs: Every worker run in deterministic dispatch (start)
            order — the schedule's execution record.
        succeeded: Count of succeeded runs.
        failed: Count of failed runs.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    worker_runs: tuple[WorkerRun, ...] = ()
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)


class TaskRunner(Protocol):
    """The injected runner contract: one typed outcome per task.

    A runner executes one DAG task and returns exactly one
    :class:`TaskOutcome`. Runners are injected by the supervisor (not wired
    here); tests inject instrumented runners to observe concurrency. A runner
    that raises is converted into a structured failed outcome by the
    scheduler (fail loudly, never silently, never retried).
    """

    async def run_task(self, task: Task) -> TaskOutcome: ...


def worker_run_id(run_id: str, task_id: str) -> str:
    """Stable worker-run id: ``worker-run-<sha256(run_id:task_id)>``.

    Deterministic per (run id, task id), so replaying a run's events always
    reproduces the same worker-run ids. A DAG is scheduled once per run id;
    scheduling the same (run id, task id) twice fails loudly on the
    duplicate entity instead of silently rewriting history.
    """
    digest = hashlib.sha256(f"{run_id}:{task_id}".encode()).hexdigest()
    return f"worker-run-{digest}"


def serialized_task(
    task_id: str,
    *,
    depends_on: Sequence[str] = (),
    plan_step_id: str | None = None,
    hypothesis_id: str | None = None,
) -> Task:
    """A supervisor-serialized task (AGENTS.md rule #7).

    The dedicated hook for flag submission and paid hints: the task carries
    :data:`SERIALIZED_CONFLICT_KEY`, so it conflicts with every other task
    and never runs concurrently with anything. The gate is deterministic and
    fail-closed — a serialized task only starts when nothing else is running.
    """
    return Task(
        id=task_id,
        depends_on=tuple(depends_on),
        conflict_keys=(SERIALIZED_CONFLICT_KEY,),
        plan_step_id=plan_step_id,
        hypothesis_id=hypothesis_id,
    )


class TaskDAG:
    """A DAG of work tasks with explicit dependencies and conflict keys.

    Construction validates the whole DAG and fails loudly (AGENTS.md rule
    #9): duplicate task ids raise :class:`DuplicateTaskError`, a dependency
    on an unknown task raises :class:`MissingDependencyError`, and a cycle
    (including a self-dependency) raises :class:`TaskCycleError`. Ready-task
    selection and topological order are deterministic — sorted by stable id.
    """

    def __init__(self, tasks: Sequence[Task]) -> None:
        self._tasks = tuple(tasks)
        self._by_id: dict[str, Task] = {}
        for task in self._tasks:
            if task.id in self._by_id:
                raise DuplicateTaskError(f"duplicate task id {task.id!r}")
            self._by_id[task.id] = task
        for task in self._tasks:
            for dep in task.depends_on:
                if dep not in self._by_id:
                    raise MissingDependencyError(
                        f"task {task.id!r} depends on unknown task {dep!r}"
                    )
        self._kahn()  # cycle check; raises TaskCycleError when cyclic

    @property
    def tasks(self) -> tuple[Task, ...]:
        """The DAG's tasks in declared order."""
        return self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def task(self, task_id: str) -> Task:
        """Look up one task by id.

        Raises:
            TaskNotFoundError: If ``task_id`` is not in the DAG.
        """
        try:
            return self._by_id[task_id]
        except KeyError:
            raise TaskNotFoundError(f"task {task_id!r} is not in the DAG") from None

    def ready_order(self, completed: Collection[str]) -> tuple[str, ...]:
        """Dependency-complete task ids, sorted by stable id.

        A task is ready when it is not in ``completed`` and every id in its
        ``depends_on`` is in ``completed``. The result is sorted by id, so
        the scheduler dispatches in a deterministic order regardless of
        insertion order.
        """
        done = frozenset(completed)
        return tuple(
            sorted(
                task.id
                for task in self._tasks
                if task.id not in done and set(task.depends_on) <= done
            )
        )

    def topological_order(self) -> tuple[Task, ...]:
        """The full DAG in deterministic topological order (by id).

        Kahn's algorithm with a min-heap of zero-indegree tasks, so the
        order depends only on the DAG, never on insertion order.
        """
        return tuple(self._by_id[task_id] for task_id in self._kahn())

    def _kahn(self) -> list[str]:
        """Kahn's algorithm: deterministic topological order, or a cycle error.

        Raises:
            TaskCycleError: If the dependency graph is cyclic.
        """
        indegree = {task_id: 0 for task_id in self._by_id}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in self._by_id}
        for task in self._tasks:
            for dep in task.depends_on:
                indegree[task.id] += 1
                dependents[dep].append(task.id)
        heap = [task_id for task_id, degree in indegree.items() if degree == 0]
        heapq.heapify(heap)
        ordered: list[str] = []
        while heap:
            task_id = heapq.heappop(heap)
            ordered.append(task_id)
            for child in sorted(dependents[task_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(heap, child)
        if len(ordered) != len(self._tasks):
            cyclic = sorted(set(self._by_id) - set(ordered))
            raise TaskCycleError(f"dependency cycle detected among tasks: {cyclic}")
        return ordered


def _worker_run_payload(run: WorkerRun) -> dict[str, object]:
    """The ``worker_run`` entity payload (stable shape across statuses)."""
    return {
        "task_id": run.task_id,
        "status": run.status.value,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at is not None else None,
        "findings": [finding.model_dump(mode="json") for finding in run.findings],
        "error": run.error,
    }


class Scheduler:
    """Bounded-parallel, conflict-aware task scheduler.

    Args:
        dag: The task DAG to schedule.
        runner: The injected runner that executes one task at a time.
        max_workers: Maximum number of tasks running concurrently
            (>= 1); the supervisor passes ``config.max_workers``.
        run_id: Run identifier recorded on every event and in every
            worker-run fingerprint.
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``scheduler.*`` run events; when ``None`` no
            events are emitted (the graph still records state).
    """

    def __init__(
        self,
        dag: TaskDAG,
        *,
        runner: TaskRunner,
        max_workers: int,
        run_id: str,
        event_log: EventLog | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if not run_id:
            raise ValueError("run_id must be non-empty")
        self._dag = dag
        self._runner = runner
        self._max_workers = max_workers
        self._run_id = run_id
        self._event_log = event_log

    async def run(self, graph: StateGraph) -> SchedulerResult:
        """Schedule the whole DAG with bounded parallelism.

        Flow: persist the DAG (task entities + ``TASK IMPLEMENTS PLANSTEP``
        edges, idempotently) -> dispatch loop: at each step start every
        dependency-complete, non-conflicting task in deterministic id order
        up to ``max_workers`` concurrent runs -> persist each worker run
        (entity + ``graph.*`` event, recorded before the runner executes)
        -> on completion, update the worker run with its status and findings
        (plus the ``WORKER_RUN EXPLORED HYPOTHESIS`` edge) -> emit
        ``scheduler.*`` run events throughout. Conflicting tasks serialize;
        independent tasks run concurrently; a runner crash becomes a
        structured failed worker run (never silent, never retried).

        The DAG is scheduled once per run id: task entities are idempotent,
        but a worker run for an already-recorded (run id, task id) pair
        fails loudly rather than rewriting history.

        Args:
            graph: The authoritative SQLite state graph to persist into.

        Returns:
            The typed :class:`SchedulerResult`: every worker run in
            deterministic start order, plus succeeded/failed counts.

        Raises:
            SchedulerError: If the dispatch loop makes no progress (a
                defensive deadlock check — construction already rejects
                cycles and missing dependencies).
            StateGraphError: If a graph mutation fails.
        """
        await self._persist_tasks(graph)
        self._append_run_event(SCHEDULER_RUN_STARTED, {"task_count": len(self._dag)})
        completed: set[str] = set()
        running: dict[str, asyncio.Task[TaskOutcome]] = {}
        runs: dict[str, WorkerRun] = {}
        start_order: list[str] = []
        total = len(self._dag)
        try:
            while len(completed) < total:
                for task_id in self._dag.ready_order(completed):
                    if len(running) >= self._max_workers:
                        break
                    if task_id in running or task_id in completed:
                        continue
                    if self._conflicts_with_running(task_id, running):
                        continue
                    run = await self._start_task(graph, task_id)
                    runs[task_id] = run
                    start_order.append(task_id)
                    running[task_id] = asyncio.create_task(self._execute(graph, task_id))
                if not running:
                    raise SchedulerError(
                        "scheduler made no progress: no task is running and no "
                        "ready task can start (this should be impossible after "
                        "DAG construction rejected cycles and missing dependencies)"
                    )
                done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    outcome = finished.result()
                    del running[outcome.task_id]
                    completed.add(outcome.task_id)
                    runs[outcome.task_id] = await self._finish_task(
                        graph, runs[outcome.task_id], outcome
                    )
        finally:
            pending = list(running.values())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        result = SchedulerResult(
            run_id=self._run_id,
            worker_runs=tuple(runs[task_id] for task_id in start_order),
            succeeded=sum(
                1 for task_id in start_order if runs[task_id].status is WorkerRunStatus.SUCCEEDED
            ),
            failed=sum(
                1 for task_id in start_order if runs[task_id].status is WorkerRunStatus.FAILED
            ),
        )
        self._append_run_event(
            SCHEDULER_RUN_COMPLETED,
            {
                "worker_runs": len(result.worker_runs),
                "succeeded": result.succeeded,
                "failed": result.failed,
            },
        )
        return result

    async def _execute(self, graph: StateGraph, task_id: str) -> TaskOutcome:
        """Run one task through the injected runner, never silently.

        A runner exception becomes a structured failed outcome (fail loudly,
        continue the schedule); cancellation propagates so the whole run can
        be torn down cleanly.
        """
        task = self._dag.task(task_id)
        try:
            return await self._runner.run_task(task)
        except Exception as exc:  # noqa: BLE001 - structured failure, rule #9
            return TaskOutcome(
                task_id=task_id,
                status=WorkerRunStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _conflicts_with_running(self, task_id: str, running: Collection[str]) -> bool:
        """True when ``task_id`` may not start alongside the running tasks.

        A task conflicts with a running task when either side carries the
        reserved :data:`SERIALIZED_CONFLICT_KEY` (supervisor-only tasks never
        run concurrently with anything) or when their conflict-key sets
        intersect. Deterministic and fail-closed: any overlap denies.
        """
        task_keys = frozenset(self._dag.task(task_id).conflict_keys)
        for other_id in running:
            other_keys = frozenset(self._dag.task(other_id).conflict_keys)
            if SERIALIZED_CONFLICT_KEY in task_keys or SERIALIZED_CONFLICT_KEY in other_keys:
                return True
            if task_keys & other_keys:
                return True
        return False

    async def _start_task(self, graph: StateGraph, task_id: str) -> WorkerRun:
        """Record a worker run before the runner executes it (step 10).

        Persists the ``worker_run`` entity (status ``running``) with a
        same-timestamp ``graph.entity_created`` event, then the
        ``scheduler.task_started`` run event — both in deterministic dispatch
        order.
        """
        run = WorkerRun(
            id=worker_run_id(self._run_id, task_id),
            task_id=task_id,
            status=WorkerRunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        await self._create_entity(graph, run.id, ENTITY_WORKER_RUN, _worker_run_payload(run))
        self._append_run_event(
            SCHEDULER_TASK_STARTED,
            {"task_id": task_id, "worker_run_id": run.id},
            task_id=task_id,
            worker_id=run.id,
        )
        return run

    async def _finish_task(
        self, graph: StateGraph, started: WorkerRun, outcome: TaskOutcome
    ) -> WorkerRun:
        """Record a worker run's verdict and its structured findings.

        Updates the ``worker_run`` entity (status, findings, error,
        ``finished_at``) with a same-timestamp ``graph.entity_updated``
        event, creates the ``WORKER_RUN EXPLORED HYPOTHESIS`` edge when the
        task explores a hypothesis, and emits the
        ``scheduler.task_completed`` / ``scheduler.task_failed`` run event.
        """
        finished = WorkerRun(
            id=started.id,
            task_id=started.task_id,
            status=outcome.status,
            started_at=started.started_at,
            finished_at=datetime.now(UTC),
            findings=outcome.findings,
            error=outcome.error,
        )
        at = datetime.now(UTC)
        await graph.update_entity(finished.id, _worker_run_payload(finished), at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_UPDATED,
                    self._run_id,
                    SCHEDULER_PRODUCER,
                    GraphEntityUpdated(
                        entity_id=finished.id,
                        data=_worker_run_payload(finished),
                        at=at,
                    ),
                )
            )
        task = self._dag.task(outcome.task_id)
        if task.hypothesis_id is not None:
            await self._create_edge(
                graph,
                f"{finished.id}-explored-{task.hypothesis_id}",
                EDGE_WORKER_RUN_EXPLORED_HYPOTHESIS,
                finished.id,
                task.hypothesis_id,
            )
        event_type = (
            SCHEDULER_TASK_COMPLETED
            if outcome.status is WorkerRunStatus.SUCCEEDED
            else SCHEDULER_TASK_FAILED
        )
        self._append_run_event(
            event_type,
            {
                "task_id": outcome.task_id,
                "worker_run_id": finished.id,
                "findings": len(outcome.findings),
            },
            task_id=outcome.task_id,
            worker_id=finished.id,
        )
        return finished

    async def _persist_tasks(self, graph: StateGraph) -> None:
        """Persist the DAG as ``task`` entities, idempotently.

        A task entity that already exists (same DAG persisted before) is left
        untouched; its ``TASK IMPLEMENTS PLANSTEP`` edge is created only when
        missing. Every mutation is mirrored as a same-timestamp
        ``graph.*`` event.
        """
        for task in self._dag.tasks:
            if await graph.get_entity(task.id) is not None:
                continue
            await self._create_entity(
                graph,
                task.id,
                ENTITY_TASK,
                {
                    "depends_on": list(task.depends_on),
                    "conflict_keys": list(task.conflict_keys),
                    "plan_step_id": task.plan_step_id,
                    "hypothesis_id": task.hypothesis_id,
                },
            )
            if task.plan_step_id is not None:
                edge_id = f"{task.id}-implements-{task.plan_step_id}"
                if await graph.get_edge(edge_id) is None:
                    await self._create_edge(
                        graph,
                        edge_id,
                        EDGE_TASK_IMPLEMENTS_PLANSTEP,
                        task.id,
                        task.plan_step_id,
                    )

    async def _create_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        entity_type: str,
        data: dict[str, object],
    ) -> None:
        """Create one entity and mirror the mutation to the event log."""
        at = datetime.now(UTC)
        await graph.create_entity(entity_id, entity_type, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    self._run_id,
                    SCHEDULER_PRODUCER,
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        data=data,
                        at=at,
                    ),
                )
            )

    async def _create_edge(
        self,
        graph: StateGraph,
        edge_id: str,
        edge_type: str,
        src_id: str,
        dst_id: str,
    ) -> None:
        """Create one edge and mirror the mutation to the event log."""
        at = datetime.now(UTC)
        await graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    self._run_id,
                    SCHEDULER_PRODUCER,
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=edge_type,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=at,
                    ),
                )
            )

    def _append_run_event(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        task_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Append one ``scheduler.*`` run event when a log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=event_type,
                producer=SCHEDULER_PRODUCER,
                task_id=task_id,
                worker_id=worker_id,
                payload=payload,
            )
        )
