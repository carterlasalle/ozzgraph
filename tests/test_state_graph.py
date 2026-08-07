"""Tests for the SQLite state graph (PR7).

Covers entity/edge CRUD, foreign-key enforcement, duplicate-edge
rejection, neighbor traversal, the forward-only migration mechanism
(fresh DB, older schema, idempotent reopen, failure), and the
transaction helper. Every test uses its own ``tmp_path`` database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

import ozzgraph.state_graph as state_graph_module
from ozzgraph.state_graph import (
    MIGRATIONS,
    SCHEMA_VERSION,
    DuplicateEdgeError,
    DuplicateEntityError,
    EdgeNotFoundError,
    EntityNotFoundError,
    Migration,
    MigrationError,
    StateGraph,
    StateGraphError,
)

T1 = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 6, 11, 0, 0, tzinfo=UTC)


async def _column_names(path: Path, table: str) -> set[str]:
    """Column names of ``table``, read through a fresh connection."""
    async with aiosqlite.connect(path) as conn:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


async def _user_version(path: Path) -> int:
    """``PRAGMA user_version`` read through a fresh connection."""
    async with aiosqlite.connect(path) as conn:
        cursor = await conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _apply_v1(path: Path) -> None:
    """Create a database at schema version 1 using the module's own DDL."""
    async with aiosqlite.connect(path, isolation_level=None) as conn:
        for statement in MIGRATIONS[0].statements:
            await conn.execute(statement)
        await conn.execute("PRAGMA user_version = 1")


@pytest.mark.asyncio
async def test_create_get_update_delete_entity(tmp_path: Path) -> None:
    """Full entity CRUD round-trip."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        created = await graph.create_entity("run-1", "run", {"phase": "RECON"})
        assert created.id == "run-1"
        assert created.type == "run"
        assert created.data == {"phase": "RECON"}
        assert created.created_at == created.updated_at
        assert created.created_at.tzinfo is not None

        record = await graph.get_entity("run-1")
        assert record == created

        updated = await graph.update_entity("run-1", {"phase": "EXPLOITATION"})
        assert updated.data == {"phase": "EXPLOITATION"}
        assert updated.created_at == created.created_at
        assert updated.updated_at >= updated.created_at
        assert (await graph.get_entity("run-1")) == updated

        await graph.delete_entity("run-1")
        assert await graph.get_entity("run-1") is None


@pytest.mark.asyncio
async def test_get_entity_missing_returns_none(tmp_path: Path) -> None:
    """get_entity on an unknown ID returns None, not an error."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        assert await graph.get_entity("nope") is None


@pytest.mark.asyncio
async def test_create_duplicate_entity_id_raises(tmp_path: Path) -> None:
    """A second entity with the same ID is rejected loudly."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("e1", "node")
        with pytest.raises(DuplicateEntityError):
            await graph.create_entity("e1", "node", {"other": True})


@pytest.mark.asyncio
async def test_update_entity_missing_raises(tmp_path: Path) -> None:
    """Updating an unknown entity is a loud not-found failure."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        with pytest.raises(EntityNotFoundError):
            await graph.update_entity("nope", {})


@pytest.mark.asyncio
async def test_delete_entity_missing_raises(tmp_path: Path) -> None:
    """Deleting an unknown entity is a loud not-found failure."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        with pytest.raises(EntityNotFoundError):
            await graph.delete_entity("nope")


@pytest.mark.asyncio
async def test_update_entity_respects_injected_timestamps(tmp_path: Path) -> None:
    """Callers may pin timestamps for deterministic replay."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        created = await graph.create_entity("e1", "node", {"v": 1}, at=T1)
        assert created.created_at == T1
        updated = await graph.update_entity("e1", {"v": 2}, at=T2)
        assert updated.created_at == T1
        assert updated.updated_at == T2


@pytest.mark.asyncio
async def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    """Naive timestamps are rejected rather than stored ambiguously."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        with pytest.raises(ValueError):
            await graph.create_entity(
                "e1",
                "node",
                at=datetime(2026, 8, 6, 10, 0, 0),  # noqa: DTZ001 - deliberately naive
            )


@pytest.mark.asyncio
async def test_non_json_serializable_data_rejected(tmp_path: Path) -> None:
    """Unserializable payloads fail loudly with a StateGraphError."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        with pytest.raises(StateGraphError):
            await graph.create_entity("e1", "node", {"blob": object()})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_entities_by_type_and_all(tmp_path: Path) -> None:
    """list_entities filters by type and orders deterministically by ID."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("run-1", "run")
        await graph.create_entity("action-b", "action")
        await graph.create_entity("action-a", "action")

        actions = await graph.list_entities("action")
        assert [record.id for record in actions] == ["action-a", "action-b"]

        runs = await graph.list_entities("run")
        assert [record.id for record in runs] == ["run-1"]

        all_entities = await graph.list_entities()
        assert [record.id for record in all_entities] == [
            "action-a",
            "action-b",
            "run-1",
        ]


@pytest.mark.asyncio
async def test_create_get_delete_edge(tmp_path: Path) -> None:
    """Full edge CRUD round-trip."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("svc-1", "service")
        await graph.create_entity("tgt-1", "target")

        edge = await graph.create_edge("edge-1", "OBSERVED_ON", "svc-1", "tgt-1", {"probe": "nmap"})
        assert edge.id == "edge-1"
        assert edge.type == "OBSERVED_ON"
        assert edge.src_id == "svc-1"
        assert edge.dst_id == "tgt-1"
        assert edge.data == {"probe": "nmap"}

        assert await graph.get_edge("edge-1") == edge

        await graph.delete_edge("edge-1")
        assert await graph.get_edge("edge-1") is None
        with pytest.raises(EdgeNotFoundError):
            await graph.delete_edge("edge-1")


@pytest.mark.asyncio
async def test_edge_to_nonexistent_endpoint_raises_and_leaves_no_state(
    tmp_path: Path,
) -> None:
    """Edges cannot reference missing endpoints, and failed attempts
    leave no partial state behind."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("svc-1", "service")
        await graph.create_entity("tgt-1", "target")

        with pytest.raises(EntityNotFoundError):
            await graph.create_edge("edge-bad-src", "OBSERVED_ON", "svc-1", "ghost")
        with pytest.raises(EntityNotFoundError):
            await graph.create_edge("edge-bad-dst", "OBSERVED_ON", "ghost", "tgt-1")
        assert await graph.get_edge("edge-bad-src") is None
        assert await graph.get_edge("edge-bad-dst") is None

        # The same relationship can still be created cleanly afterwards.
        edge = await graph.create_edge("edge-1", "OBSERVED_ON", "svc-1", "tgt-1")
        assert edge.src_id == "svc-1"
        assert edge.dst_id == "tgt-1"


@pytest.mark.asyncio
async def test_duplicate_edge_triple_rejected(tmp_path: Path) -> None:
    """The same (type, src, dst) relationship cannot be inserted twice,
    even under a different edge ID."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("svc-1", "service")
        await graph.create_entity("tgt-1", "target")
        await graph.create_edge("edge-1", "OBSERVED_ON", "svc-1", "tgt-1")

        with pytest.raises(DuplicateEdgeError):
            await graph.create_edge("edge-2", "OBSERVED_ON", "svc-1", "tgt-1")
        assert await graph.get_edge("edge-2") is None


@pytest.mark.asyncio
async def test_duplicate_edge_id_rejected(tmp_path: Path) -> None:
    """Edge IDs are unique even across different relationships."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("a", "node")
        await graph.create_entity("b", "node")
        await graph.create_entity("c", "node")
        await graph.create_edge("edge-1", "links", "a", "b")
        with pytest.raises(DuplicateEdgeError):
            await graph.create_edge("edge-1", "links", "a", "c")


@pytest.mark.asyncio
async def test_neighbors_returns_both_directions(tmp_path: Path) -> None:
    """neighbors returns incoming and outgoing edges around an entity."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("tgt-1", "target")
        await graph.create_entity("svc-1", "service")
        await graph.create_entity("ep-1", "endpoint")
        await graph.create_edge("e1", "OBSERVED_ON", "svc-1", "tgt-1")
        await graph.create_edge("e2", "EXPOSED_BY", "ep-1", "svc-1")

        neighbors = await graph.neighbors("svc-1")
        assert [edge.id for edge in neighbors.outgoing] == ["e1"]
        assert [edge.id for edge in neighbors.incoming] == ["e2"]

        # ep-1 is the source of e2 (ep-1 -> svc-1), so it has an outgoing
        # edge and no incoming ones.
        leaf = await graph.neighbors("ep-1")
        assert [edge.id for edge in leaf.outgoing] == ["e2"]
        assert leaf.incoming == ()

        # A disconnected entity has no edges in either direction.
        await graph.create_entity("tgt-2", "target")
        isolated = await graph.neighbors("tgt-2")
        assert isolated.incoming == ()
        assert isolated.outgoing == ()


@pytest.mark.asyncio
async def test_neighbors_edge_type_filter(tmp_path: Path) -> None:
    """edge_type narrows neighbor traversal to one relationship kind."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("tgt-1", "target")
        await graph.create_entity("svc-1", "service")
        await graph.create_entity("ep-1", "endpoint")
        await graph.create_edge("e1", "OBSERVED_ON", "svc-1", "tgt-1")
        await graph.create_edge("e2", "EXPOSED_BY", "ep-1", "svc-1")

        filtered = await graph.neighbors("svc-1", edge_type="OBSERVED_ON")
        assert [edge.id for edge in filtered.outgoing] == ["e1"]
        assert filtered.incoming == ()

        none = await graph.neighbors("svc-1", edge_type="SUPPORTS")
        assert none.incoming == ()
        assert none.outgoing == ()


@pytest.mark.asyncio
async def test_neighbors_missing_entity_raises(tmp_path: Path) -> None:
    """Traversal of an unknown entity is a loud not-found failure."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        with pytest.raises(EntityNotFoundError):
            await graph.neighbors("ghost")


@pytest.mark.asyncio
async def test_delete_entity_cascades_edges(tmp_path: Path) -> None:
    """Deleting an entity removes every edge touching it (foreign_keys=ON
    with ON DELETE CASCADE)."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("tgt-1", "target")
        await graph.create_entity("svc-1", "service")
        await graph.create_entity("ep-1", "endpoint")
        await graph.create_edge("e1", "OBSERVED_ON", "svc-1", "tgt-1")
        await graph.create_edge("e2", "EXPOSED_BY", "ep-1", "svc-1")

        await graph.delete_entity("svc-1")

        assert await graph.get_edge("e1") is None
        assert await graph.get_edge("e2") is None
        assert (await graph.neighbors("ep-1")).incoming == ()


@pytest.mark.asyncio
async def test_fresh_db_opens_at_latest_schema(tmp_path: Path) -> None:
    """A brand-new database runs every migration and lands on the latest
    schema with WAL journaling."""
    path = tmp_path / "graph.db"
    async with StateGraph(path) as graph:
        assert await graph.schema_version() == SCHEMA_VERSION

    assert await _user_version(path) == SCHEMA_VERSION
    entity_columns = await _column_names(path, "entities")
    assert {"id", "type", "data", "created_at", "updated_at"}.issubset(entity_columns)
    edge_columns = await _column_names(path, "edges")
    assert {"id", "type", "src_id", "dst_id", "data", "created_at"}.issubset(edge_columns)

    async with aiosqlite.connect(path) as conn:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "wal"

        cursor = await conn.execute("PRAGMA index_list(edges)")
        indexes = {str(row[1]): int(row[2]) for row in await cursor.fetchall()}
    assert indexes["idx_edges_type_src_dst"] == 1  # unique


@pytest.mark.asyncio
async def test_older_schema_migrated_forward(tmp_path: Path) -> None:
    """A database at an older schema version is migrated forward: the
    missing column appears and CRUD still works."""
    path = tmp_path / "old.db"
    await _apply_v1(path)
    assert await _user_version(path) == 1
    assert "data_version" not in await _column_names(path, "entities")

    async with StateGraph(path) as graph:
        assert await graph.schema_version() == SCHEMA_VERSION
        await graph.create_entity("e1", "node", {"v": 1})
        record = await graph.get_entity("e1")
        assert record is not None
        assert record.data == {"v": 1}

    assert "data_version" in await _column_names(path, "entities")


@pytest.mark.asyncio
async def test_reopen_is_idempotent_noop(tmp_path: Path) -> None:
    """Reopening an already-current database applies no migrations and
    preserves stored state."""
    path = tmp_path / "graph.db"
    async with StateGraph(path) as graph:
        await graph.create_entity("e1", "node", {"v": 1})
        first_version = await graph.schema_version()

    async with StateGraph(path) as graph:
        assert await graph.schema_version() == first_version
        assert first_version == SCHEMA_VERSION
        record = await graph.get_entity("e1")
        assert record is not None
        assert record.data == {"v": 1}

    # open() on an already-open graph is a no-op too.
    graph = StateGraph(path)
    await graph.open()
    await graph.open()
    assert await graph.schema_version() == SCHEMA_VERSION
    await graph.close()


@pytest.mark.asyncio
async def test_migration_failure_raises_and_leaves_graph_closed(tmp_path: Path) -> None:
    """A failing migration raises MigrationError, rolls back, and leaves
    the graph closed rather than half-open."""
    path = tmp_path / "broken.db"
    await _apply_v1(path)
    # Simulate a partially-applied v2: the column exists but user_version
    # was never bumped, so the ALTER will collide.
    async with aiosqlite.connect(path, isolation_level=None) as conn:
        await conn.execute(
            "ALTER TABLE entities ADD COLUMN data_version INTEGER NOT NULL DEFAULT 1"
        )
        await conn.execute("PRAGMA user_version = 1")

    graph = StateGraph(path)
    with pytest.raises(MigrationError):
        await graph.open()

    assert await _user_version(path) == 1  # migration rolled back
    with pytest.raises(StateGraphError):
        await graph.schema_version()  # graph is closed, not half-open


@pytest.mark.asyncio
async def test_malformed_migration_list_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration list that skips version 1 is rejected at open time."""
    monkeypatch.setattr(
        state_graph_module,
        "MIGRATIONS",
        (Migration(version=2, statements=("CREATE TABLE x (id TEXT PRIMARY KEY)",)),),
    )
    with pytest.raises(MigrationError):
        async with StateGraph(tmp_path / "graph.db"):
            pass


@pytest.mark.asyncio
async def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    """transaction() commits on success and rolls back on failure."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        async with graph.transaction():
            await graph.create_entity("e1", "node")
        assert await graph.get_entity("e1") is not None

        with pytest.raises(RuntimeError):
            async with graph.transaction():
                await graph.create_entity("e2", "node")
                raise RuntimeError("boom")
        assert await graph.get_entity("e2") is None
        assert await graph.get_entity("e1") is not None


@pytest.mark.asyncio
async def test_nested_transaction_rejected(tmp_path: Path) -> None:
    """Nested transactions are rejected loudly, and the outer block
    still commits normally afterwards."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        async with graph.transaction():
            await graph.create_entity("e1", "node")
            with pytest.raises(StateGraphError):
                async with graph.transaction():
                    await graph.create_entity("e2", "node")
        assert await graph.get_entity("e1") is not None
        assert await graph.get_entity("e2") is None


@pytest.mark.asyncio
async def test_closed_graph_raises_loudly(tmp_path: Path) -> None:
    """Every operation on a closed graph fails loudly instead of leaking
    sqlite3 errors."""
    path = tmp_path / "graph.db"
    graph = StateGraph(path)
    with pytest.raises(StateGraphError):
        await graph.schema_version()

    await graph.open()
    await graph.close()
    with pytest.raises(StateGraphError):
        await graph.create_entity("e1", "node")
    await graph.close()  # idempotent
