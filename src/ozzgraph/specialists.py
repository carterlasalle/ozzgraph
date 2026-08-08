"""V07 specialists — bounded parallel hypothesis-testing batches (milestone 7).

Implements milestone 7 of docs/CHANGES_v2.md: genuine narrow micro-agents
(:class:`~ozzgraph.workers.SpecialistMicroAgent`) wired into the V06
brain-driven runner through a :class:`SpecialistFleet` that owns the
parallel batch lifecycle:

    build narrow tasks -> schedule (per-hypothesis conflict keys,
    bounded concurrency) -> execute (micro-agent loop) -> reduce
    (structured verdicts into graph facts) -> hypothesis lifecycle
    (promote confirmed / abandon refuted) -> findings (evidence-backed
    findings + findings.json render).

Design rules:

- Narrow context, never the full graph (docs/CHANGES_v2.md milestone 7):
  the fleet reads the graph ONLY to build each task's narrow context —
  the hypothesis id, its objective, its exploitation direction (the
  deterministic reproduction probe), and the phase. The micro agent
  itself never touches the graph: it runs the bounded experiments and
  concludes from the parsed observations. A hypothesis whose direction
  is missing or mutating-family is skipped loudly (a
  ``specialist.hypothesis_skipped`` run event) and stays open for the
  serialized strategic path — evidence gathering parallelizes, mutable
  exploit chains never do (AGENTS.md rule #7).

- Parallel independent hypotheses, serialized on conflict (AGENTS.md
  rule #7): each hypothesis becomes one :func:`~ozzgraph.scheduler.
  hypothesis_task` carrying the hypothesis id AS its conflict key, so
  two tasks exploring the SAME hypothesis are mutually exclusive while
  independent hypotheses run concurrently under ``max_workers`` —
  ``ready_order`` drives each batch, and global-strategy/mutation work
  stays serialized through the scheduler's reserved keys.

- Structured verdicts merge through the reducer (rule #3): the micro
  agent's conclusion rides the scheduler :class:`Finding` as
  ``verdict`` + ``impact`` (CWE/assets/confidence); the reducer
  validates the evidence references and merges the verdict into a
  ``fact`` entity unchanged. Every experiment's raw output is already
  content-addressed in the artifact store; the fleet additionally
  persists one ``evidence`` entity per worker run so every conclusion
  has a graph-resident evidence endpoint (``evidence-<worker-run id>``).

- Fail loudly (rule #9): a hypothesis that is not in the graph raises
  :class:`SpecialistError` (kernel state is corrupt — never silently
  skipped); a run that gathered no evidence is a structured failed
  worker run (the micro agent's own gate); every rejection is surfaced
  as an event, never silently filtered.

- Deterministic and idempotent: task ids derive from hypothesis ids,
  evidence/finding ids derive from worker-run/hypothesis ids, the
  scheduler's ready order drives the batch, and re-running a batch
  writes nothing new (entities exist -> skipped).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.entities import ENTITY_EVIDENCE
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.findings import (
    DEFAULT_FINDING_CWE,
    EDGE_FINDING_VALIDATES_HYPOTHESIS,
    ENTITY_FINDING,
    REPRODUCTION_LIMIT,
    Finding,
    FindingStore,
    ImpactCIA,
    ImpactLevel,
)
from ozzgraph.phases import Phase
from ozzgraph.policy import ScopePolicy, classify_family
from ozzgraph.reducer import Fact, Reducer
from ozzgraph.scheduler import (
    Scheduler,
    SchedulerResult,
    Task,
    TaskDAG,
    TaskOutcome,
    WorkerRun,
    WorkerRunStatus,
    hypothesis_task,
)
from ozzgraph.security_brain import HypothesisManager
from ozzgraph.shell import ShellRunner
from ozzgraph.state_graph import StateGraph
from ozzgraph.workers import (
    DEFAULT_MICRO_AGENT_SCOPE,
    MUTATING_COMMAND_FAMILIES,
    MicroAgentTask,
    SpecialistMicroAgent,
    SpecialistWorker,
    WorkerScope,
    WorkerTask,
)

#: Producer name on every specialist event (the component pattern:
#: ``scheduler.*``, ``reducer.*``, ``brain.*``).
SPECIALIST_PRODUCER = "specialists"

#: Run-log event emitted when a hypothesis batch starts.
SPECIALIST_BATCH_STARTED = "specialist.batch_started"
#: Run-log event emitted when a hypothesis batch finishes (the counts).
SPECIALIST_BATCH_COMPLETED = "specialist.batch_completed"
#: Run-log event emitted when one hypothesis is skipped (no direction,
#: mutating direction, or a phase outside the specialist scope).
SPECIALIST_HYPOTHESIS_SKIPPED = "specialist.hypothesis_skipped"
#: Run-log event emitted when a confirmed conclusion produces a finding.
SPECIALIST_FINDING_CREATED = "specialist.finding_created"

#: Deterministic impact for a confirmed conclusion whose CWE is the
#: sensitive-data default (mirrors the runner's exposed-data impact).
_EXPOSED_IMPACT: dict[str, ImpactLevel] = {
    "confidentiality": "high",
    "integrity": "low",
    "availability": "none",
}

#: Conservative impact for a confirmed conclusion without the
#: sensitive-data signal (mirrors the runner's default impact).
_DEFAULT_IMPACT: dict[str, ImpactLevel] = {
    "confidentiality": "medium",
    "integrity": "unknown",
    "availability": "unknown",
}


class SpecialistError(RuntimeError):
    """Base error for the specialist layer (AGENTS.md rule #9)."""


class SpecialistBatchResult(BaseModel):
    """The typed result of one :meth:`SpecialistFleet.run_hypothesis_batch`.

    Attributes:
        run_id: The run the batch served.
        scheduled: Number of specialist tasks dispatched.
        succeeded: Succeeded worker runs (each carries a verdict).
        failed: Failed worker runs (no evidence gathered, or a scope
            rejection).
        promoted: Hypothesis ids the batch confirmed (terminal).
        abandoned: Hypothesis ids the batch refuted (terminal).
        open_hypotheses: Hypothesis ids left open (inconclusive or the
            run failed).
        facts: The facts the reducer merged from the structured
            verdicts, in deterministic fact-id order.
        findings: The evidence-backed findings produced for confirmed
            hypotheses, in deterministic (worker-run) order.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    scheduled: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    promoted: tuple[str, ...] = ()
    abandoned: tuple[str, ...] = ()
    open_hypotheses: tuple[str, ...] = ()
    facts: tuple[Fact, ...] = ()
    findings: tuple[Finding, ...] = ()


class _AssignedRunner:
    """Scheduler TaskRunner dispatching each task to its assigned micro agent.

    The composition the supervisor-level batch needs (the PR24
    dispatcher pattern): one :class:`~ozzgraph.scheduler.TaskRunner`
    that forwards every DAG task to the :class:`SpecialistMicroAgent`
    that owns its assignment.
    """

    def __init__(self, agents: dict[str, SpecialistWorker]) -> None:
        self._agents = agents

    async def run_task(self, task: Task) -> TaskOutcome:
        return await self._agents[task.id].run_task(task)


class SpecialistFleet:
    """Owns the specialist workers and runs bounded parallel hypothesis batches.

    Args:
        artifacts: The artifact store every experiment's raw output is
            persisted into (the verdict evidence the reducer resolves).
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``specialist.*`` run events; when ``None`` no
            events are emitted (the graph still records state).
        run_id: Run identifier recorded on every event.
        policy: The operator-level scope policy gate the micro agents
            execute under; defaults to a fresh :class:`ScopePolicy`
            (fail closed: an empty target allowlist).
        shell: The bounded shell runner experiments execute through;
            defaults to a fresh :class:`ShellRunner`.
        max_workers: Bounded concurrency for one batch (>= 1); the
            runner passes ``config.max_workers``.
        state_dir: Optional run state directory; when set, produced
            findings are rendered to ``findings.json`` via
            :class:`FindingStore`.
        hypotheses: The hypothesis lifecycle manager; defaults to a
            fresh :class:`~ozzgraph.security_brain.HypothesisManager`
            over ``event_log``/``run_id``.
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        event_log: EventLog | None = None,
        run_id: str = "",
        policy: ScopePolicy | None = None,
        shell: ShellRunner | None = None,
        max_workers: int = 4,
        state_dir: Path | None = None,
        hypotheses: HypothesisManager | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if not run_id:
            raise ValueError("run_id must be non-empty")
        self._artifacts = artifacts
        self._event_log = event_log
        self._run_id = run_id
        self._policy = policy if policy is not None else ScopePolicy()
        self._shell = shell if shell is not None else ShellRunner()
        self._max_workers = max_workers
        self._state_dir = state_dir
        self._hypotheses = (
            hypotheses
            if hypotheses is not None
            else HypothesisManager(event_log=event_log, run_id=run_id)
        )
        self._reducer = Reducer(event_log=event_log, artifacts=artifacts)

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------

    async def run_hypothesis_batch(
        self,
        graph: StateGraph,
        *,
        hypothesis_ids: Sequence[str],
        phase: Phase,
    ) -> SpecialistBatchResult:
        """Test every hypothesis with a bounded parallel specialist batch.

        Flow: emit ``specialist.batch_started`` -> build one narrow
        :class:`MicroAgentTask` per hypothesis (skipping loudly the
        hypotheses that cannot be reproduced read-only) -> schedule the
        DAG through :class:`~ozzgraph.scheduler.Scheduler` under
        bounded concurrency with per-hypothesis conflict keys (same
        hypothesis serializes, independent hypotheses parallelize,
        ``ready_order`` drives each batch) -> persist one ``evidence``
        entity per worker run -> merge the structured verdicts into
        graph facts via the reducer -> promote confirmed hypotheses
        (with an evidence-backed finding) and abandon refuted ones ->
        emit ``specialist.batch_completed`` with the counts.

        Args:
            graph: The authoritative SQLite state graph.
            hypothesis_ids: The hypothesis entity ids to test, in the
                brain's ranked order.
            phase: The routed phase the batch serves; hypotheses whose
                phase the specialist scope cannot cover are skipped.

        Returns:
            The typed :class:`SpecialistBatchResult`.

        Raises:
            ValueError: If ``hypothesis_ids`` is empty.
            SpecialistError: If a hypothesis entity is missing from the
                graph (corrupt kernel state — fail loudly).
            StateGraphError: If a graph mutation fails.
        """
        if not hypothesis_ids:
            raise ValueError("hypothesis_ids must be non-empty")
        # Defensive dedupe: task ids derive from hypothesis ids, so the
        # same hypothesis twice would collide on the DAG node; the brain
        # never emits duplicates, but the gate is cheap and loud-safe.
        hypothesis_ids = tuple(dict.fromkeys(hypothesis_ids))
        self._append(
            SPECIALIST_BATCH_STARTED,
            {
                "hypotheses": len(hypothesis_ids),
                "phase": phase.value,
                "max_workers": self._max_workers,
            },
        )
        tasks: list[MicroAgentTask] = []
        agents: dict[str, SpecialistWorker] = {}
        skipped: list[dict[str, object]] = []
        targets = await graph.list_entities("target")
        target_id = targets[0].id if targets else None
        for hypothesis_id in hypothesis_ids:
            work, agent, reason = await self._build_task(
                graph, hypothesis_id, phase=phase, target_id=target_id
            )
            if work is None or agent is None:
                if reason:
                    skipped.append({"hypothesis_id": hypothesis_id, "reason": reason})
                continue
            tasks.append(work)
            agents[work.task.id] = agent
        for entry in skipped:
            self._append(SPECIALIST_HYPOTHESIS_SKIPPED, entry)
        if not tasks:
            result = SpecialistBatchResult(
                run_id=self._run_id,
                scheduled=0,
                succeeded=0,
                failed=0,
                open_hypotheses=tuple(hypothesis_ids),
            )
            self._append(
                SPECIALIST_BATCH_COMPLETED,
                {
                    "hypotheses": len(hypothesis_ids),
                    "scheduled": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "skipped": len(skipped),
                    "promoted": 0,
                    "abandoned": 0,
                    "open": len(result.open_hypotheses),
                    "facts": 0,
                    "findings": 0,
                },
            )
            return result

        dag = TaskDAG([work.task for work in tasks])
        scheduler = Scheduler(
            dag=dag,
            runner=_AssignedRunner(agents),
            max_workers=min(self._max_workers, len(tasks)),
            run_id=self._run_id,
            event_log=self._event_log,
        )
        scheduled = await scheduler.run(graph)
        await self._persist_evidence(graph, scheduled)
        reduced = await self._reducer.reduce(graph, self._run_id, scheduled.worker_runs)

        hypothesis_by_task = {work.task.id: work.task.hypothesis_id for work in tasks}
        commands_by_task = {
            work.task.id: tuple(experiment.command for experiment in work.experiments)
            for work in tasks
        }
        promoted: list[str] = []
        abandoned: list[str] = []
        open_hypotheses: list[str] = []
        created_findings: list[Finding] = []
        for run in scheduled.worker_runs:
            tested_hypothesis = hypothesis_by_task.get(run.task_id)
            if tested_hypothesis is None:
                continue  # defensive: every scheduled task has one
            if run.status is WorkerRunStatus.FAILED or not run.findings:
                open_hypotheses.append(tested_hypothesis)
                continue
            conclusion = run.findings[0]
            if conclusion.verdict == "confirmed":
                promoted.append(tested_hypothesis)
                await self._hypotheses.promote(graph, hypothesis_id=tested_hypothesis)
                created = await self._produce_finding(
                    graph, run, tested_hypothesis, commands_by_task.get(run.task_id, ())
                )
                if created is not None:
                    created_findings.append(created)
            elif conclusion.verdict == "refuted":
                abandoned.append(tested_hypothesis)
                await self._hypotheses.abandon(graph, hypothesis_id=tested_hypothesis)
            else:
                open_hypotheses.append(tested_hypothesis)

        result = SpecialistBatchResult(
            run_id=self._run_id,
            scheduled=len(tasks),
            succeeded=scheduled.succeeded,
            failed=scheduled.failed,
            promoted=tuple(promoted),
            abandoned=tuple(abandoned),
            open_hypotheses=tuple(open_hypotheses),
            facts=reduced.facts,
            findings=tuple(created_findings),
        )
        self._append(
            SPECIALIST_BATCH_COMPLETED,
            {
                "hypotheses": len(hypothesis_ids),
                "scheduled": result.scheduled,
                "succeeded": result.succeeded,
                "failed": result.failed,
                "skipped": len(skipped),
                "promoted": len(result.promoted),
                "abandoned": len(result.abandoned),
                "open": len(result.open_hypotheses),
                "facts": len(result.facts),
                "findings": len(result.findings),
            },
        )
        return result

    # ------------------------------------------------------------------
    # batch construction
    # ------------------------------------------------------------------

    async def _build_task(
        self,
        graph: StateGraph,
        hypothesis_id: str,
        *,
        phase: Phase,
        target_id: str | None,
    ) -> tuple[MicroAgentTask | None, SpecialistWorker | None, str | None]:
        """One narrow micro-agent task for one hypothesis.

        The narrow context (docs/CHANGES_v2.md milestone 7): the task
        carries the hypothesis id and ONE bounded reproduction
        experiment — the exploitation direction that produced the
        supporting evidence — gated through the worker's read-only
        recon/shell scope. The micro agent never sees the graph.

        Returns ``(None, None, reason)`` for a hypothesis that cannot
        be reproduced read-only (skipped loudly, stays open):
        """
        record = await graph.get_entity(hypothesis_id)
        if record is None:
            raise SpecialistError(
                f"hypothesis {hypothesis_id!r} is not in the graph; cannot build a specialist task"
            )
        direction = record.data.get("exploitation_direction")
        if not isinstance(direction, str) or not direction.strip():
            return None, None, "hypothesis has no exploitation_direction; nothing to reproduce"
        family = classify_family(direction)
        if family in MUTATING_COMMAND_FAMILIES:
            return (
                None,
                None,
                (
                    f"exploitation direction classifies into mutating family {family!r}; "
                    "evidence gathering is read-only (AGENTS.md rule #7)"
                ),
            )
        if phase not in DEFAULT_MICRO_AGENT_SCOPE.phases:
            return (
                None,
                None,
                (
                    f"phase {phase.value!r} is outside the specialist scope phases "
                    f"{[item.value for item in DEFAULT_MICRO_AGENT_SCOPE.phases]}"
                ),
            )
        task = hypothesis_task(f"specialist-{hypothesis_id}", hypothesis_id)
        required_scope = WorkerScope(
            name=f"required-{task.id}",
            command_families=("recon", "shell"),
            phases=(phase,),
            mutating=False,
        )
        experiment_scope = WorkerScope(
            name=f"experiment-{task.id}",
            command_families=(family,),
            phases=(phase,),
            mutating=False,
            target_allowlist=(target_id,) if target_id else (),
        )
        experiment = WorkerTask(
            task=task,
            command=direction,
            phase=phase,
            required_scope=experiment_scope,
        )
        work = MicroAgentTask(
            task=task,
            phase=phase,
            required_scope=required_scope,
            experiments=(experiment,),
        )
        agent = SpecialistMicroAgent(
            artifacts=self._artifacts,
            policy=self._policy,
            runner=self._shell,
        )
        agent.assign(work)
        return work, agent, None

    # ------------------------------------------------------------------
    # graph persistence + findings
    # ------------------------------------------------------------------

    async def _persist_evidence(self, graph: StateGraph, scheduled: SchedulerResult) -> None:
        """One ``evidence`` entity per succeeded worker run (V07).

        Every conclusion's artifact references gain a graph-resident
        evidence endpoint (``evidence-<worker-run id>``) referencing
        the artifact ids in its payload — the observation is persisted
        as durable graph state (AGENTS.md rule #1), and replay
        reconstructs the identical graph hash. Idempotent.
        """
        for run in scheduled.worker_runs:
            if run.status is WorkerRunStatus.FAILED or not run.findings:
                continue
            conclusion = run.findings[0]
            evidence_id = f"evidence-{run.id}"
            if await graph.get_entity(evidence_id) is not None:
                continue
            at = datetime.now(UTC)
            await self._create_entity(
                graph,
                evidence_id,
                ENTITY_EVIDENCE,
                {
                    "note": conclusion.summary,
                    "artifact_ids": list(conclusion.evidence_ids),
                    "worker_run_id": run.id,
                },
                at=at,
            )

    async def _produce_finding(
        self,
        graph: StateGraph,
        run: WorkerRun,
        hypothesis_id: str,
        commands: tuple[str, ...],
    ) -> Finding | None:
        """One evidence-backed finding for a confirmed hypothesis.

        Mirrors the runner's validated-finding path: the finding id is
        ``finding-<hypothesis id>`` (idempotent), the CWE comes from
        the verdict impact (the sensitive-data default when unset),
        affected assets are the seeded targets, reproduction is the
        bounded reproduction commands, and the impact CIA is the
        deterministic exposed/default split. Rendered to
        ``findings.json`` when a ``state_dir`` was configured.
        """
        conclusion = run.findings[0]
        finding_id = f"finding-{hypothesis_id}"
        if await graph.get_entity(finding_id) is not None:
            return None
        impact = conclusion.impact or {}
        cwe = impact.get("cwe")
        if not isinstance(cwe, str) or not cwe:
            cwe = DEFAULT_FINDING_CWE
        exposed = cwe == DEFAULT_FINDING_CWE
        targets = await graph.list_entities("target")
        target_id = targets[0].id if targets else None
        impact_payload = _EXPOSED_IMPACT if exposed else _DEFAULT_IMPACT
        finding = Finding(
            id=finding_id,
            cwe=cwe,
            affected_assets=tuple(record.id for record in targets),
            preconditions=("authorized assessment scope",),
            evidence_ids=conclusion.evidence_ids,
            reproduction=_bounded("; ".join(commands), REPRODUCTION_LIMIT),
            impact=ImpactCIA(**impact_payload),
            confidence=conclusion.confidence,
            hypothesis_id=hypothesis_id,
            target_id=target_id,
        )
        at = datetime.now(UTC)
        await self._create_entity(
            graph,
            finding_id,
            ENTITY_FINDING,
            finding.model_dump(mode="json"),
            at=at,
        )
        await self._create_edge(
            graph,
            f"{finding_id}-validates-{hypothesis_id}",
            EDGE_FINDING_VALIDATES_HYPOTHESIS,
            finding_id,
            hypothesis_id,
            at=at,
        )
        if self._state_dir is not None:
            FindingStore.for_run(self._state_dir).save(finding)
        self._append(
            SPECIALIST_FINDING_CREATED,
            {
                "finding_id": finding_id,
                "hypothesis_id": hypothesis_id,
                "cwe": finding.cwe,
                "confidence": finding.confidence,
            },
        )
        return finding

    # ------------------------------------------------------------------
    # event mirroring (AGENTS.md rule #1: replay reconstructs the hash)
    # ------------------------------------------------------------------

    async def _create_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        entity_type: str,
        data: dict[str, object],
        *,
        at: datetime,
    ) -> None:
        await graph.create_entity(entity_id, entity_type, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    self._run_id,
                    SPECIALIST_PRODUCER,
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
        *,
        at: datetime,
    ) -> None:
        await graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    self._run_id,
                    SPECIALIST_PRODUCER,
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=edge_type,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=at,
                    ),
                )
            )

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one ``specialist.*`` run event when a log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=event_type,
                producer=SPECIALIST_PRODUCER,
                payload=payload,
            )
        )


def _bounded(text: str, limit: int) -> str:
    """Deterministic truncation for summaries and reproduction steps."""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
