"""Scope-limited specialist workers for OzzGraph (PR25).

Implements the second slice of Phase 9 "Workers"
(docs/IMPLEMENTATION_PLAN.md, PR step 25; docs/ARCHITECTURE.md,
"Scheduler" + "Parallelism"; AGENTS.md data invariant "a worker cannot
mutate state outside its declared task scope"): the declarative worker
scope model and the concrete scope-limited specialist workers that the
scheduler (PR24) drives. The reducer (PR step 26) and supervisor wiring
are separate later PRs and are NOT implemented here.

Design rules:

- Declarative scopes, validated loudly at construction (AGENTS.md rule
  #9): a :class:`WorkerScope` declares the command families, phases,
  mutation permission, and optional target-allowlist narrowing a worker
  may use. Empty scopes, blank or duplicate families, unknown families,
  and a read-only scope declaring a mutating family are all rejected
  loudly at construction through the typed :class:`WorkerScopeError`
  hierarchy. Scopes are immutable contracts and are declared as class
  attributes on the concrete workers.

- Fail-closed isolation (AGENTS.md data invariant): a
  :class:`WorkerTask` — the scheduler's DAG node plus the one bounded
  action it performs — is assigned to a worker only when the worker's
  declared scope covers the task's required scope; a conflicting
  assignment is rejected loudly BEFORE any execution with the typed
  :class:`TaskOutOfScopeError`. At run time
  :meth:`SpecialistWorker.run_task` re-checks the assignment, refuses
  unassigned tasks, and rejects commands whose classified family is
  outside the worker's declared families (:class:`FamilyOutOfScopeError`)
  or is a mutating family on a read-only worker
  (:class:`ReadOnlyViolationError`) — rejected with a structured error,
  never silently filtered.

- Deterministic mutation partition: the policy gate's command families
  are partitioned once, at module level, into mutating
  (:data:`MUTATING_COMMAND_FAMILIES`, ``{"exploit"}``) and read-only
  (``recon`` and ``shell``), mirroring docs/ARCHITECTURE.md "Safe
  parallel work": evidence gathering (recon/shell) parallelizes, mutable
  exploit chains and rate-limited credential attacks (exploit) do not. A
  read-only worker can never run a mutating-family command, and a scope
  that declares one is rejected at construction.

- One bounded action per run (AGENTS.md rule #4): each
  :class:`WorkerTask` carries exactly one command plus its timeout and
  output limit. The command is gated through
  :class:`~ozzgraph.policy.ScopePolicy` (the worker's families passed as
  ``worker_scope``) and, when the scope declares target narrowing,
  through a scope-narrowed policy, before its fingerprint is recorded
  and it is executed through the bounded shell runner. The worker stores
  the bounded output as content-addressed artifacts (evidence ids) and
  returns a :class:`~ozzgraph.scheduler.TaskOutcome` carrying one
  structured :class:`~ozzgraph.scheduler.Finding` with provenance (task
  id, worker source) and mandatory evidence references (AGENTS.md rule
  #3) — never free-form prose as state.

- Supervisor-only serialization composes (AGENTS.md rule #7):
  :class:`SubmissionWorker` is the supervisor-serialized worker wrapper.
  It refuses any task that does not carry the reserved
  :data:`~ozzgraph.scheduler.SERIALIZED_CONFLICT_KEY`
  (:class:`SerializationRequiredError`), so it can only ever run tasks
  created with :func:`~ozzgraph.scheduler.serialized_task`, which the
  scheduler already serializes against every other task.

- Deterministic: no randomness, no wall-clock ordering decisions, no
  hidden global mutable state (the only instance state is the assignment
  map, written exclusively through :meth:`SpecialistWorker.assign`), and
  no dynamic imports. Findings are deterministic: artifact ids are
  content-addressed, summaries derive from the command, exit code, and
  artifact ids, and confidence is a per-worker declared constant.

- Small kernel (AGENTS.md rule #10): workers are a component plus its
  contracts, delivered standalone like PR24. Nothing is wired into the
  supervisor, and workers persist nothing themselves — the scheduler
  owns the ``task``/``worker_run`` entities and the event log. A scope
  violation raised from :meth:`SpecialistWorker.run_task` becomes a
  structured failed worker run when driven through the scheduler
  (never silent, never retried).
"""

from __future__ import annotations

import ipaddress
from abc import ABC
from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.phases import Phase
from ozzgraph.policy import (
    COMMAND_FAMILIES,
    FingerprintStore,
    ScopePolicy,
    classify_family,
    extract_destinations,
)
from ozzgraph.scheduler import (
    SERIALIZED_CONFLICT_KEY,
    Finding,
    Task,
    TaskOutcome,
    WorkerRunStatus,
)
from ozzgraph.shell import ShellRunner, ToolResult

#: Command families the worker layer treats as MUTATING. Mirrors
#: docs/ARCHITECTURE.md "Safe parallel work": evidence gathering
#: (``recon``) and unclassified commands (``shell``) are read-only and
#: parallelize; exploit chains and rate-limited credential attacks
#: (``exploit``) mutate state and never run on a read-only worker. This
#: is a deterministic partition of the policy gate's family vocabulary;
#: the policy gate itself remains authoritative for every command.
MUTATING_COMMAND_FAMILIES: frozenset[str] = frozenset({"exploit"})

#: Default wall-clock budget for one worker action, in seconds.
DEFAULT_TIMEOUT_SECONDS = 30

#: Default per-stream output cap for one worker action, in characters
#: (mirrors the executor's default output limit).
DEFAULT_OUTPUT_LIMIT = 65536


class WorkerError(RuntimeError):
    """Base error for the worker layer (AGENTS.md rule #9)."""


class WorkerScopeError(WorkerError):
    """Base error for every worker-scope rejection (fail closed)."""


class EmptyWorkerScopeError(WorkerScopeError):
    """A scope declares no command families or no phases.

    Raised at construction: a worker that may run nothing is a
    configuration error, never a silently permissive worker.
    """


class UnknownCommandFamilyError(WorkerScopeError):
    """A scope declares a command family the policy gate does not know.

    Raised at construction so a mistyped family fails loudly instead of
    silently narrowing (or widening) the worker's permissions later.
    """


class ReadOnlyScopeError(WorkerScopeError):
    """A read-only scope declares a mutating command family.

    Raised at construction: a read-only worker declaring ``exploit`` is
    a contradiction (AGENTS.md data invariant "a worker cannot mutate
    state outside its declared task scope") and is rejected before it
    can ever run.
    """


class TaskOutOfScopeError(WorkerScopeError):
    """A task is outside the worker's declared scope.

    Raised when a task is assigned to a worker whose declared scope does
    not cover the task's required scope (families, phases, mutation
    permission, or target narrowing), or when :meth:`run_task` is called
    for a task the worker was never assigned — always BEFORE any
    execution.
    """


class FamilyOutOfScopeError(WorkerScopeError):
    """A command classifies into a family the worker's scope forbids.

    Raised at run time from the command itself (the policy gate's
    deterministic family classification), never silently filtered: an
    action attempted outside the worker's allowed command families is
    rejected loudly.
    """


class ReadOnlyViolationError(WorkerScopeError):
    """A read-only worker attempted a mutating-family command.

    Raised at run time BEFORE any execution: a read-only worker can
    never run a command classified into
    :data:`MUTATING_COMMAND_FAMILIES`.
    """


class SerializationRequiredError(WorkerScopeError):
    """A supervisor-serialized worker refuses a non-serialized task.

    Raised when a task assigned to :class:`SubmissionWorker` (or any
    serialized worker) does not carry the reserved
    :data:`~ozzgraph.scheduler.SERIALIZED_CONFLICT_KEY` — the worker
    wrapper only ever runs tasks built with
    :func:`~ozzgraph.scheduler.serialized_task`, so the scheduler's
    serialization gate and the worker's own gate agree.
    """


class WorkerTaskError(WorkerError):
    """Base error for task-assignment failures."""


class DuplicateAssignmentError(WorkerTaskError):
    """A worker was assigned the same task id twice.

    Assignments are one per task id; a duplicate is a wiring error and
    fails loudly rather than silently replacing the first assignment.
    """


class WorkerScope(BaseModel):
    """One worker's declarative scope: what it may run and how.

    A scope is an immutable contract between the harness and a
    :class:`SpecialistWorker`. Construction validates it loudly
    (AGENTS.md rule #9): at least one command family and at least one
    phase are required (no empty scopes), families must be known to the
    policy gate, and a read-only scope cannot declare a mutating family.

    Attributes:
        name: Stable scope name (e.g. ``"recon"``), for errors and logs.
        command_families: Command families the worker may run (a subset
            of :data:`ozzgraph.policy.COMMAND_FAMILIES`; casefolded).
            ``shell`` is never implied — declare it explicitly.
        phases: Graph phases the worker may serve, deduplicated and
            ordered by the canonical :class:`Phase` definition order.
        mutating: True when the worker may run mutating work (exploit
            chains, credential attacks, submissions); False marks a
            read-only worker that can never run a mutating-family
            command.
        target_allowlist: Optional narrowing of the operator policy's
            target allowlist (hostnames, IPs, CIDRs). Empty means no
            narrowing at the worker layer — the operator policy
            governs.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    command_families: tuple[str, ...]
    phases: tuple[Phase, ...]
    mutating: bool
    target_allowlist: tuple[str, ...] = ()

    @field_validator("command_families")
    @classmethod
    def _families_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank, duplicate, and unknown families loudly.

        Families are casefolded for a deterministic vocabulary, then
        checked against the policy gate's known families so a mistyped
        family can never silently change what a worker may run.
        """
        folded = [family.casefold() for family in value]
        for family in folded:
            if not family or not family.strip():
                raise ValueError("command families must be non-empty strings")
        if len(set(folded)) != len(folded):
            raise ValueError("command families must be unique")
        for family in folded:
            if family not in COMMAND_FAMILIES:
                raise UnknownCommandFamilyError(
                    f"unknown command family {family!r}; known families: {sorted(COMMAND_FAMILIES)}"
                )
        return tuple(folded)

    @field_validator("phases")
    @classmethod
    def _phases_canonical(cls, value: tuple[Phase, ...]) -> tuple[Phase, ...]:
        """Deduplicate phases and order them by the Phase enum order.

        Deterministic rendering regardless of author input order: the
        canonical order is the :class:`Phase` definition order (the
        ARCHITECTURE.md phase order), and duplicates are dropped.
        """
        return tuple(phase for phase in Phase if phase in value)

    @field_validator("target_allowlist")
    @classmethod
    def _targets_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank and duplicate target allowlist entries."""
        for entry in value:
            if not entry or not entry.strip():
                raise ValueError("target allowlist entries must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("target allowlist entries must be unique")
        return value

    @model_validator(mode="after")
    def _scope_must_be_nonempty_and_consistent(self) -> Self:
        """Reject empty scopes and read-only/mutating contradictions.

        Raises:
            EmptyWorkerScopeError: If no command family or no phase is
                declared.
            ReadOnlyScopeError: If a read-only scope declares a
                mutating command family.
        """
        if not self.command_families:
            raise EmptyWorkerScopeError(
                f"worker scope {self.name!r} must declare at least one command family"
            )
        if not self.phases:
            raise EmptyWorkerScopeError(
                f"worker scope {self.name!r} must declare at least one phase"
            )
        if not self.mutating:
            mutating = set(self.command_families) & set(MUTATING_COMMAND_FAMILIES)
            if mutating:
                raise ReadOnlyScopeError(
                    f"read-only worker scope {self.name!r} cannot declare mutating "
                    f"command families {sorted(mutating)}"
                )
        return self

    def covers(self, required: WorkerScope) -> bool:
        """True when this scope covers every capability ``required`` needs.

        ``required`` is a task's required scope (see
        :class:`WorkerTask`). Coverage is family containment, phase
        containment, mutation permission (a read-only scope cannot cover
        mutating work), and target narrowing (every required target must
        be covered by this scope's allowlist entries, using the policy
        gate's hostname/CIDR matching semantics).
        """
        return not self.gaps(required)

    def gaps(self, required: WorkerScope) -> tuple[str, ...]:
        """Human-readable reasons this scope cannot cover ``required``.

        Returns an empty tuple when coverage holds. Deterministic and
        ordered: families, phases, mutation permission, targets.
        """
        missing_families = set(required.command_families) - set(self.command_families)
        missing_phases = set(required.phases) - set(self.phases)
        gaps: list[str] = []
        if missing_families:
            gaps.append(
                f"required command families {sorted(missing_families)} are outside "
                f"declared families {sorted(self.command_families)}"
            )
        if missing_phases:
            gaps.append(
                f"required phases {sorted(phase.value for phase in missing_phases)} are "
                f"outside declared phases {sorted(phase.value for phase in self.phases)}"
            )
        if required.mutating and not self.mutating:
            gaps.append("required work is mutating but the worker is read-only")
        if required.target_allowlist and self.target_allowlist:
            uncovered = [
                target
                for target in required.target_allowlist
                if not _target_covered(target, self.target_allowlist)
            ]
            if uncovered:
                gaps.append(
                    f"required targets {uncovered} are not covered by the declared "
                    f"target allowlist {list(self.target_allowlist)}"
                )
        return tuple(gaps)


class WorkerTask(BaseModel):
    """One task assigned to a specialist worker: the DAG node plus work.

    A worker executes exactly ONE bounded action per task (AGENTS.md
    rule #4): the ``command`` plus its timeout and output limit. The
    ``required_scope`` declares what the task needs — its command
    families, the phases it may serve, whether it mutates state, and the
    targets it addresses — so the worker's declared scope can be checked
    against it BEFORE any execution.

    Attributes:
        task: The scheduler's DAG node this assignment serves (the
            worker looks assignments up by ``task.id``).
        command: The single bounded command line to execute.
        phase: The graph phase this action serves; must be within
            ``required_scope.phases``.
        required_scope: The scope the task needs; the worker must cover
            it, or the assignment is rejected loudly.
        timeout_seconds: Wall-clock budget for the bounded action.
        output_limit: Per-stream output cap for the bounded action.
        working_directory: Directory the command runs in.
    """

    model_config = ConfigDict(extra="forbid")

    task: Task
    command: str = Field(min_length=1)
    phase: Phase
    required_scope: WorkerScope
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1)
    output_limit: int = Field(default=DEFAULT_OUTPUT_LIMIT, ge=1)
    working_directory: str = Field(default=".", min_length=1)

    @model_validator(mode="after")
    def _phase_within_required_scope(self) -> Self:
        """The action's phase must be one the required scope permits."""
        if self.phase not in self.required_scope.phases:
            raise ValueError(
                f"task {self.task.id!r} phase {self.phase.value!r} is not within the "
                f"required scope's phases "
                f"{[phase.value for phase in self.required_scope.phases]}"
            )
        return self


class SpecialistWorker(ABC):
    """Base class for scope-limited workers (the TaskRunner contract).

    A concrete worker declares its ``scope``, ``worker_id``, and
    ``default_confidence`` as class attributes; everything else is
    inherited. :meth:`run_task` implements the scheduler's
    :class:`~ozzgraph.scheduler.TaskRunner` protocol and executes ONLY
    within the declared scope:

    1. the task must be assigned (``assign``), or
       :class:`TaskOutOfScopeError` raises — nothing executes;
    2. the assignment's required scope must be covered by the worker's
       declared scope (re-checked at run time, fail closed);
    3. the command's classified family must be within the declared
       families, and a read-only worker must never attempt a mutating
       family (:class:`ReadOnlyViolationError`);
    4. the command is gated through the injected policy (and the
       scope-narrowed policy when target narrowing is declared), its
       fingerprint is recorded, and it runs through the bounded shell
       runner;
    5. bounded output is stored as content-addressed artifacts and
       returned as a structured :class:`Finding` inside the
       :class:`TaskOutcome` — provenance (task id, worker source) plus
       mandatory evidence references, never free-form prose.

    Args:
        artifacts: The artifact store raw output is stored into; every
            successful run references its output artifact as evidence.
        policy: The operator-level scope policy gate; defaults to a
            fresh :class:`ScopePolicy` (fail closed: empty target
            allowlist).
        runner: The bounded shell runner; defaults to a fresh
            :class:`ShellRunner`.
        store: The fingerprint store duplicate rejection records into;
            defaults to a fresh in-memory store (one per worker
            instance).

    Raises:
        WorkerError: If the concrete class does not declare a
            ``scope``, a non-empty ``worker_id``, or a
            ``default_confidence`` in [0.0, 1.0].
    """

    scope: ClassVar[WorkerScope]
    worker_id: ClassVar[str]
    default_confidence: ClassVar[float]

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        policy: ScopePolicy | None = None,
        runner: ShellRunner | None = None,
        store: FingerprintStore | None = None,
    ) -> None:
        declared_scope = getattr(type(self), "scope", None)
        if not isinstance(declared_scope, WorkerScope):
            raise WorkerError(f"{type(self).__name__} must declare a WorkerScope class attribute")
        worker_id = getattr(type(self), "worker_id", "")
        if not worker_id:
            raise WorkerError(
                f"{type(self).__name__} must declare a non-empty worker_id class attribute"
            )
        confidence = getattr(type(self), "default_confidence", None)
        if not isinstance(confidence, float) or not 0.0 <= confidence <= 1.0:
            raise WorkerError(
                f"{type(self).__name__} must declare a default_confidence in [0.0, 1.0]"
            )
        self._assignments: dict[str, WorkerTask] = {}
        self._policy = policy if policy is not None else ScopePolicy()
        self._runner = runner if runner is not None else ShellRunner()
        self._store = store if store is not None else FingerprintStore()
        self._artifacts = artifacts
        self._narrowed_policy: ScopePolicy | None = None
        if self.scope.target_allowlist:
            self._narrowed_policy = ScopePolicy(
                target_allowlist=self.scope.target_allowlist,
                allowed_command_families=self.scope.command_families,
            )

    @property
    def assignments(self) -> tuple[WorkerTask, ...]:
        """Every assigned task, in deterministic (task-id) order."""
        return tuple(self._assignments[task_id] for task_id in sorted(self._assignments))

    def assign(self, work: WorkerTask) -> None:
        """Assign one task to this worker, rejecting out-of-scope work.

        The assignment gate runs BEFORE any scheduling or execution: the
        worker's declared scope must cover the task's required scope, or
        :class:`TaskOutOfScopeError` raises with the exact gaps.

        Raises:
            DuplicateAssignmentError: If the task id is already assigned.
            TaskOutOfScopeError: If the worker's scope cannot cover the
                task's required scope.
        """
        if work.task.id in self._assignments:
            raise DuplicateAssignmentError(
                f"worker {self.worker_id!r} already has an assignment for task {work.task.id!r}"
            )
        self._task_gate(work.task)
        gaps = self.scope.gaps(work.required_scope)
        if gaps:
            raise TaskOutOfScopeError(
                f"worker {self.worker_id!r} scope {self.scope.name!r} cannot run task "
                f"{work.task.id!r}: {'; '.join(gaps)}"
            )
        self._assignments[work.task.id] = work

    async def run_task(self, task: Task) -> TaskOutcome:
        """Run one assigned task, strictly within the declared scope.

        Implements the scheduler's TaskRunner contract. Every scope
        rejection raises a typed :class:`WorkerScopeError` BEFORE any
        execution — when driven through the scheduler, the raise becomes
        a structured failed worker run (never silent, never retried).

        Raises:
            TaskOutOfScopeError: If the task is not assigned to this
                worker or the assignment's required scope is no longer
                covered.
            FamilyOutOfScopeError: If the command classifies into a
                family outside the declared families.
            ReadOnlyViolationError: If a read-only worker attempts a
                mutating-family command.
        """
        work = self._assignments.get(task.id)
        if work is None:
            raise TaskOutOfScopeError(
                f"worker {self.worker_id!r} has no assignment for task {task.id!r}; "
                "a worker cannot run a task outside its declared assignments"
            )
        self._task_gate(task)
        self._enforce_assignment(work)
        self._check_action(work.command)
        return await self._execute(work)

    def _task_gate(self, task: Task) -> None:
        """Hook for worker-specific task gates (default: no restriction).

        Called at assignment and again before execution (fail closed).
        :class:`SubmissionWorker` overrides this to require the reserved
        serialized conflict key.
        """

    def _enforce_assignment(self, work: WorkerTask) -> None:
        """Re-check scope coverage immediately before execution."""
        gaps = self.scope.gaps(work.required_scope)
        if gaps:
            raise TaskOutOfScopeError(
                f"worker {self.worker_id!r} scope {self.scope.name!r} cannot run task "
                f"{work.task.id!r}: {'; '.join(gaps)}"
            )

    def _check_action(self, command: str) -> None:
        """Reject an action the worker's scope forbids, loudly.

        The command's family comes from the policy gate's deterministic
        classification (the actual action text, never a declared claim),
        so a mis-specified assignment cannot smuggle a command past the
        worker's families. A read-only worker can never attempt a
        mutating-family command; an action in a family the worker does
        not declare is rejected, not filtered.
        """
        family = classify_family(command)
        if not self.scope.mutating and family in MUTATING_COMMAND_FAMILIES:
            raise ReadOnlyViolationError(
                f"read-only worker {self.worker_id!r} cannot run command "
                f"{_bounded(command, 96)!r}: family {family!r} is a mutating "
                "command family"
            )
        if family not in self.scope.command_families:
            raise FamilyOutOfScopeError(
                f"worker {self.worker_id!r} scope {self.scope.name!r} cannot run "
                f"command {_bounded(command, 96)!r}: family {family!r} is outside "
                f"declared families {sorted(self.scope.command_families)}"
            )

    async def _execute(self, work: WorkerTask) -> TaskOutcome:
        """Gate, record, run, and evidence one bounded action.

        Order is deterministic and fail-closed: the scope-narrowed
        policy (target narrowing) and the operator policy gate both run
        before the fingerprint is recorded, and the fingerprint is
        recorded before the command executes (loop prevention). A
        nonzero exit, a timeout, or a process that never exits produces
        a structured failed outcome; success stores the bounded output
        as content-addressed artifacts and returns a succeeded outcome
        with one evidence-referencing finding.
        """
        command = work.command
        phase = work.phase
        decision = self._policy.check(
            command,
            phase=phase.value,
            worker_scope=",".join(self.scope.command_families),
        )
        if self._narrowed_policy is not None:
            self._narrowed_policy.check(command, phase=phase.value)
        self._store.record(decision.fingerprint, canonical=decision.canonical)
        result = await self._runner.run(
            command=command,
            timeout_seconds=work.timeout_seconds,
            stdout_limit=work.output_limit,
            stderr_limit=work.output_limit,
            working_directory=Path(work.working_directory),
        )
        if result.timeout_state:
            return TaskOutcome(
                task_id=work.task.id,
                status=WorkerRunStatus.FAILED,
                error=(
                    f"command {_bounded(command, 96)!r} timed out after {work.timeout_seconds}s"
                ),
            )
        if result.exit_code is None:
            return TaskOutcome(
                task_id=work.task.id,
                status=WorkerRunStatus.FAILED,
                error=f"command {_bounded(command, 96)!r} never exited",
            )
        if result.exit_code != 0:
            return TaskOutcome(
                task_id=work.task.id,
                status=WorkerRunStatus.FAILED,
                error=(f"command {_bounded(command, 96)!r} exited with code {result.exit_code}"),
            )
        artifact_ids = await self._capture_output(work, result)
        finding = self._finding(work, result, artifact_ids)
        return TaskOutcome(
            task_id=work.task.id,
            status=WorkerRunStatus.SUCCEEDED,
            findings=(finding,),
        )

    async def _capture_output(self, work: WorkerTask, result: ToolResult) -> tuple[str, ...]:
        """Store bounded output as content-addressed artifacts.

        stdout is always stored (the run's primary evidence); stderr is
        stored as a second artifact when non-empty. Artifact ids are
        content-addressed sha256 digests, so identical output dedupes
        and the evidence references are deterministic across replays.
        """
        destination = self._first_destination(work.command)
        stdout_record = await self._artifacts.put(
            source=result.stdout.encode("utf-8"),
            mime_type="text/plain",
            source_action=result.action_id,
            target=destination,
            truncated=result.truncation_state.stdout_truncated,
        )
        artifact_ids = [stdout_record.artifact_id]
        if result.stderr:
            stderr_record = await self._artifacts.put(
                source=result.stderr.encode("utf-8"),
                mime_type="text/plain",
                source_action=result.action_id,
                target=destination,
                truncated=result.truncation_state.stderr_truncated,
            )
            artifact_ids.append(stderr_record.artifact_id)
        return tuple(artifact_ids)

    def _finding(
        self, work: WorkerTask, result: ToolResult, artifact_ids: tuple[str, ...]
    ) -> Finding:
        """One structured finding: provenance + mandatory evidence.

        The finding is attributed to the task that produced it
        (``task_id``) and the worker that ran it (``source``), references
        the stored output artifacts, and carries a bounded, deterministic
        summary — never free-form prose as state (AGENTS.md rule #3).
        """
        return Finding(
            task_id=work.task.id,
            source=self.worker_id,
            evidence_ids=artifact_ids,
            summary=(
                f"{self.worker_id} ran {_bounded(work.command, 96)!r} "
                f"(exit {result.exit_code}); evidence: {', '.join(artifact_ids)}"
            ),
            confidence=self.default_confidence,
        )

    @staticmethod
    def _first_destination(command: str) -> str | None:
        """The first destination the command addresses, if any."""
        destinations = extract_destinations(command)
        return destinations[0] if destinations else None


class ReconWorker(SpecialistWorker):
    """Read-only reconnaissance/enumeration worker (safe parallel work).

    Runs bounded recon and shell-family commands (service enumeration,
    host discovery, read-only probing) during the evidence-gathering
    phases. Read-only: it can never run a mutating-family command, so it
    parallelizes safely (docs/ARCHITECTURE.md, "Safe parallel work").
    """

    scope = WorkerScope(
        name="recon",
        command_families=("recon", "shell"),
        phases=(
            Phase.BOOTSTRAP,
            Phase.RECON,
            Phase.ENUMERATION,
            Phase.PIVOT,
        ),
        mutating=False,
    )
    worker_id = "recon"
    default_confidence = 0.7


class ArtifactAnalysisWorker(SpecialistWorker):
    """Read-only artifact-analysis worker (safe parallel work).

    Runs bounded shell-family analysis commands (``file``, ``strings``,
    ``binwalk``, ...) over stored artifacts during the analysis phases.
    Read-only, so separate analyses parallelize safely; its findings
    reference the stored analysis output as evidence.
    """

    scope = WorkerScope(
        name="artifact-analysis",
        command_families=("shell",),
        phases=(
            Phase.ENUMERATION,
            Phase.POST_EXPLOITATION,
            Phase.FLAG_HUNT,
        ),
        mutating=False,
    )
    worker_id = "artifact-analysis"
    default_confidence = 0.8


class SubmissionWorker(SpecialistWorker):
    """Supervisor-serialized submission worker (AGENTS.md rule #7).

    The wrapper demonstrating supervisor-only serialization: it refuses
    any task that does not carry the reserved
    :data:`~ozzgraph.scheduler.SERIALIZED_CONFLICT_KEY`
    (:class:`SerializationRequiredError`), so it can only ever run tasks
    built with :func:`~ozzgraph.scheduler.serialized_task` — which the
    scheduler already serializes against every other task. The same
    wrapper shape composes paid-hint purchases. Only the supervisor may
    wire this worker (AGENTS.md rule #5); this PR delivers the component
    only.
    """

    scope = WorkerScope(
        name="flag-submission",
        command_families=("shell",),
        phases=(Phase.VERIFY_AND_SUBMIT,),
        mutating=True,
    )
    worker_id = "flag-submission"
    default_confidence = 1.0

    def _task_gate(self, task: Task) -> None:
        """Only supervisor-serialized tasks may run here (fail closed)."""
        if SERIALIZED_CONFLICT_KEY not in task.conflict_keys:
            raise SerializationRequiredError(
                f"supervisor-serialized worker {self.worker_id!r} refuses task "
                f"{task.id!r}: the task must carry the reserved "
                f"{SERIALIZED_CONFLICT_KEY!r} conflict key (use "
                "ozzgraph.scheduler.serialized_task)"
            )


def _target_covered(destination: str, entries: tuple[str, ...]) -> bool:
    """True when ``destination`` matches an allowlist entry.

    Mirrors :meth:`ozzgraph.policy.ScopePolicy._is_allowlisted`:
    hostname destinations match only exact (case-insensitive) hostname
    entries; IP destinations match an exact IP entry or any CIDR entry
    containing the address. The policy gate remains authoritative for
    every executed command — this is the declarative containment rule
    the worker layer uses to check required targets against a declared
    allowlist.
    """
    lowered = destination.casefold()
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return any(entry.casefold() == lowered for entry in entries)
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry.casefold(), strict=False)
        except ValueError:
            continue  # hostname entry cannot match an IP destination
        address_is_v4 = isinstance(address, ipaddress.IPv4Address)
        if address_is_v4 != isinstance(network, ipaddress.IPv4Network):
            continue
        if address in network:
            return True
    return False


def _bounded(text: str, limit: int) -> str:
    """Deterministic truncation for error messages and summaries."""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
