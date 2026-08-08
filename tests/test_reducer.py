"""Tests for the reducer — validated findings merge into facts (PR26).

Covers the validation rule (every evidence reference must resolve to a
graph ``evidence`` entity or a known artifact; an unresolved reference
raises UnresolvedEvidenceError carrying the exact id(s) and rejects the
finding loudly without blocking the rest of the merge), the deterministic
fact id, the fact/edge/event persistence with replay consistency
(replaying the log reconstructs the identical graph hash — the PR20
executor pattern), idempotent and deduplicating merges, contradictory
findings merging as separate facts, failed worker runs being skipped, and
the no-event-log path.

Every test uses its own in-memory SQLite graph (``\":memory:\"``); replay
tests use a file-backed live graph plus a fresh replay database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.entities import ENTITY_EVIDENCE
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    Event,
    EventLog,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.reducer import (
    EDGE_FACT_DERIVED_FROM_EVIDENCE,
    ENTITY_FACT,
    REDUCER_FINDINGS_REJECTED,
    REDUCER_PRODUCER,
    REDUCER_RUN_COMPLETED,
    REDUCER_RUN_STARTED,
    Fact,
    Reducer,
    ReducerError,
    ReducerResult,
    UnresolvedEvidenceError,
    fact_id,
)
from ozzgraph.replay import replay_graph
from ozzgraph.scheduler import (
    Finding,
    TaskOutcome,
    WorkerRun,
    WorkerRunStatus,
    worker_run_id,
)
from ozzgraph.state_graph import StateGraph

RUN = "run-1"

# Fixed deterministic timestamps (same style as tests/test_replay.py).
T1 = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)

#: Producer used when seeding evidence entities into the event log (the
#: evidence was created by an earlier phase, not by the reducer).
EVIDENCE_PRODUCER = "executor"


def _finding(
    task_id: str,
    *,
    evidence_ids: tuple[str, ...],
    summary: str,
    confidence: float = 0.8,
    source: str = "probe",
) -> Finding:
    return Finding(
        task_id=task_id,
        source=source,
        evidence_ids=evidence_ids,
        summary=summary,
        confidence=confidence,
    )


def _run(
    run_id: str,
    task_id: str,
    findings: tuple[Finding, ...],
    *,
    status: WorkerRunStatus = WorkerRunStatus.SUCCEEDED,
    error: str | None = None,
) -> WorkerRun:
    return WorkerRun(
        id=worker_run_id(run_id, task_id),
        task_id=task_id,
        status=status,
        started_at=T1,
        finished_at=T1,
        findings=findings,
        error=error,
    )


async def _seed_evidence(
    graph: StateGraph,
    *entity_ids: str,
    log: EventLog | None = None,
) -> None:
    """Evidence entities a reduction may reference, as ``evidence`` entities.

    When a log is given, the seeding is mirrored as ``graph.*`` events (the
    same same-timestamp pattern the reducer uses) so replay reconstructs
    the endpoints before the facts that reference them.
    """
    for entity_id in entity_ids:
        await graph.create_entity(entity_id, ENTITY_EVIDENCE, {"kind": "scan"}, at=T1)
        if log is not None:
            log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    RUN,
                    EVIDENCE_PRODUCER,
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=ENTITY_EVIDENCE,
                        data={"kind": "scan"},
                        at=T1,
                    ),
                )
            )


def _read_log(log: EventLog) -> list[Event]:
    """Parse the JSONL log back into structured events."""
    events: list[Event] = []
    with log.path.open(encoding="utf-8") as handle:
        for line in handle:
            events.append(Event.model_validate_json(line))
    return events


# ---------------------------------------------------------------------------
# fact id determinism and fact schema
# ---------------------------------------------------------------------------


def test_fact_id_is_stable_and_field_order_independent() -> None:
    a = _finding("t-1", evidence_ids=("ev-2", "ev-1"), summary="port 22 open")
    b = _finding("t-1", evidence_ids=("ev-1", "ev-2"), summary="port 22 open")
    assert fact_id(a) == fact_id(b)  # evidence order never changes the id
    assert fact_id(a).startswith("fact-")
    assert len(fact_id(a)) == len("fact-") + 64  # sha256 hex digest
    assert fact_id(a) == fact_id(a)  # stable across calls
    # Any fingerprint difference changes the id.
    assert fact_id(a) != fact_id(_finding("t-1", evidence_ids=("ev-1",), summary="port 22 open"))
    assert fact_id(a) != fact_id(
        _finding("t-1", evidence_ids=("ev-1", "ev-2"), summary="port 22 closed")
    )
    assert fact_id(a) != fact_id(
        _finding("t-2", evidence_ids=("ev-1", "ev-2"), summary="port 22 open")
    )
    assert fact_id(a) != fact_id(
        _finding("t-1", evidence_ids=("ev-1", "ev-2"), summary="port 22 open", source="scanner")
    )


def test_fact_schema_valid_and_rejects_invalid_shapes() -> None:
    fact = Fact(
        id="fact-x",
        task_id="t-1",
        source="probe",
        evidence_ids=("ev-1",),
        summary="port 22 open",
        confidence=0.9,
    )
    assert fact.evidence_ids == ("ev-1",)
    assert fact.confidence == 0.9
    with pytest.raises(ValidationError):
        Fact(
            id="fact-x", task_id="t-1", source="probe", evidence_ids=(), summary="x", confidence=0.5
        )
    with pytest.raises(ValidationError):
        Fact(
            id="fact-x",
            task_id="t-1",
            source="probe",
            evidence_ids=("  ",),
            summary="x",
            confidence=0.5,
        )
    with pytest.raises(ValidationError):
        Fact(
            id="fact-x",
            task_id="t-1",
            source="probe",
            evidence_ids=("ev-1",),
            summary="",
            confidence=0.5,
        )
    with pytest.raises(ValidationError):
        Fact(
            id="fact-x",
            task_id="t-1",
            source="probe",
            evidence_ids=("ev-1",),
            summary="x",
            confidence=1.5,
        )
    with pytest.raises(ValidationError):
        Fact(
            id="fact-x",
            task_id="t-1",
            source="probe",
            evidence_ids=("ev-1",),
            summary="x",
            confidence=0.5,
            extra="forbidden",  # type: ignore[call-arg]
        )


def test_reducer_error_hierarchy() -> None:
    assert issubclass(ReducerError, RuntimeError)
    assert issubclass(UnresolvedEvidenceError, ReducerError)
    assert ReducerResult.model_config.get("extra") == "forbid"


def test_finding_without_evidence_is_rejected_through_worker_run() -> None:
    """Empty evidence_ids cannot reach the reducer (Finding validator)."""
    with pytest.raises(ValidationError, match="at least one evidence"):
        WorkerRun(
            id="worker-run-x",
            task_id="t-a",
            status=WorkerRunStatus.SUCCEEDED,
            started_at=T1,
            finished_at=T1,
            findings=(Finding(task_id="t-a", source="probe", evidence_ids=(), summary="trust me"),),
        )
    with pytest.raises(ValidationError, match="at least one evidence"):
        TaskOutcome(
            task_id="t-a",
            status=WorkerRunStatus.SUCCEEDED,
            findings=(Finding(task_id="t-a", source="probe", evidence_ids=(), summary="trust me"),),
        )


# ---------------------------------------------------------------------------
# validation: resolving evidence references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_evidence_raises_unresolved_evidence_error() -> None:
    """An unknown id raises UnresolvedEvidenceError listing the exact id(s)."""
    async with StateGraph(":memory:") as graph:
        await _seed_evidence(graph, "ev-1")
        reducer = Reducer()
        with pytest.raises(UnresolvedEvidenceError, match="ev-missing"):
            await reducer.resolve_evidence(
                graph, _finding("t-a", evidence_ids=("ev-1", "ev-missing"), summary="x")
            )
        # Only the unresolved id is listed, in deterministic sorted order.
        with pytest.raises(UnresolvedEvidenceError, match=r"\[.ev-9., .ev-x.\]"):
            await reducer.resolve_evidence(
                graph, _finding("t-a", evidence_ids=("ev-x", "ev-1", "ev-9"), summary="x")
            )


@pytest.mark.asyncio
async def test_non_evidence_entity_does_not_resolve() -> None:
    """A same-named entity of another type is not evidence (rule #3)."""
    async with StateGraph(":memory:") as graph:
        await graph.create_entity("ev-1", "observation", {"summary": "not evidence"}, at=T1)
        with pytest.raises(UnresolvedEvidenceError, match="ev-1"):
            await Reducer().resolve_evidence(
                graph, _finding("t-a", evidence_ids=("ev-1",), summary="x")
            )


@pytest.mark.asyncio
async def test_artifact_references_resolve_via_store_index(tmp_path: Path) -> None:
    """Artifact-prefixed ids resolve only through a configured store."""
    store = ArtifactStore(tmp_path / "artifacts")
    record = await store.put(source=b"nmap -sV 10.0.0.1", mime_type="text/plain")
    async with StateGraph(":memory:") as graph:
        await _seed_evidence(graph, "ev-1")
        with_store = Reducer(artifacts=store)
        resolved = await with_store.resolve_evidence(
            graph,
            _finding("t-a", evidence_ids=("ev-1", record.artifact_id), summary="x"),
        )
        assert set(resolved) == {"ev-1", record.artifact_id}
        # An artifact id unknown to the store rejects.
        with pytest.raises(UnresolvedEvidenceError, match="art-ghost"):
            await with_store.resolve_evidence(
                graph, _finding("t-a", evidence_ids=("art-ghost",), summary="x")
            )
        # Without a store, an artifact-prefixed id that is not a graph
        # evidence entity rejects.
        no_store = Reducer()
        with pytest.raises(UnresolvedEvidenceError, match=record.artifact_id):
            await no_store.resolve_evidence(
                graph, _finding("t-a", evidence_ids=(record.artifact_id,), summary="x")
            )


# ---------------------------------------------------------------------------
# merge: facts, edges, events, replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reduce_merges_facts_edges_and_replays_identically(tmp_path: Path) -> None:
    log = EventLog.for_run(tmp_path)
    good_a = _finding("t-a", evidence_ids=("ev-1",), summary="port 22 open")
    good_b = _finding("t-b", evidence_ids=("ev-2",), summary="service version 1.2.3")
    runs = (_run(RUN, "t-a", (good_a,)), _run(RUN, "t-b", (good_b,)))
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", "ev-2", log=log)
        result = await Reducer(event_log=log).reduce(live, RUN, runs)
        assert result.accepted == 2
        assert result.rejected == 0
        assert [fact.id for fact in result.facts] == sorted(fact.id for fact in result.facts)

        # One fact entity per validated finding, with the full payload.
        facts = {record.id: record for record in await live.list_entities(ENTITY_FACT)}
        assert set(facts) == {fact.id for fact in result.facts}
        for fact in result.facts:
            record = facts[fact.id]
            assert record.type == ENTITY_FACT
            assert record.data["task_id"] == fact.task_id
            assert record.data["source"] == fact.source
            assert record.data["evidence_ids"] == list(fact.evidence_ids)
            assert record.data["summary"] == fact.summary
            assert record.data["confidence"] == fact.confidence

        # One FACT DERIVED_FROM EVIDENCE edge per evidence id, fact -> evidence.
        for fact in result.facts:
            for evidence_id in fact.evidence_ids:
                edge = await live.get_edge(f"{fact.id}-derived-{evidence_id}")
                assert edge is not None
                assert edge.type == EDGE_FACT_DERIVED_FROM_EVIDENCE
                assert edge.src_id == fact.id
                assert edge.dst_id == evidence_id

        # A fact's mutation and its event share one timestamp (PR20 pattern).
        created_events = [
            event
            for event in _read_log(log)
            if event.event_type == GRAPH_ENTITY_CREATED and event.producer == REDUCER_PRODUCER
        ]
        assert len(created_events) == 2
        for event in created_events:
            entity_id = str(event.payload["entity_id"])
            assert datetime.fromisoformat(str(event.payload["at"])) == facts[entity_id].created_at

        live_hash = await live.graph_hash()

    # Replaying the event log reconstructs the identical graph hash.
    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


@pytest.mark.asyncio
async def test_reduce_emits_graph_and_run_events(tmp_path: Path) -> None:
    log = EventLog.for_run(tmp_path)
    finding = _finding("t-a", evidence_ids=("ev-1",), summary="port 22 open")
    async with StateGraph(":memory:") as graph:
        await _seed_evidence(graph, "ev-1")
        result = await Reducer(event_log=log).reduce(graph, RUN, (_run(RUN, "t-a", (finding,)),))
    assert result.accepted == 1
    events = _read_log(log)
    assert all(event.producer == REDUCER_PRODUCER for event in events)
    graph_events = [event for event in events if event.event_type.startswith("graph.")]
    assert len(graph_events) == 2  # 1 fact entity + 1 derived edge
    assert {event.event_type for event in graph_events} == {
        GRAPH_ENTITY_CREATED,
        GRAPH_EDGE_CREATED,
    }
    types = [event.event_type for event in events]
    assert types.count(REDUCER_RUN_STARTED) == 1
    assert types.count(REDUCER_RUN_COMPLETED) == 1
    assert types.count(REDUCER_FINDINGS_REJECTED) == 0
    completed = next(event for event in events if event.event_type == REDUCER_RUN_COMPLETED)
    assert completed.payload == {"accepted": 1, "rejected": 0}


# ---------------------------------------------------------------------------
# rejection: unresolved evidence never merges, other findings still do
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_evidence_rejects_loudly_and_others_merge(tmp_path: Path) -> None:
    log = EventLog.for_run(tmp_path)
    good = _finding("t-a", evidence_ids=("ev-1",), summary="port 22 open")
    bad = _finding("t-b", evidence_ids=("ev-missing",), summary="trust me")
    runs = (_run(RUN, "t-a", (good,)), _run(RUN, "t-b", (bad,)))
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        result = await Reducer(event_log=log).reduce(live, RUN, runs)
        assert result.accepted == 1
        assert result.rejected == 1
        assert [fact.id for fact in result.facts] == [fact_id(good)]
        # Nothing was written for the rejected finding (rule #3).
        assert await live.get_entity(fact_id(bad)) is None
        assert len(await live.list_entities(ENTITY_FACT)) == 1
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash

    events = _read_log(log)
    rejected_events = [event for event in events if event.event_type == REDUCER_FINDINGS_REJECTED]
    assert len(rejected_events) == 1
    rejection = rejected_events[0]
    assert rejection.task_id == "t-b"
    assert rejection.worker_id == worker_run_id(RUN, "t-b")
    assert rejection.payload["worker_run_id"] == worker_run_id(RUN, "t-b")
    assert rejection.payload["evidence_ids"] == ["ev-missing"]
    # The exact unresolved id appears in the error message (fail loudly).
    assert "ev-missing" in str(rejection.payload["error"])
    assert isinstance(rejection.payload["error"], str)
    assert "t-b" in rejection.payload["error"]


# ---------------------------------------------------------------------------
# idempotency, dedupe, contradictions, failed runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reduce_is_idempotent(tmp_path: Path) -> None:
    """Reducing the same runs twice writes nothing new (same graph hash)."""
    log = EventLog.for_run(tmp_path)
    runs = (_run(RUN, "t-a", (_finding("t-a", evidence_ids=("ev-1",), summary="port 22 open"),)),)
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        reducer = Reducer(event_log=log)
        first = await reducer.reduce(live, RUN, runs)
        graph_events_after_first = sum(
            1 for event in _read_log(log) if event.event_type.startswith("graph.")
        )
        hash_after_first = await live.graph_hash()

        second = await reducer.reduce(live, RUN, runs)
        assert second.accepted == 1
        assert second.rejected == 0
        assert second.facts == first.facts
        assert await live.graph_hash() == hash_after_first
        assert len(await live.list_entities(ENTITY_FACT)) == 1
        # No new graph mutations were emitted for the no-op merge (only the
        # reducer.* run events, which replay ignores).
        assert (
            sum(1 for event in _read_log(log) if event.event_type.startswith("graph."))
            == graph_events_after_first
        )


@pytest.mark.asyncio
async def test_identical_findings_dedupe_to_one_fact(tmp_path: Path) -> None:
    """Two runs carrying identical findings produce one fact, not two."""
    log = EventLog.for_run(tmp_path)
    finding = _finding("t-a", evidence_ids=("ev-1",), summary="port 22 open")
    runs = (
        WorkerRun(
            id="worker-run-1",
            task_id="t-a",
            status=WorkerRunStatus.SUCCEEDED,
            started_at=T1,
            finished_at=T1,
            findings=(finding,),
        ),
        WorkerRun(
            id="worker-run-2",
            task_id="t-a",
            status=WorkerRunStatus.SUCCEEDED,
            started_at=T1,
            finished_at=T1,
            findings=(finding,),
        ),
    )
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        result = await Reducer(event_log=log).reduce(live, RUN, runs)
        assert result.accepted == 1
        assert len(result.facts) == 1
        assert len(await live.list_entities(ENTITY_FACT)) == 1
        neighbors = await live.neighbors(fact_id(finding))
        assert len(neighbors.outgoing) == 1  # one derived edge, not two
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


@pytest.mark.asyncio
async def test_contradictory_findings_merge_as_separate_facts(tmp_path: Path) -> None:
    """Same evidence, different summaries: both facts merge with provenance."""
    log = EventLog.for_run(tmp_path)
    runs = (
        _run(
            RUN,
            "t-a",
            (_finding("t-a", evidence_ids=("ev-1",), summary="port 22 open", confidence=0.8),),
        ),
        _run(
            RUN,
            "t-b",
            (_finding("t-b", evidence_ids=("ev-1",), summary="port 22 closed", confidence=0.6),),
        ),
    )
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        result = await Reducer(event_log=log).reduce(live, RUN, runs)
        assert result.accepted == 2
        assert len(result.facts) == 2
        assert len({fact.id for fact in result.facts}) == 2
        assert len(await live.list_entities(ENTITY_FACT)) == 2
        # Both facts derive from the same evidence entity.
        neighbors = await live.neighbors("ev-1")
        assert len(neighbors.incoming) == 2
        assert {edge.type for edge in neighbors.incoming} == {EDGE_FACT_DERIVED_FROM_EVIDENCE}
        assert {edge.src_id for edge in neighbors.incoming} == {fact.id for fact in result.facts}
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


@pytest.mark.asyncio
async def test_failed_worker_run_is_skipped_without_error(tmp_path: Path) -> None:
    """A failed run carries no findings; reduce() skips it, never errors."""
    log = EventLog.for_run(tmp_path)
    good = _finding("t-ok", evidence_ids=("ev-1",), summary="port 22 open")
    runs = (
        _run(RUN, "t-fail", (), status=WorkerRunStatus.FAILED, error="command timed out"),
        _run(RUN, "t-ok", (good,)),
    )
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        result = await Reducer(event_log=log).reduce(live, RUN, runs)
        assert result.rejected == 0
        assert result.accepted == 1
        assert [fact.id for fact in result.facts] == [fact_id(good)]
        assert len(await live.list_entities(ENTITY_FACT)) == 1
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


@pytest.mark.asyncio
async def test_artifact_only_reference_merges_without_edge(tmp_path: Path) -> None:
    """An artifact-store-only reference merges; no edge to a non-graph id.

    The graph's foreign keys require both edge endpoints to exist, so a
    reference that resolves via the artifact store but has no graph
    entity under that id stays in the fact payload as provenance (no
    edge, no error) — the merge remains deterministic and replayable.
    """
    log = EventLog.for_run(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    record = await store.put(source=b"nmap -sV 10.0.0.1", mime_type="text/plain")
    finding = _finding("t-a", evidence_ids=("ev-1", record.artifact_id), summary="scanned")
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        result = await Reducer(event_log=log, artifacts=store).reduce(
            live, RUN, (_run(RUN, "t-a", (finding,)),)
        )
        assert result.accepted == 1
        assert result.rejected == 0
        fact = await live.get_entity(fact_id(finding))
        assert fact is not None
        assert fact.data["evidence_ids"] == ["ev-1", record.artifact_id]
        # Only the graph-resident reference got an edge.
        assert await live.get_edge(f"{fact_id(finding)}-derived-ev-1") is not None
        assert await live.get_edge(f"{fact_id(finding)}-derived-{record.artifact_id}") is None
        assert len((await live.neighbors(fact_id(finding))).outgoing) == 1
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


# ---------------------------------------------------------------------------
# determinism and the no-event-log path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facts_order_is_independent_of_run_order(tmp_path: Path) -> None:
    """The result's facts are in deterministic fact-id order."""
    runs = (
        _run(RUN, "t-b", (_finding("t-b", evidence_ids=("ev-2",), summary="bbb"),)),
        _run(RUN, "t-a", (_finding("t-a", evidence_ids=("ev-1",), summary="aaa"),)),
    )
    async with StateGraph(":memory:") as first_graph:
        await _seed_evidence(first_graph, "ev-1", "ev-2")
        first = await Reducer().reduce(first_graph, RUN, runs)
    async with StateGraph(":memory:") as second_graph:
        await _seed_evidence(second_graph, "ev-1", "ev-2")
        second = await Reducer().reduce(second_graph, RUN, tuple(reversed(runs)))
    assert first.accepted == second.accepted == 2
    assert first.rejected == second.rejected == 0
    assert first.facts == second.facts
    assert first.facts == tuple(sorted(first.facts, key=lambda fact: fact.id))


@pytest.mark.asyncio
async def test_reduce_without_event_log_still_persists_graph(tmp_path: Path) -> None:
    """No log configured: the graph still records the merged state."""
    finding = _finding("t-a", evidence_ids=("ev-1",), summary="port 22 open")
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1")
        result = await Reducer().reduce(live, RUN, (_run(RUN, "t-a", (finding,)),))
        assert result.accepted == 1
        assert result.rejected == 0
        facts = await live.list_entities(ENTITY_FACT)
        assert len(facts) == 1
        assert facts[0].id == fact_id(finding)
        edges = (await live.neighbors(fact_id(finding))).outgoing
        assert len(edges) == 1
        assert edges[0].type == EDGE_FACT_DERIVED_FROM_EVIDENCE
        assert edges[0].dst_id == "ev-1"
    # Nothing was written to a log file.
    assert not (tmp_path / "actions.jsonl").exists()


@pytest.mark.asyncio
async def test_reduce_rejects_empty_run_id() -> None:
    async with StateGraph(":memory:") as graph:
        with pytest.raises(ValueError, match="run_id"):
            await Reducer().reduce(graph, "", ())


# ---------------------------------------------------------------------------
# V07: structured verdicts merge into facts (verdict + impact payload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_finding_merges_into_structured_fact(tmp_path: Path) -> None:
    """A finding carrying verdict + impact merges as a structured fact."""
    log = EventLog.for_run(tmp_path)
    finding = Finding(
        task_id="t-a",
        source="micro-agent",
        evidence_ids=("ev-1",),
        summary="micro agent confirmed: 1 experiment(s), evidence: ev-1",
        confidence=0.7,
        verdict="confirmed",
        impact={
            "cwe": "CWE-200: Exposure of Sensitive Information",
            "assets": ("target-a",),
            "confidence": 0.7,
        },
    )
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_evidence(live, "ev-1", log=log)
        result = await Reducer(event_log=log).reduce(live, RUN, (_run(RUN, "t-a", (finding,)),))
        assert result.accepted == 1
        assert result.rejected == 0
        assert len(result.facts) == 1
        fact = result.facts[0]
        # The merged fact IS the structured verdict.
        assert fact.verdict == "confirmed"
        assert fact.impact == {
            "cwe": "CWE-200: Exposure of Sensitive Information",
            "assets": ("target-a",),
            "confidence": 0.7,
        }
        # The graph payload carries the verdict + impact.
        record = await live.get_entity(fact.id)
        assert record is not None
        assert record.data["verdict"] == "confirmed"
        assert record.data["impact"] == {
            "cwe": "CWE-200: Exposure of Sensitive Information",
            "assets": ["target-a"],
            "confidence": 0.7,
        }
        live_hash = await live.graph_hash()
    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash


def test_verdict_changes_the_fact_id() -> None:
    """Same evidence + summary, different verdicts: two distinct facts."""
    base = {
        "task_id": "t-a",
        "source": "micro-agent",
        "evidence_ids": ("ev-1",),
        "summary": "same summary",
    }
    confirmed = Finding(**base, verdict="confirmed")  # type: ignore[arg-type]
    refuted = Finding(**base, verdict="refuted")  # type: ignore[arg-type]
    assert fact_id(confirmed) != fact_id(refuted)
    # An impact payload also changes the fingerprint.
    with_impact = Finding(
        **base, verdict="confirmed", impact={"cwe": None, "assets": ("t",), "confidence": 0.7}
    )  # type: ignore[arg-type]
    assert fact_id(confirmed) != fact_id(with_impact)
    # The same verdict + impact on the same evidence stays one fact.
    again = Finding(
        **base, verdict="confirmed", impact={"cwe": None, "assets": ("t",), "confidence": 0.7}
    )  # type: ignore[arg-type]
    assert fact_id(with_impact) == fact_id(again)


def test_fact_schema_rejects_invalid_verdict_and_impact() -> None:
    base = {
        "id": "fact-x",
        "task_id": "t-a",
        "source": "probe",
        "evidence_ids": ("ev-1",),
        "summary": "x",
        "confidence": 0.5,
    }
    with pytest.raises(ValidationError):
        Fact(**base, verdict="maybe")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="missing"):
        Fact(**base, impact={"cwe": None})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="confidence"):
        Fact(**base, impact={"cwe": None, "assets": (), "confidence": 2.0})  # type: ignore[arg-type]
