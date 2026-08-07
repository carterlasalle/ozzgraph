"""SQLite-backed state graph for OzzGraph (PR7).

Implements the authoritative state layer required by AGENTS.md rule #1
("Authoritative state lives outside model context") and the entity/edge
model documented in docs/DATA_STRATEGY.md ("SQLite State Graph") and
docs/ARCHITECTURE.md ("State and Work Graph").

The graph is a single SQLite database with two tables:

- ``entities(id, type, data, created_at, updated_at)`` — nodes. IDs are
  caller-supplied stable IDs (``run-<uuid>``, ``action-<uuid>``, ...).
- ``edges(id, type, src_id, dst_id, data, created_at)`` — typed, directed
  relationships between entities (e.g. ``ACTION PRODUCED OBSERVATION``).
  Edges are unique on ``(type, src_id, dst_id)`` so the graph is
  deterministic: the same relationship cannot be inserted twice.

Both endpoints of an edge must already exist. This is enforced by a
foreign key (``PRAGMA foreign_keys=ON`` is set on every connection) with
``ON DELETE CASCADE``, so deleting an entity also removes every edge
touching it.

Schema evolution is forward-only: :data:`MIGRATIONS` is an ordered tuple
of ``(version, statements)`` applied in ascending order, and the current
version is stored in ``PRAGMA user_version``. Opening an already-current
database applies no migrations, so reopening is an idempotent no-op.

All I/O is async (aiosqlite), JSON payloads are plain
``dict[str, object]``, and every failure is reported through the
:class:`StateGraphError` hierarchy — no bare ``sqlite3`` exceptions leak
to callers. The module holds no global mutable state; each
:class:`StateGraph` instance owns exactly one connection to its database
path.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import aiosqlite

# Current graph schema version; must equal the largest version in MIGRATIONS.
SCHEMA_VERSION = 2

# Busy timeout in milliseconds for concurrent connections (WAL allows one
# writer plus many readers; writers wait up to this long for the lock).
_BUSY_TIMEOUT_MS = 5000

# Column lists used by every read query (kept in one place so the row
# factories below never drift from the SELECT clauses).
_ENTITY_COLUMNS = "id, type, data, created_at, updated_at"
_EDGE_COLUMNS = "id, type, src_id, dst_id, data, created_at"


class StateGraphError(RuntimeError):
    """Base error for every state-graph failure.

    No bare ``sqlite3`` exception ever reaches a caller; every failure is
    raised as (a subclass of) this error.
    """


class EntityNotFoundError(StateGraphError):
    """Raised when an entity is required but does not exist."""


class EdgeNotFoundError(StateGraphError):
    """Raised when an edge is required but does not exist."""


class DuplicateEntityError(StateGraphError):
    """Raised when creating an entity whose ID already exists."""


class DuplicateEdgeError(StateGraphError):
    """Raised when an equivalent edge already exists.

    Equivalent means the same ``(type, src_id, dst_id)`` triple (or the
    same edge ID): the graph is deterministic and never holds duplicate
    relationships.
    """


class ForeignKeyViolationError(StateGraphError):
    """Raised when an edge references an endpoint that does not exist."""


class MigrationError(StateGraphError):
    """Raised when a schema migration fails or the list is malformed."""


@dataclass(frozen=True)
class EntityRecord:
    """One row of the ``entities`` table.

    Attributes:
        id: Stable, caller-supplied entity ID.
        type: Entity kind (e.g. ``run``, ``action``, ``hypothesis``).
        data: Entity payload as JSON data.
        created_at: UTC creation timestamp.
        updated_at: UTC timestamp of the last update.
    """

    id: str
    type: str
    data: dict[str, object]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EdgeRecord:
    """One row of the ``edges`` table.

    Attributes:
        id: Stable, caller-supplied edge ID.
        type: Relationship kind (e.g. ``ACTION PRODUCED OBSERVATION``).
        src_id: Source entity ID.
        dst_id: Destination entity ID.
        data: Edge payload as JSON data.
        created_at: UTC creation timestamp.
    """

    id: str
    type: str
    src_id: str
    dst_id: str
    data: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class Neighbors:
    """The edges touching one entity, split by direction.

    Attributes:
        incoming: Edges where the entity is the destination (``? -> e``).
        outgoing: Edges where the entity is the source (``e -> ?``).
    """

    incoming: tuple[EdgeRecord, ...]
    outgoing: tuple[EdgeRecord, ...]


@dataclass(frozen=True)
class Migration:
    """One forward-only schema migration.

    Attributes:
        version: Target ``PRAGMA user_version`` after this migration.
        statements: SQL statements applied in order, atomically, inside a
            single transaction.
    """

    version: int
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (type)",
            """
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                src_id TEXT NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
                dst_id TEXT NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src_id)",
            "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_type_src_dst ON edges (type, src_id, dst_id)",
        ),
    ),
    Migration(
        version=2,
        statements=(
            # Per-entity payload version, defaulting to 1 for rows created
            # before this migration. Future payload migrations bump this
            # per row instead of rewriting stored JSON.
            "ALTER TABLE entities ADD COLUMN data_version INTEGER NOT NULL DEFAULT 1",
        ),
    ),
)


def _validate_migrations(migrations: tuple[Migration, ...]) -> None:
    """Reject malformed migration lists (duplicates, disorder, no base).

    Raises:
        MigrationError: If versions are not unique, not strictly ascending,
            or do not start at version 1.
    """
    versions = [migration.version for migration in migrations]
    if not versions or versions[0] != 1 or versions != sorted(versions) or len(set(versions)) != len(
        versions
    ):
        raise MigrationError(
            "migrations must have unique, strictly ascending versions starting at 1"
        )


def _dumps(data: dict[str, object]) -> str:
    """Canonical JSON encoding: sorted keys, compact separators.

    Raises:
        StateGraphError: If ``data`` is not JSON-serializable.
    """
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StateGraphError(f"data must be JSON-serializable: {exc}") from exc


def _loads(raw: str) -> dict[str, object]:
    """Decode a stored JSON payload back into a dict.

    Raises:
        StateGraphError: If the stored text is not a JSON object.
    """
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateGraphError(f"stored JSON is corrupt: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StateGraphError("stored JSON is not an object")
    return loaded


def _parse_ts(raw: str) -> datetime:
    """Parse a stored ISO-8601 timestamp into a timezone-aware datetime.

    Raises:
        StateGraphError: If the stored text is not a valid ISO-8601 string.
    """
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise StateGraphError(f"stored timestamp {raw!r} is not ISO-8601: {exc}") from exc


def _now(at: datetime | None) -> datetime:
    """The creation/update timestamp: ``at`` normalized to UTC, else now.

    Raises:
        ValueError: If ``at`` is naive (missing timezone information).
    """
    if at is None:
        return datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return at.astimezone(UTC)


def _fmt_ts(value: datetime) -> str:
    """Serialize a datetime as ISO-8601 for storage."""
    return value.isoformat()


def _entity_record(row: aiosqlite.Row) -> EntityRecord:
    return EntityRecord(
        id=str(row["id"]),
        type=str(row["type"]),
        data=_loads(str(row["data"])),
        created_at=_parse_ts(str(row["created_at"])),
        updated_at=_parse_ts(str(row["updated_at"])),
    )


def _edge_record(row: aiosqlite.Row) -> EdgeRecord:
    return EdgeRecord(
        id=str(row["id"]),
        type=str(row["type"]),
        src_id=str(row["src_id"]),
        dst_id=str(row["dst_id"]),
        data=_loads(str(row["data"])),
        created_at=_parse_ts(str(row["created_at"])),
    )


async def _read_user_version(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    if row is None:
        raise MigrationError("could not read PRAGMA user_version")
    return int(row[0])


@asynccontextmanager
async def _transaction(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Atomic block over ``conn`` with explicit BEGIN/COMMIT/ROLLBACK.

    Raises:
        StateGraphError: If BEGIN, COMMIT, or ROLLBACK itself fails.
    """
    try:
        await conn.execute("BEGIN")
    except sqlite3.Error as exc:
        raise StateGraphError(f"could not begin transaction: {exc}") from exc
    try:
        yield
    except BaseException:
        try:
            await conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise StateGraphError(f"could not roll back transaction: {exc}") from exc
        raise
    try:
        await conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise StateGraphError(f"could not commit transaction: {exc}") from exc


class StateGraph:
    """Async SQLite-backed state graph.

    Args:
        db_path: Path to the SQLite database file. The file (and its parent
            directory) is created on :meth:`open` when missing.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._in_transaction = False

    @property
    def path(self) -> Path:
        """The database file path this graph is bound to."""
        return self._path

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def _connection(self) -> aiosqlite.Connection:
        """The live connection, or a loud error when the graph is closed."""
        conn = self._conn
        if conn is None:
            raise StateGraphError("StateGraph is not open; call open() first")
        return conn

    async def open(self) -> None:
        """Open the database, apply PRAGMAs, and run pending migrations.

        The connection uses WAL journaling, ``foreign_keys=ON``, and a
        5-second busy timeout. Missing tables and out-of-date schemas are
        migrated forward; an already-current database is left untouched.
        Calling :meth:`open` on an already-open graph is a no-op.

        Raises:
            MigrationError: If applying a migration fails; the connection
                is closed and the graph is left closed.
        """
        if self._conn is not None:
            return
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            cursor = await conn.execute("PRAGMA journal_mode=WAL")
            await cursor.fetchone()
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            await self._migrate(conn)
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        """Close the connection. Idempotent; the graph can be reopened."""
        conn = self._conn
        self._conn = None
        if conn is not None:
            await conn.close()

    async def schema_version(self) -> int:
        """The current ``PRAGMA user_version`` of the open database."""
        return await _read_user_version(self._connection())

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """Apply every migration newer than the stored user_version.

        Each migration runs atomically; a failure rolls back its partial
        statements and raises :class:`MigrationError`.
        """
        _validate_migrations(MIGRATIONS)
        current = await _read_user_version(conn)
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            try:
                async with _transaction(conn):
                    for statement in migration.statements:
                        await conn.execute(statement)
                    await conn.execute(f"PRAGMA user_version = {migration.version}")
            except (StateGraphError, sqlite3.Error) as exc:
                raise MigrationError(
                    f"migration to schema version {migration.version} failed: {exc}"
                ) from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run a block of mutations atomically (BEGIN/COMMIT/ROLLBACK).

        Mutations performed inside the block either all commit or all roll
        back. Nested transactions are rejected loudly rather than silently
        sharing a connection-level transaction.

        Raises:
            StateGraphError: If the graph is closed, if a transaction is
                already open, or if BEGIN/COMMIT/ROLLBACK fails.
        """
        conn = self._connection()
        if self._in_transaction:
            raise StateGraphError("nested transactions are not supported")
        self._in_transaction = True
        try:
            async with _transaction(conn):
                yield
        finally:
            self._in_transaction = False

    async def _entity_exists(self, conn: aiosqlite.Connection, entity_id: str) -> bool:
        cursor = await conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,))
        return await cursor.fetchone() is not None

    async def _find_edge(
        self, conn: aiosqlite.Connection, edge_type: str, src_id: str, dst_id: str
    ) -> EdgeRecord | None:
        cursor = await conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges WHERE type = ? AND src_id = ? AND dst_id = ?",
            (edge_type, src_id, dst_id),
        )
        row = await cursor.fetchone()
        return _edge_record(row) if row is not None else None

    async def create_entity(
        self,
        entity_id: str,
        entity_type: str,
        data: dict[str, object] | None = None,
        *,
        at: datetime | None = None,
    ) -> EntityRecord:
        """Insert a new entity and return its record.

        Args:
            entity_id: Stable caller-supplied ID (e.g. ``run-<uuid>``).
            entity_type: Entity kind (e.g. ``run``, ``action``).
            data: JSON payload; defaults to ``{}``.
            at: Optional creation timestamp; defaults to UTC now. Callers
                replaying events may pass the event timestamp for
                deterministic reconstruction.

        Raises:
            DuplicateEntityError: If ``entity_id`` already exists.
            StateGraphError: If the payload is not JSON-serializable.
        """
        conn = self._connection()
        now = _now(at)
        if await self._entity_exists(conn, entity_id):
            raise DuplicateEntityError(f"entity {entity_id!r} already exists")
        try:
            await conn.execute(
                "INSERT INTO entities (id, type, data, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (entity_id, entity_type, _dumps({} if data is None else data), _fmt_ts(now), _fmt_ts(now)),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEntityError(f"entity {entity_id!r} already exists") from exc
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to create entity {entity_id!r}: {exc}") from exc
        return EntityRecord(
            id=entity_id,
            type=entity_type,
            data={} if data is None else data,
            created_at=now,
            updated_at=now,
        )

    async def get_entity(self, entity_id: str) -> EntityRecord | None:
        """Fetch one entity by ID, or ``None`` when it does not exist.

        Raises:
            StateGraphError: If the graph is closed or the read fails.
        """
        conn = self._connection()
        try:
            cursor = await conn.execute(
                f"SELECT {_ENTITY_COLUMNS} FROM entities WHERE id = ?", (entity_id,)
            )
            row = await cursor.fetchone()
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to read entity {entity_id!r}: {exc}") from exc
        return _entity_record(row) if row is not None else None

    async def update_entity(
        self,
        entity_id: str,
        data: dict[str, object],
        *,
        at: datetime | None = None,
    ) -> EntityRecord:
        """Replace an entity's payload and refresh ``updated_at``.

        Args:
            entity_id: ID of the entity to update.
            data: New JSON payload.
            at: Optional update timestamp; defaults to UTC now.

        Raises:
            EntityNotFoundError: If the entity does not exist.
            StateGraphError: If the payload is not JSON-serializable.
        """
        conn = self._connection()
        try:
            cursor = await conn.execute(
                "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
                (_dumps(data), _fmt_ts(_now(at)), entity_id),
            )
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to update entity {entity_id!r}: {exc}") from exc
        if cursor.rowcount == 0:
            raise EntityNotFoundError(f"entity {entity_id!r} does not exist")
        record = await self.get_entity(entity_id)
        if record is None:  # pragma: no cover - the row was just updated
            raise StateGraphError(f"entity {entity_id!r} vanished during update")
        return record

    async def delete_entity(self, entity_id: str) -> None:
        """Delete an entity and cascade-delete every edge touching it.

        Raises:
            EntityNotFoundError: If the entity does not exist.
        """
        conn = self._connection()
        try:
            cursor = await conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to delete entity {entity_id!r}: {exc}") from exc
        if cursor.rowcount == 0:
            raise EntityNotFoundError(f"entity {entity_id!r} does not exist")

    async def create_edge(
        self,
        edge_id: str,
        edge_type: str,
        src_id: str,
        dst_id: str,
        data: dict[str, object] | None = None,
        *,
        at: datetime | None = None,
    ) -> EdgeRecord:
        """Insert a new typed edge and return its record.

        Both endpoints must already exist (foreign keys are enforced). The
        ``(type, src_id, dst_id)`` triple is unique, so the same
        relationship cannot be inserted twice.

        Args:
            edge_id: Stable caller-supplied edge ID.
            edge_type: Relationship kind (e.g. ``ACTION PRODUCED OBSERVATION``).
            src_id: Source entity ID.
            dst_id: Destination entity ID.
            data: JSON payload; defaults to ``{}``.
            at: Optional creation timestamp; defaults to UTC now. Callers
                replaying events may pass the event timestamp.

        Raises:
            EntityNotFoundError: If either endpoint does not exist.
            DuplicateEdgeError: If ``edge_id`` or the ``(type, src, dst)``
                triple already exists.
            ForeignKeyViolationError: If the database rejects the edge for
                a missing endpoint.
            StateGraphError: If the payload is not JSON-serializable.
        """
        conn = self._connection()
        now = _now(at)
        payload = {} if data is None else data
        try:
            async with self.transaction():
                if not await self._entity_exists(conn, src_id):
                    raise EntityNotFoundError(f"source entity {src_id!r} does not exist")
                if not await self._entity_exists(conn, dst_id):
                    raise EntityNotFoundError(f"destination entity {dst_id!r} does not exist")
                if await self._find_edge(conn, edge_type, src_id, dst_id) is not None:
                    raise DuplicateEdgeError(
                        f"edge {edge_type!r} {src_id!r}->{dst_id!r} already exists"
                    )
                try:
                    await conn.execute(
                        "INSERT INTO edges (id, type, src_id, dst_id, data, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (edge_id, edge_type, src_id, dst_id, _dumps(payload), _fmt_ts(now)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise _map_edge_integrity_error(exc, edge_id, edge_type, src_id, dst_id) from exc
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to create edge {edge_id!r}: {exc}") from exc
        return EdgeRecord(
            id=edge_id,
            type=edge_type,
            src_id=src_id,
            dst_id=dst_id,
            data=payload,
            created_at=now,
        )

    async def get_edge(self, edge_id: str) -> EdgeRecord | None:
        """Fetch one edge by ID, or ``None`` when it does not exist.

        Raises:
            StateGraphError: If the graph is closed or the read fails.
        """
        conn = self._connection()
        try:
            cursor = await conn.execute(
                f"SELECT {_EDGE_COLUMNS} FROM edges WHERE id = ?", (edge_id,)
            )
            row = await cursor.fetchone()
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to read edge {edge_id!r}: {exc}") from exc
        return _edge_record(row) if row is not None else None

    async def delete_edge(self, edge_id: str) -> None:
        """Delete one edge by ID.

        Raises:
            EdgeNotFoundError: If the edge does not exist.
        """
        conn = self._connection()
        try:
            cursor = await conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to delete edge {edge_id!r}: {exc}") from exc
        if cursor.rowcount == 0:
            raise EdgeNotFoundError(f"edge {edge_id!r} does not exist")

    async def neighbors(self, entity_id: str, edge_type: str | None = None) -> Neighbors:
        """All edges touching ``entity_id``, split by direction.

        With ``edge_type`` set, only edges of that type are returned.
        Results are ordered by edge ID, so the shape is deterministic.

        Raises:
            EntityNotFoundError: If the entity does not exist.
        """
        conn = self._connection()
        if not await self._entity_exists(conn, entity_id):
            raise EntityNotFoundError(f"entity {entity_id!r} does not exist")
        try:
            if edge_type is None:
                outgoing_sql = f"SELECT {_EDGE_COLUMNS} FROM edges WHERE src_id = ? ORDER BY id"
                incoming_sql = f"SELECT {_EDGE_COLUMNS} FROM edges WHERE dst_id = ? ORDER BY id"
                params: tuple[object, ...] = (entity_id,)
            else:
                outgoing_sql = (
                    f"SELECT {_EDGE_COLUMNS} FROM edges WHERE src_id = ? AND type = ? ORDER BY id"
                )
                incoming_sql = (
                    f"SELECT {_EDGE_COLUMNS} FROM edges WHERE dst_id = ? AND type = ? ORDER BY id"
                )
                params = (entity_id, edge_type)
            outgoing_cursor = await conn.execute(outgoing_sql, params)
            incoming_cursor = await conn.execute(incoming_sql, params)
            outgoing_rows = await outgoing_cursor.fetchall()
            incoming_rows = await incoming_cursor.fetchall()
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to read neighbors of {entity_id!r}: {exc}") from exc
        return Neighbors(
            incoming=tuple(_edge_record(row) for row in incoming_rows),
            outgoing=tuple(_edge_record(row) for row in outgoing_rows),
        )

    async def list_entities(self, entity_type: str | None = None) -> list[EntityRecord]:
        """List entities, optionally filtered by type, ordered by ID.

        Args:
            entity_type: When given, only entities of this type are
                returned; otherwise every entity is returned.

        Raises:
            StateGraphError: If the graph is closed or the read fails.
        """
        conn = self._connection()
        try:
            if entity_type is None:
                cursor = await conn.execute(
                    f"SELECT {_ENTITY_COLUMNS} FROM entities ORDER BY id"
                )
            else:
                cursor = await conn.execute(
                    f"SELECT {_ENTITY_COLUMNS} FROM entities WHERE type = ? ORDER BY id",
                    (entity_type,),
                )
            rows = await cursor.fetchall()
        except sqlite3.Error as exc:
            raise StateGraphError(f"failed to list entities: {exc}") from exc
        return [_entity_record(row) for row in rows]


def _map_edge_integrity_error(
    exc: sqlite3.IntegrityError, edge_id: str, edge_type: str, src_id: str, dst_id: str
) -> StateGraphError:
    """Map an INSERT-time integrity error to the specific violation.

    The public methods pre-check the common violations, so this is a
    defensive fallback for the race paths (e.g. a concurrent delete
    removing an endpoint between check and insert).
    """
    message = str(exc)
    if "UNIQUE constraint failed: edges.id" in message:
        return DuplicateEdgeError(f"edge id {edge_id!r} already exists")
    if "UNIQUE constraint failed" in message:
        return DuplicateEdgeError(f"edge {edge_type!r} {src_id!r}->{dst_id!r} already exists")
    if "FOREIGN KEY constraint failed" in message:
        return ForeignKeyViolationError(f"edge endpoint does not exist: {message}")
    return StateGraphError(f"failed to create edge {edge_id!r}: {message}")
