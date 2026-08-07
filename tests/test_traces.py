"""Tests for golden traces (PR28): capture, deterministic replay verify.

Covers the docs/TESTING_AND_QA.md "Golden Traces" contract: a trace
captures a run's challenge input, model responses, tool outputs,
expected graph events, expected final graph, and expected metrics;
verification replays the events through ozzgraph.replay and compares
the reconstructed entity set, edge set, and graph hash plus the metrics
— reporting a structured diff on any mismatch (prompt-regression
visibility), and failing loudly on corrupt traces.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.state_graph import StateGraph
from ozzgraph.traces import (
    TRACE_FORMAT,
    TraceChallenge,
    TraceError,
    TraceMetrics,
    TraceToolOutput,
    TraceVerificationError,
    capture_trace,
    load_trace,
    verify_metrics,
    verify_trace,
)

RUN = "run-trace"
T1 = datetime(2026, 8, 7, 9, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 7, 9, 1, 0, tzinfo=UTC)

CHALLENGE = TraceChallenge(
    target_name="hidden-routes",
    target_category="hidden routes",
    target_description="robots.txt advertises /admin; only /admin holds the flag",
    flag_pattern=r"OZ\{[^{}\s]+\}",
)

METRICS = TraceMetrics(
    valid_output_rate=1.0,
    correct_tool_selection=1.0,
    repetition_rate=0.0,
    recovery_rate=1.0,
    output_tokens_per_decision=42.0,
    steps_per_objective=3.0,
    solve_rate=1.0,
    unsupported_fact_rate=0.0,
    unsupported_flag_rate=0.0,
)

MODEL_RESPONSES = [
    "probe the root",
    '{"kind": "think", "rationale": "robots.txt points at /admin"}',
    '{"kind": "run", "payload": "curl -sS --max-time 5 http://127.0.0.1:1/admin"}',
]

TOOL_OUTPUTS = [
    TraceToolOutput(
        turn=3,
        command="curl -sS --max-time 5 http://127.0.0.1:1/admin",
        exit_code=0,
        stdout="OZ{lab-hidden-routes-0123456789}",
        stderr="",
        timeout_state=False,
    )
]


async def _seed_run(tmp_path: Path) -> tuple[Path, Path]:
    """Seed a small deterministic run: an event log plus a matching live graph.

    Three graph events (two entity creations and one edge creation)
    mirrored one-for-one onto the live graph, exactly like the PR7/PR8
    pattern: replaying the log must reconstruct the same graph.
    """
    log = EventLog.for_run(tmp_path)
    db = tmp_path / "live.db"
    async with StateGraph(db) as graph:
        await graph.create_entity("run-1", "run", {"phase": "RECON"}, at=T1)
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(
                    entity_id="run-1", entity_type="run", data={"phase": "RECON"}, at=T1
                ),
            )
        )
        await graph.create_entity("svc-1", "service", {"port": 80}, at=T1)
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(
                    entity_id="svc-1", entity_type="service", data={"port": 80}, at=T1
                ),
            )
        )
        await graph.create_edge("edge-1", "OBSERVED_ON", "svc-1", "run-1", {"probe": "curl"}, at=T2)
        log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                RUN,
                "supervisor",
                GraphEdgeCreated(
                    edge_id="edge-1",
                    edge_type="OBSERVED_ON",
                    src_id="svc-1",
                    dst_id="run-1",
                    data={"probe": "curl"},
                    at=T2,
                ),
            )
        )
    return log.path, db


def _mutate_trace(trace_path: Path, mutator: Callable[[dict[str, object]], None]) -> None:
    """Load the trace JSON, apply ``mutator``, and write it back."""
    document = json.loads(trace_path.read_text(encoding="utf-8"))
    mutator(document)
    trace_path.write_text(json.dumps(document), encoding="utf-8")


# ---------------------------------------------------------------------------
# capture -> verify round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_verify_round_trip_is_identical(tmp_path: Path) -> None:
    """A captured trace verifies ok: replay reconstructs the expected graph
    and the supplied metrics match exactly."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"

    async with StateGraph(db) as graph:
        trace = await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
            model_responses=MODEL_RESPONSES,
            tool_outputs=TOOL_OUTPUTS,
        )

    assert trace.format == TRACE_FORMAT
    assert trace.format_version == 1
    assert trace.schema_version == 2
    assert len(trace.events) == 3
    assert trace.challenge.target_name == "hidden-routes"

    verification = await verify_trace(trace_path, actual_metrics=METRICS)
    assert verification.ok, verification.mismatches
    assert verification.mismatches == []
    assert verification.replayed_hash == trace.expected_graph.graph_hash
    assert verification.replayed_schema_version == trace.schema_version

    # The full document is persisted: challenge input, model responses,
    # tool outputs, events, expected graph, expected metrics.
    loaded = load_trace(trace_path)
    assert loaded.model_responses == MODEL_RESPONSES
    assert loaded.tool_outputs == TOOL_OUTPUTS
    assert loaded.events == trace.events
    assert loaded.expected_metrics == METRICS
    assert loaded.expected_graph.graph_hash == trace.expected_graph.graph_hash


@pytest.mark.asyncio
async def test_replay_of_trace_reproduces_expected_final_graph(tmp_path: Path) -> None:
    """The replayed database holds exactly the expected entity/edge set."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"

    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    replayed_db = tmp_path / "replayed.db"
    verification = await verify_trace(trace_path, db_path=replayed_db, actual_metrics=METRICS)
    assert verification.ok, verification.mismatches

    async with StateGraph(replayed_db) as replayed:
        entities = await replayed.list_entities()
        edges = await replayed.list_edges()
        assert [entity.id for entity in entities] == ["run-1", "svc-1"]
        assert [entity.type for entity in entities] == ["run", "service"]
        assert [edge.id for edge in edges] == ["edge-1"]
        edge = await replayed.get_edge("edge-1")
        assert edge is not None
        assert edge.src_id == "svc-1"
        assert edge.dst_id == "run-1"
        assert edge.data == {"probe": "curl"}


@pytest.mark.asyncio
async def test_capture_is_byte_deterministic(tmp_path: Path) -> None:
    """Capturing the same run twice produces byte-identical trace files."""
    events_path, db = await _seed_run(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    async with StateGraph(db) as graph:
        await capture_trace(
            first,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )
        await capture_trace(
            second,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    assert first.read_bytes() == second.read_bytes()


def test_verify_metrics_exact_equality_contract() -> None:
    """Metric comparison is exact float equality, per field."""
    assert verify_metrics(METRICS, METRICS) == []
    diff = verify_metrics(METRICS, METRICS.model_copy(update={"recovery_rate": 0.5}))
    assert len(diff) == 1
    assert diff[0].path == "expected_metrics.recovery_rate"
    assert diff[0].kind == "metric"


# ---------------------------------------------------------------------------
# regression detection (prompt-regression visibility)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_flags_metric_regression(tmp_path: Path) -> None:
    """A mutated expected metric is reported as a structured mismatch."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"
    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    _mutate_trace(trace_path, lambda doc: doc["expected_metrics"].update({"solve_rate": 0.0}))
    verification = await verify_trace(trace_path, actual_metrics=METRICS)
    assert not verification.ok
    assert {mismatch.kind for mismatch in verification.mismatches} == {"metric"}
    mismatch = verification.mismatches[0]
    assert mismatch.path == "expected_metrics.solve_rate"
    assert "expected 0.0" in mismatch.detail
    assert "replayed 1.0" in mismatch.detail

    with pytest.raises(TraceVerificationError, match="golden trace mismatch"):
        verification.assert_ok()


@pytest.mark.asyncio
async def test_verify_flags_entity_regression(tmp_path: Path) -> None:
    """A mutated expected entity payload is reported (entity_set + hash)."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"
    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    _mutate_trace(
        trace_path,
        lambda doc: doc["expected_graph"]["entities"][0]["data"].update({"phase": "DONE"}),
    )
    verification = await verify_trace(trace_path, actual_metrics=METRICS)
    assert not verification.ok
    # The events still replay the ORIGINAL graph, so the original hash still
    # matches: the diff pinpoints exactly the expectation that regressed.
    assert {mismatch.kind for mismatch in verification.mismatches} == {"entity_set"}
    entity_mismatch = verification.mismatches[0]
    assert entity_mismatch.path == "expected_graph.entities"
    assert "changed entities: run-1" in entity_mismatch.detail


@pytest.mark.asyncio
async def test_verify_flags_graph_hash_regression(tmp_path: Path) -> None:
    """A mutated expected graph hash is reported as a hash mismatch."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"
    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    _mutate_trace(trace_path, lambda doc: doc["expected_graph"].update({"graph_hash": "0" * 64}))
    verification = await verify_trace(trace_path, actual_metrics=METRICS)
    assert not verification.ok
    assert {mismatch.kind for mismatch in verification.mismatches} == {"hash"}
    assert verification.mismatches[0].path == "expected_graph.graph_hash"


@pytest.mark.asyncio
async def test_verify_flags_dropped_event_regression(tmp_path: Path) -> None:
    """A dropped event changes the replayed graph (edge_set + hash)."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"
    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    # Drop the edge-creation event: replay reconstructs no edges.
    _mutate_trace(trace_path, lambda doc: doc["events"].pop(2))
    verification = await verify_trace(trace_path, actual_metrics=METRICS)
    assert not verification.ok
    kinds = {mismatch.kind for mismatch in verification.mismatches}
    assert {"edge_set", "hash"} <= kinds
    edge_mismatch = next(
        mismatch for mismatch in verification.mismatches if mismatch.kind == "edge_set"
    )
    assert "missing edges: edge-1" in edge_mismatch.detail


@pytest.mark.asyncio
async def test_verify_flags_schema_migration(tmp_path: Path) -> None:
    """A trace captured under another schema version is flagged as schema."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"
    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    _mutate_trace(trace_path, lambda doc: doc.update({"schema_version": 1}))
    verification = await verify_trace(trace_path, actual_metrics=METRICS)
    assert not verification.ok
    assert {mismatch.kind for mismatch in verification.mismatches} == {"schema"}
    assert verification.mismatches[0].path == "schema_version"
    assert "schema migration since capture" in verification.mismatches[0].detail


# ---------------------------------------------------------------------------
# failure paths (fail loudly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_malformed_trace_fails_loudly(tmp_path: Path) -> None:
    """A corrupt trace document raises TraceError, never a silent pass."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TraceError, match="not a valid golden trace"):
        await verify_trace(bad)

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"format": "nope"}), encoding="utf-8")
    with pytest.raises(TraceError, match="not a valid golden trace"):
        await verify_trace(wrong_shape)


@pytest.mark.asyncio
async def test_verify_events_failing_replay_raises(tmp_path: Path) -> None:
    """A trace whose events fail replay raises TraceError (fail loudly)."""
    events_path, db = await _seed_run(tmp_path)
    trace_path = tmp_path / "trace.json"
    async with StateGraph(db) as graph:
        await capture_trace(
            trace_path,
            events_path=events_path,
            graph=graph,
            challenge=CHALLENGE,
            metrics=METRICS,
        )

    # Append a graph event with a missing required field: replay must abort.
    _mutate_trace(
        trace_path,
        lambda doc: doc["events"].append(
            {"event_type": "graph.entity_created", "payload": {"entity_id": "x"}}
        ),
    )
    with pytest.raises(TraceError, match="events fail replay"):
        await verify_trace(trace_path)


@pytest.mark.asyncio
async def test_capture_malformed_event_log_fails_loudly(tmp_path: Path) -> None:
    """Capture refuses to embed a malformed event log line."""
    events_path, db = await _seed_run(tmp_path)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    async with StateGraph(db) as graph:
        with pytest.raises(TraceError, match="not valid JSON"):
            await capture_trace(
                tmp_path / "trace.json",
                events_path=events_path,
                graph=graph,
                challenge=CHALLENGE,
                metrics=METRICS,
            )


def test_verify_metrics_round_trip_matches_matrix_contract() -> None:
    """TraceMetrics carries exactly the nine documented matrix metrics."""
    fields = set(TraceMetrics.model_fields)
    assert fields == {
        "valid_output_rate",
        "correct_tool_selection",
        "repetition_rate",
        "recovery_rate",
        "output_tokens_per_decision",
        "steps_per_objective",
        "solve_rate",
        "unsupported_fact_rate",
        "unsupported_flag_rate",
    }
