"""Tests for deterministic graph replay (PR8).

Covers the AGENTS.md replay invariant (replaying all events
reconstructs the same graph hash), update/delete replay parity, ignoring
non-graph events, malformed-event failures, the empty-log hash, and the
graph event payload/helper contracts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.environments.halctf import FlagCandidateExtractor, SubmissionCoordinator
from ozzgraph.events import (
    BOOTSTRAP,
    GRAPH_EDGE_CREATED,
    GRAPH_EDGE_DELETED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_DELETED,
    GRAPH_ENTITY_UPDATED,
    TERMINATION,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEdgeDeleted,
    GraphEntityCreated,
    GraphEntityDeleted,
    GraphEntityUpdated,
    graph_event,
)
from ozzgraph.hal_client import SubmissionResult
from ozzgraph.replay import (
    GRAPH_EVENT_TYPES,
    ReplayMalformedEventError,
    replay_graph,
    replay_into,
)
from ozzgraph.state_graph import StateGraph

# Stable digest of the canonical empty graph (sha256 of the
# "schema_version=2" header with no entity or edge lines), matching
# tests/test_state_graph.py.
EMPTY_GRAPH_HASH = "fc0b406e171ac834601bceea6d3edd8e0ecfedefbe465ad289f3d7b1c184fea9"

RUN = "run-1"
T1 = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 6, 10, 1, 0, tzinfo=UTC)
T3 = datetime(2026, 8, 6, 10, 2, 0, tzinfo=UTC)


def test_graph_event_types_are_exactly_the_five_constants() -> None:
    """Replay applies exactly the five graph mutation event types."""
    assert GRAPH_EVENT_TYPES == frozenset(
        {
            GRAPH_ENTITY_CREATED,
            GRAPH_ENTITY_UPDATED,
            GRAPH_ENTITY_DELETED,
            GRAPH_EDGE_CREATED,
            GRAPH_EDGE_DELETED,
        }
    )


def test_graph_event_timestamp_derived_from_payload_at() -> None:
    """graph_event stamps the Event with the payload's ``at`` timestamp."""
    event = graph_event(
        GRAPH_ENTITY_CREATED,
        RUN,
        "supervisor",
        GraphEntityCreated(entity_id="e1", entity_type="node", data={"v": 1}, at=T1),
    )
    assert event.timestamp == T1
    assert event.event_type == GRAPH_ENTITY_CREATED
    assert event.payload == {
        "entity_id": "e1",
        "entity_type": "node",
        "data": {"v": 1},
        "at": "2026-08-06T10:00:00Z",
    }


def test_graph_payload_naive_timestamp_rejected() -> None:
    """Graph mutation payloads reject naive ``at`` timestamps loudly."""
    with pytest.raises(ValidationError):
        GraphEntityCreated(
            entity_id="e1",
            entity_type="node",
            at=datetime(2026, 8, 6, 10, 0, 0),  # noqa: DTZ001 - deliberately naive
        )


@pytest.mark.asyncio
async def test_replay_reconstructs_identical_graph(tmp_path: Path) -> None:
    """Entity/edge creation events replay to the same hash AND the same
    reconstructed state (get_entity/get_edge spot checks)."""
    log = EventLog.for_run(tmp_path)
    log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            RUN,
            "supervisor",
            GraphEntityCreated(entity_id="svc-1", entity_type="service", data={"port": 80}, at=T1),
        )
    )
    log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            RUN,
            "supervisor",
            GraphEntityCreated(entity_id="tgt-1", entity_type="target", at=T1),
        )
    )
    log.append(
        graph_event(
            GRAPH_EDGE_CREATED,
            RUN,
            "supervisor",
            GraphEdgeCreated(
                edge_id="edge-1",
                edge_type="OBSERVED_ON",
                src_id="svc-1",
                dst_id="tgt-1",
                data={"probe": "nmap"},
                at=T2,
            ),
        )
    )

    async with StateGraph(tmp_path / "live.db") as live:
        await live.create_entity("svc-1", "service", {"port": 80}, at=T1)
        await live.create_entity("tgt-1", "target", {}, at=T1)
        await live.create_edge("edge-1", "OBSERVED_ON", "svc-1", "tgt-1", {"probe": "nmap"}, at=T2)
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash

    replayed = await replay_into(log.path, tmp_path / "replay2.db")
    try:
        assert await replayed.graph_hash() == live_hash
        svc = await replayed.get_entity("svc-1")
        assert svc is not None
        assert svc.data == {"port": 80}
        assert svc.created_at == T1
        assert svc.updated_at == T1
        assert await replayed.get_entity("tgt-1") is not None
        edge = await replayed.get_edge("edge-1")
        assert edge is not None
        assert edge.src_id == "svc-1"
        assert edge.dst_id == "tgt-1"
        assert edge.created_at == T2
    finally:
        await replayed.close()


@pytest.mark.asyncio
async def test_replay_update_and_delete_identical_hash(tmp_path: Path) -> None:
    """Update and delete events replay to the live graph's final hash."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(tmp_path / "live.db") as live:
        await live.create_entity("e1", "node", {"v": 1}, at=T1)
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(entity_id="e1", entity_type="node", data={"v": 1}, at=T1),
            )
        )
        await live.update_entity("e1", {"v": 2}, at=T2)
        log.append(
            graph_event(
                GRAPH_ENTITY_UPDATED,
                RUN,
                "supervisor",
                GraphEntityUpdated(entity_id="e1", data={"v": 2}, at=T2),
            )
        )
        await live.create_entity("e2", "node", {}, at=T1)
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(entity_id="e2", entity_type="node", at=T1),
            )
        )
        await live.create_edge("edge-1", "links", "e1", "e2", at=T3)
        log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                RUN,
                "supervisor",
                GraphEdgeCreated(
                    edge_id="edge-1", edge_type="links", src_id="e1", dst_id="e2", at=T3
                ),
            )
        )
        await live.delete_edge("edge-1")
        log.append(
            graph_event(
                GRAPH_EDGE_DELETED,
                RUN,
                "supervisor",
                GraphEdgeDeleted(edge_id="edge-1"),
            )
        )
        await live.delete_entity("e1")
        log.append(
            graph_event(
                GRAPH_ENTITY_DELETED,
                RUN,
                "supervisor",
                GraphEntityDeleted(entity_id="e1"),
            )
        )
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash

    replayed = await replay_into(log.path, tmp_path / "replay2.db")
    try:
        assert await replayed.get_entity("e1") is None
        assert await replayed.get_edge("edge-1") is None
        remaining = await replayed.get_entity("e2")
        assert remaining is not None
        assert remaining.created_at == T1
        assert remaining.updated_at == T1
    finally:
        await replayed.close()


@pytest.mark.asyncio
async def test_replay_ignores_non_graph_events(tmp_path: Path) -> None:
    """Bootstrap, termination, and unknown future event types are ignored."""
    log = EventLog.for_run(tmp_path)
    log.append(
        Event(
            run_id=RUN,
            timestamp=T1,
            event_type=BOOTSTRAP,
            producer="supervisor",
            payload={"note": "start"},
        )
    )
    log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            RUN,
            "supervisor",
            GraphEntityCreated(entity_id="e1", entity_type="node", at=T1),
        )
    )
    # A future, unknown event type (even with a weird payload) must not
    # break replay.
    log.append(
        Event(
            run_id=RUN,
            timestamp=T2,
            event_type="graph.phase_changed",
            producer="supervisor",
            payload={"phase": "RECON"},
        )
    )
    log.append(
        Event(
            run_id=RUN,
            timestamp=T2,
            event_type=TERMINATION,
            producer="supervisor",
            payload={"reason": "interrupted"},
        )
    )

    replayed = await replay_into(log.path, tmp_path / "replay.db")
    try:
        entity = await replayed.get_entity("e1")
        assert entity is not None
        assert entity.created_at == T1
        assert len(await replayed.list_entities()) == 1
    finally:
        await replayed.close()


@pytest.mark.asyncio
async def test_replay_malformed_json_line_raises(tmp_path: Path) -> None:
    """A non-JSON line is a loud ReplayMalformedEventError."""
    path = tmp_path / "actions.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")
    with pytest.raises(ReplayMalformedEventError):
        await replay_graph(path, tmp_path / "replay.db")


@pytest.mark.asyncio
async def test_replay_non_object_event_raises(tmp_path: Path) -> None:
    """A JSON line that is not an object is malformed."""
    path = tmp_path / "actions.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ReplayMalformedEventError):
        await replay_graph(path, tmp_path / "replay.db")


@pytest.mark.asyncio
async def test_replay_missing_field_raises(tmp_path: Path) -> None:
    """A graph event missing required payload fields is malformed."""
    path = tmp_path / "actions.jsonl"
    path.write_text(
        json.dumps({"event_type": GRAPH_ENTITY_CREATED, "payload": {"entity_id": "e1"}}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayMalformedEventError):
        await replay_graph(path, tmp_path / "replay.db")


@pytest.mark.asyncio
async def test_replay_naive_timestamp_raises(tmp_path: Path) -> None:
    """A graph event carrying a naive timestamp is malformed."""
    path = tmp_path / "actions.jsonl"
    line = json.dumps(
        {
            "event_type": GRAPH_ENTITY_CREATED,
            "payload": {
                "entity_id": "e1",
                "entity_type": "node",
                "data": {},
                "at": "2026-08-06T10:00:00",
            },
        }
    )
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ReplayMalformedEventError):
        await replay_graph(path, tmp_path / "replay.db")


@pytest.mark.asyncio
async def test_replay_non_object_payload_raises(tmp_path: Path) -> None:
    """A graph event whose payload is not an object is malformed."""
    path = tmp_path / "actions.jsonl"
    path.write_text(
        json.dumps({"event_type": GRAPH_ENTITY_CREATED, "payload": [1, 2]}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayMalformedEventError):
        await replay_graph(path, tmp_path / "replay.db")


@pytest.mark.asyncio
async def test_replay_empty_log_returns_empty_hash(tmp_path: Path) -> None:
    """An empty (zero-byte) log replays to the stable empty-graph hash."""
    path = tmp_path / "actions.jsonl"
    path.write_text("", encoding="utf-8")
    assert await replay_graph(path, tmp_path / "replay.db") == EMPTY_GRAPH_HASH


@pytest.mark.asyncio
async def test_replay_graph_free_log_returns_empty_hash(tmp_path: Path) -> None:
    """A log with only bootstrap/termination events replays to the
    stable empty-graph hash."""
    log = EventLog.for_run(tmp_path)
    log.append(Event(run_id=RUN, timestamp=T1, event_type=BOOTSTRAP, producer="supervisor"))
    log.append(
        Event(
            run_id=RUN,
            timestamp=T2,
            event_type=TERMINATION,
            producer="supervisor",
            payload={"reason": "interrupted"},
        )
    )
    assert await replay_graph(log.path, tmp_path / "replay.db") == EMPTY_GRAPH_HASH


@pytest.mark.asyncio
async def test_replay_after_flag_candidate_and_submission(tmp_path: Path) -> None:
    """PR22 invariant: extractor + coordinator mutations replay identically.

    The flag candidate extractor (PR22) persists the candidate entity
    and its OBSERVED_IN edges, and the submission coordinator persists
    the submission entity, its SUBMITS edge, and the rejected marker —
    every mutation mirrored as a graph.* event sharing one timestamp, so
    replaying the log reconstructs the identical graph hash.
    """
    log = EventLog.for_run(tmp_path)

    class _AcceptedClient:
        """Minimal privileged submit surface for the coordinator."""

        privileged = True

        async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
            return SubmissionResult(
                challenge_id=challenge_id, accepted=True, message="ok", points=50
            )

    async with StateGraph(tmp_path / "live.db") as live:
        # Seed the observation + evidence (mirrored as graph events).
        await live.create_entity(
            "obs-1", "observation", {"summary": "found flag{replay-42}"}, at=T1
        )
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(
                    entity_id="obs-1",
                    entity_type="observation",
                    data={"summary": "found flag{replay-42}"},
                    at=T1,
                ),
            )
        )
        await live.create_entity("evidence-1", "evidence", {}, at=T1)
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(entity_id="evidence-1", entity_type="evidence", at=T1),
            )
        )
        await live.create_edge(
            "evidence-1-extracted-obs-1",
            "EVIDENCE EXTRACTED_FROM OBSERVATION",
            "evidence-1",
            "obs-1",
            at=T2,
        )
        log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                RUN,
                "supervisor",
                GraphEdgeCreated(
                    edge_id="evidence-1-extracted-obs-1",
                    edge_type="EVIDENCE EXTRACTED_FROM OBSERVATION",
                    src_id="evidence-1",
                    dst_id="obs-1",
                    at=T2,
                ),
            )
        )
        # Extract the candidate (appends candidate entity + edge + run event).
        await FlagCandidateExtractor(run_id=RUN, event_log=log).extract(live)
        # Submit it (appends submission entity + edge + run events).
        coordinator = SubmissionCoordinator(
            client=_AcceptedClient(),
            run_id=RUN,
            challenge_id="web-01",
            event_log=log,
        )
        await coordinator.submit_verified_candidate(live)
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash

    replayed = await replay_into(log.path, tmp_path / "replay2.db")
    try:
        candidates = await replayed.list_entities("flag_candidate")
        assert len(candidates) == 1
        assert candidates[0].data["flag"] == "flag{replay-42}"
        submissions = await replayed.list_entities("submission")
        assert len(submissions) == 1
        assert submissions[0].data["accepted"] is True
    finally:
        await replayed.close()


@pytest.mark.asyncio
async def test_replay_matches_live_graph_invariant(tmp_path: Path) -> None:
    """AGENTS.md invariant: mutating a live graph while appending one
    graph event per mutation, replay_graph(log) == live graph hash."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(tmp_path / "live.db") as live:
        await live.create_entity("run-1", "run", {"phase": "RECON"}, at=T1)
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
        await live.create_entity("svc-1", "service", {"port": 80}, at=T1)
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
        await live.create_entity("tgt-1", "target", {}, at=T1)
        log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                RUN,
                "supervisor",
                GraphEntityCreated(entity_id="tgt-1", entity_type="target", at=T1),
            )
        )
        await live.create_edge("e1", "OBSERVED_ON", "svc-1", "tgt-1", {"probe": "nmap"}, at=T2)
        log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                RUN,
                "supervisor",
                GraphEdgeCreated(
                    edge_id="e1",
                    edge_type="OBSERVED_ON",
                    src_id="svc-1",
                    dst_id="tgt-1",
                    data={"probe": "nmap"},
                    at=T2,
                ),
            )
        )
        await live.update_entity("run-1", {"phase": "EXPLOITATION"}, at=T3)
        log.append(
            graph_event(
                GRAPH_ENTITY_UPDATED,
                RUN,
                "supervisor",
                GraphEntityUpdated(entity_id="run-1", data={"phase": "EXPLOITATION"}, at=T3),
            )
        )
        # Interleave a bootstrap event; replay must ignore it.
        log.append(Event(run_id=RUN, timestamp=T3, event_type=BOOTSTRAP, producer="supervisor"))
        await live.delete_edge("e1")
        log.append(
            graph_event(
                GRAPH_EDGE_DELETED,
                RUN,
                "supervisor",
                GraphEdgeDeleted(edge_id="e1"),
            )
        )
        # Deleting an entity with a live edge proves cascade parity.
        await live.create_edge("e2", "OBSERVED_ON", "svc-1", "tgt-1", at=T3)
        log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                RUN,
                "supervisor",
                GraphEdgeCreated(
                    edge_id="e2", edge_type="OBSERVED_ON", src_id="svc-1", dst_id="tgt-1", at=T3
                ),
            )
        )
        await live.delete_entity("svc-1")
        log.append(
            graph_event(
                GRAPH_ENTITY_DELETED,
                RUN,
                "supervisor",
                GraphEntityDeleted(entity_id="svc-1"),
            )
        )
        live_hash = await live.graph_hash()

    assert await replay_graph(log.path, tmp_path / "replay.db") == live_hash
