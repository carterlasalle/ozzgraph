"""Reducer — validated findings merge into authoritative graph facts (PR26).

Implements the third slice of Phase 9 "Workers" (docs/IMPLEMENTATION_PLAN.md,
PR step 26; docs/ARCHITECTURE.md, "Reducer"): the component that turns the
structured findings carried by persisted ``worker_run`` entities (PR24) into
authoritative ``fact`` graph entities AFTER validating their evidence
references (AGENTS.md rule #3). The scheduler persists findings; the reducer
promotes them. It never merges free-form model prose as authoritative state.

Design rules:

- Validation before merge (AGENTS.md rule #3): every id in a finding's
  ``evidence_ids`` must resolve — to an existing graph entity of type
  ``evidence`` (:data:`ozzgraph.entities.ENTITY_EVIDENCE`) or to an artifact
  known to the artifact store's index (when a store is configured). A
  finding with at least one unresolved reference raises
  :class:`UnresolvedEvidenceError` carrying the exact unresolved id(s) in
  its message (rule #9: fail loudly) and is NEVER written to the graph;
  :meth:`Reducer.reduce` catches the rejection per finding, counts it in
  :class:`ReducerResult.rejected`, surfaces it as a
  ``reducer.findings_rejected`` run event, and continues with the
  remaining findings so one bad finding cannot block the whole merge.

- Deterministic fact ids: a fact's entity id is ``fact-<sha256(fingerprint)>``
  where the fingerprint is ``{task_id}:{source}:{sorted(evidence_ids)}:{summary}``
  — the evidence tuple is normalized by sorting, so the id does not depend
  on finding field order and is reproducible when replaying events (the
  ``worker-run-<fingerprint>`` pattern from scheduler.py).

- Idempotent, conflict-safe merge: a fact entity that already exists is
  skipped and its ``FACT DERIVED_FROM EVIDENCE`` edges are created only
  when missing — and only toward references that are graph entities (an
  artifact-store-only reference has no graph endpoint to point at and
  stays in the fact payload as provenance) — so reducing the same worker
  runs twice writes nothing new, and two findings with identical
  fingerprints dedupe to one fact.
  Contradictory findings (same evidence, different summary/confidence)
  have different fingerprints and merge as separate facts — facts are
  additive with provenance; conflict resolution is downstream. This is the
  deterministic merge the PR title's "conflict handling" refers to
  (scheduler-level task conflicts are already done in PR24).

- Graph persistence (AGENTS.md rule #1): every mutation is mirrored as a
  same-timestamp ``graph.*`` event (producer ``reducer``, the PR20
  executor / PR24 scheduler pattern), so replaying the log reconstructs
  the identical graph hash. A rejected finding is never represented in
  the graph.

- Failed runs are skipped: a ``failed`` :class:`WorkerRun` carries no
  findings (the scheduler guarantees this), so :meth:`Reducer.reduce`
  skips failed runs entirely — nothing to merge, and never an error.

- Structured verdicts (V07, docs/CHANGES_v2.md milestone 7): a worker
  conclusion travels through :class:`~ozzgraph.scheduler.Finding` as an
  optional ``verdict`` (``confirmed`` / ``refuted`` / ``inconclusive``)
  plus an ``impact`` payload (CWE / assets / confidence). When present,
  the merged :class:`Fact` carries the same verdict and impact — the
  graph fact IS the structured verdict, not a summary that mentions it —
  and the fact fingerprint includes both, so two findings with the same
  evidence and summary but different verdicts merge as distinct facts
  (contradictions stay additive with provenance). Findings without a
  verdict merge exactly as before (additive optional fields).

- Small kernel (AGENTS.md rule #10): the reducer only validates and
  merges; nothing is wired into the supervisor here. It is a component
  plus its contracts, delivered standalone (PR step 26).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ozzgraph.artifacts import ArtifactIndexError, ArtifactNotFoundError, ArtifactStore
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
from ozzgraph.scheduler import (
    Finding,
    WorkerRun,
    WorkerRunStatus,
    _impact_shape_errors,
)
from ozzgraph.state_graph import StateGraph

#: Producer name on every reducer event.
REDUCER_PRODUCER = "reducer"

#: Run-log event emitted when a reduce() call starts.
REDUCER_RUN_STARTED = "reducer.run_started"
#: Run-log event emitted when a reduce() call finishes (with the counts).
REDUCER_RUN_COMPLETED = "reducer.run_completed"
#: Run-log event emitted when one finding is rejected for unresolvable
#: evidence references (fail loudly — the rejection is never silent).
REDUCER_FINDINGS_REJECTED = "reducer.findings_rejected"

#: Entity type the reducer writes for one validated finding
#: (docs/DATA_STRATEGY.md, lowercase by convention).
ENTITY_FACT = "fact"

#: Edge type linking a fact to the evidence it derives from (fact -> evidence,
#: docs/DATA_STRATEGY.md, uppercase by convention).
EDGE_FACT_DERIVED_FROM_EVIDENCE = "FACT DERIVED_FROM EVIDENCE"


class ReducerError(RuntimeError):
    """Base error for the reducer layer (AGENTS.md rule #9)."""


class UnresolvedEvidenceError(ReducerError):
    """A finding references an evidence/artifact id that does not exist.

    Raised by :meth:`Reducer.resolve_evidence` (and caught per finding by
    :meth:`Reducer.reduce`) carrying the exact unresolved id(s) in its
    message. A finding with unresolvable evidence is model prose (AGENTS.md
    rule #3) and is never merged — it is rejected loudly (rule #9), never
    silently dropped, never written to the graph.
    """


class Fact(BaseModel):
    """One validated, authoritative fact merged from a worker finding (rule #3).

    Attributes:
        id: Deterministic entity id ``fact-<sha256(fingerprint)>`` via
            :func:`fact_id` — reproducible when replaying events.
        task_id: The task that produced the finding (provenance).
        source: The finding's source.
        evidence_ids: The resolved evidence references; never empty
            (AGENTS.md data invariant: every ``Fact`` references at least
            one ``Evidence``).
        summary: Bounded prose summary — never authoritative by itself
            (the evidence references are the authority).
        confidence: The finding's confidence in [0.0, 1.0].
        verdict: The structured worker conclusion (V07): ``confirmed``
            / ``refuted`` / ``inconclusive``, or ``None`` for a plain
            finding without a verdict.
        impact: The structured verdict impact payload (V07): ``cwe``
            (None or a non-empty string), ``assets`` (non-empty
            strings), and ``confidence`` in [0.0, 1.0]; ``None`` for a
            plain finding.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    summary: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0.0, le=1.0)
    verdict: str | None = Field(default=None, pattern=r"^(confirmed|refuted|inconclusive)$")
    impact: dict[str, object] | None = None

    @field_validator("evidence_ids")
    @classmethod
    def _evidence_required(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject empty or blank evidence references (data invariant)."""
        for item in value:
            if not item or not item.strip():
                raise ValueError("evidence ids must be non-empty strings")
        if not value:
            raise ValueError(
                "a fact must reference at least one evidence/artifact id; "
                "a fact without evidence violates the AGENTS.md data invariant"
            )
        return value

    @field_validator("impact")
    @classmethod
    def _impact_shape(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        """The verdict impact payload carries exactly CWE/assets/confidence."""
        if value is None:
            return None
        errors = _impact_shape_errors(value)
        if errors:
            raise ValueError("; ".join(errors))
        return value


class ReducerResult(BaseModel):
    """The typed result of one :meth:`Reducer.reduce` call.

    Attributes:
        run_id: The run the reduction served.
        accepted: Number of facts merged (unique fact entities in
            ``facts``).
        rejected: Number of findings rejected loudly for unresolvable
            evidence references — never represented in the graph.
        facts: The merged facts, in deterministic (fact-id) order.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    accepted: int = Field(ge=0)
    rejected: int = Field(ge=0)
    facts: tuple[Fact, ...] = ()


def fact_id(finding: Finding) -> str:
    """Deterministic fact entity id: ``fact-<sha256(fingerprint)>``.

    The fingerprint is ``{task_id}:{source}:{sorted(evidence_ids)}:{summary}``
    for a plain finding — unchanged from before V07, so replaying a
    pre-V07 run reconstructs the identical fact ids (replay compatibility).
    A finding carrying a structured verdict (V07) extends the fingerprint
    with ``:{verdict}:{impact}``, so two findings with the same evidence
    and summary but different verdicts merge as distinct facts. The
    evidence tuple is normalized by sorting and the verdict/impact
    serialize deterministically, so the id does not depend on finding
    field order. Two identical findings always map to one fact (dedupe);
    two findings whose summary OR verdict differs map to two facts even
    when they share evidence (contradictions are additive with provenance).
    """
    fingerprint = (
        f"{finding.task_id}:{finding.source}:{sorted(finding.evidence_ids)}:{finding.summary}"
    )
    if finding.verdict is not None:
        fingerprint += f":{finding.verdict}:{_impact_fingerprint(finding.impact)}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"fact-{digest}"


def _impact_fingerprint(impact: dict[str, object] | None) -> str:
    """Deterministic fingerprint of a verdict impact payload (V07).

    ``None`` and an empty payload are distinct from a payload with
    content; items are sorted by key so field order never changes the
    fingerprint. Values are JSON-scalar or tuples of JSON scalars (the
    impact validator guarantees the shape), so ``repr`` is deterministic.
    """
    if impact is None:
        return "none"
    if not impact:
        return "empty"
    return repr(sorted(impact.items()))


def _fact_payload(fact: Fact) -> dict[str, object]:
    """The ``fact`` entity payload (docs/DATA_STRATEGY.md)."""
    payload: dict[str, object] = {
        "task_id": fact.task_id,
        "source": fact.source,
        "evidence_ids": list(fact.evidence_ids),
        "summary": fact.summary,
        "confidence": fact.confidence,
    }
    if fact.verdict is not None:
        payload["verdict"] = fact.verdict
    if fact.impact is not None:
        payload["impact"] = fact.impact
    return payload


class Reducer:
    """Validates worker findings and merges structured evidence into the graph.

    Args:
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``reducer.*`` run events; when ``None`` no events
            are emitted (the graph still records state).
        artifacts: Optional artifact store whose index resolves
            artifact-prefixed evidence references. When ``None``, only
            graph ``evidence`` entities resolve; artifact-prefixed ids
            that are not graph evidence ids reject.
    """

    def __init__(
        self,
        *,
        event_log: EventLog | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._event_log = event_log
        self._artifacts = artifacts

    async def resolve_evidence(self, graph: StateGraph, finding: Finding) -> tuple[str, ...]:
        """Resolve every evidence reference in ``finding``, or raise.

        Each id in ``finding.evidence_ids`` must resolve to an existing
        graph entity of type ``evidence`` OR to an artifact known to the
        artifact store's index (when a store is configured). The first
        unresolved reference raises :class:`UnresolvedEvidenceError`
        carrying the exact unresolved id(s) in its message (rule #9) —
        the caller must never write the finding to the graph.

        Args:
            graph: The authoritative SQLite state graph.
            finding: The finding whose evidence references to resolve.

        Raises:
            UnresolvedEvidenceError: If any reference is unresolved.

        Returns:
            The resolved evidence ids, deduplicated in first-seen order.
        """
        unresolved: list[str] = []
        for evidence_id in finding.evidence_ids:
            evidence_known = await self._evidence_known(graph, evidence_id)
            if not (evidence_known or await self._artifact_known(evidence_id)):
                unresolved.append(evidence_id)
        if unresolved:
            raise UnresolvedEvidenceError(
                f"finding from task {finding.task_id!r} references unresolved "
                f"evidence/artifact ids: {sorted(set(unresolved))}"
            )
        return tuple(dict.fromkeys(finding.evidence_ids))

    async def reduce(
        self,
        graph: StateGraph,
        run_id: str,
        worker_runs: Sequence[WorkerRun],
    ) -> ReducerResult:
        """Validate every finding and merge the accepted ones into facts.

        Flow: emit ``reducer.run_started`` -> for each worker run in
        order, skip failed runs (they carry no findings — the scheduler
        guarantees it), then validate each finding's evidence references
        via :meth:`resolve_evidence`; a rejected finding is counted,
        surfaced as a ``reducer.findings_rejected`` run event carrying the
        exact unresolved id(s) in its error message, and NEVER written to
        the graph; each accepted finding becomes one ``fact`` entity
        (idempotently — an existing fact is skipped, its ``FACT DERIVED
        FROM EVIDENCE`` edges created only when missing) plus one
        same-timestamp ``graph.*`` event per mutation (producer
        ``reducer``) -> emit ``reducer.run_completed`` with the counts.

        Args:
            graph: The authoritative SQLite state graph to merge into.
            run_id: Run identifier recorded on every event.
            worker_runs: The worker runs to reduce, in deterministic
                order (the scheduler's start order).

        Returns:
            The typed :class:`ReducerResult`: the merged facts in
            deterministic (fact-id) order plus accepted/rejected counts.

        Raises:
            ValueError: If ``run_id`` is empty.
            StateGraphError: If a graph mutation fails.
        """
        if not run_id:
            raise ValueError("run_id must be non-empty")
        facts_by_id: dict[str, Fact] = {}
        rejected = 0
        self._append_run_event(
            REDUCER_RUN_STARTED,
            {"worker_runs": len(worker_runs)},
            run_id=run_id,
        )
        for run in worker_runs:
            if run.status is WorkerRunStatus.FAILED:
                continue  # failed runs carry no findings (scheduler guarantee)
            for finding in run.findings:
                try:
                    evidence_ids = await self.resolve_evidence(graph, finding)
                except UnresolvedEvidenceError as exc:
                    rejected += 1
                    self._append_run_event(
                        REDUCER_FINDINGS_REJECTED,
                        {
                            "worker_run_id": run.id,
                            "task_id": finding.task_id,
                            "evidence_ids": list(finding.evidence_ids),
                            "error": str(exc),
                        },
                        run_id=run_id,
                        task_id=finding.task_id,
                        worker_id=run.id,
                    )
                    continue
                fact = Fact(
                    id=fact_id(finding),
                    task_id=finding.task_id,
                    source=finding.source,
                    evidence_ids=evidence_ids,
                    summary=finding.summary,
                    confidence=finding.confidence,
                    verdict=finding.verdict,
                    impact=finding.impact,
                )
                await self._merge_fact(graph, run_id, fact)
                facts_by_id[fact.id] = fact
        facts = tuple(sorted(facts_by_id.values(), key=lambda fact: fact.id))
        result = ReducerResult(
            run_id=run_id,
            accepted=len(facts),
            rejected=rejected,
            facts=facts,
        )
        self._append_run_event(
            REDUCER_RUN_COMPLETED,
            {"accepted": result.accepted, "rejected": result.rejected},
            run_id=run_id,
        )
        return result

    async def _merge_fact(self, graph: StateGraph, run_id: str, fact: Fact) -> None:
        """Idempotently persist one fact entity and its evidence edges.

        A fact entity that already exists (same deterministic id — an
        earlier reduce of the same runs, or a duplicate finding in this
        call) is left untouched; each ``FACT DERIVED_FROM EVIDENCE`` edge
        is created only when missing. An evidence reference that resolves
        via the artifact store but has no graph entity under that id gets
        no edge — the graph's foreign keys require both endpoints to
        exist — and stays in the fact payload's ``evidence_ids`` as
        provenance. Every mutation is mirrored as a same-timestamp
        ``graph.*`` event, so replay reconstructs the identical graph
        hash.
        """
        if await graph.get_entity(fact.id) is None:
            await self._create_entity(
                graph,
                run_id,
                fact.id,
                ENTITY_FACT,
                _fact_payload(fact),
            )
        for evidence_id in fact.evidence_ids:
            edge_id = f"{fact.id}-derived-{evidence_id}"
            if (
                await graph.get_edge(edge_id) is None
                and await graph.get_entity(evidence_id) is not None
            ):
                await self._create_edge(
                    graph,
                    run_id,
                    edge_id,
                    EDGE_FACT_DERIVED_FROM_EVIDENCE,
                    fact.id,
                    evidence_id,
                )

    async def _evidence_known(self, graph: StateGraph, entity_id: str) -> bool:
        """True when ``entity_id`` is an existing graph entity of type ``evidence``.

        A same-named entity of any other type does NOT resolve — an
        evidence reference is only satisfied by an actual ``evidence``
        entity (AGENTS.md data invariant).
        """
        record = await graph.get_entity(entity_id)
        return record is not None and record.type == ENTITY_EVIDENCE

    async def _artifact_known(self, artifact_id: str) -> bool:
        """True when the configured artifact store's index knows ``artifact_id``.

        Without a configured store no id can resolve as an artifact. A
        missing artifact, or an index that cannot be read (missing or
        corrupt), is treated as unresolved — the finding is still rejected
        loudly with its ids rather than silently merged.
        """
        if self._artifacts is None:
            return False
        try:
            await self._artifacts.get(artifact_id)
        except (ArtifactNotFoundError, ArtifactIndexError):
            return False
        return True

    async def _create_entity(
        self,
        graph: StateGraph,
        run_id: str,
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
                    run_id,
                    REDUCER_PRODUCER,
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
        run_id: str,
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
                    run_id,
                    REDUCER_PRODUCER,
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
        run_id: str,
        task_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Append one ``reducer.*`` run event when a log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=run_id,
                timestamp=datetime.now(UTC),
                event_type=event_type,
                producer=REDUCER_PRODUCER,
                task_id=task_id,
                worker_id=worker_id,
                payload=payload,
            )
        )
