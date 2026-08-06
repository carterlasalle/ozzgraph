"""Append-only structured event log for OzzGraph (PR4).

Implements the append-only JSONL event log that AGENTS.md requires
("append-only JSONL events" as authoritative state) and that
docs/DATA_STRATEGY.md places at ``state_dir/actions.jsonl``. Every meaningful
event — bootstrap and termination — is written as exactly one JSON line
carrying an event ID, run ID, UTC timestamp, event type, producer, schema
version, and payload.

The log is strictly append-only: :meth:`EventLog.append` opens the file in
append mode, writes one line, and flushes; reopening the log appends and
never truncates or rewrites prior lines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1

# Event type constants (module-level, matching DATA_STRATEGY's event types).
BOOTSTRAP = "bootstrap"
TERMINATION = "termination"


class Event(BaseModel):
    """One structured event in the append-only run log.

    Attributes:
        event_id: Unique identifier for the event (uuid4 hex by default).
        run_id: Identifier of the run that produced the event.
        timestamp: Timezone-aware UTC timestamp, serialized as ISO-8601.
        event_type: Kind of event (see ``BOOTSTRAP`` / ``TERMINATION``).
        producer: Component that emitted the event (e.g. ``supervisor``).
        schema_version: Event schema version; bumped only by forward-only
            migrations.
        task_id: Owning task, when the event belongs to a worker task.
        worker_id: Owning worker, when the event was emitted by a worker.
        payload: Free-form, event-specific data.
    """

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    timestamp: datetime
    event_type: str
    producer: str
    schema_version: int = SCHEMA_VERSION
    task_id: str | None = None
    worker_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_utc(cls, value: datetime) -> datetime:
        """Reject naive timestamps and normalize any offset to UTC.

        Raises:
            ValueError: If the timestamp has no timezone information.
        """
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return value.astimezone(UTC)


class EventLog:
    """Append-only JSONL writer for :class:`Event` records.

    Args:
        path: Log file path. Created on the first append; the parent
            directory must already exist.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_run(cls, state_dir: Path) -> EventLog:
        """The standard run log at ``state_dir / 'actions.jsonl'``."""
        return cls(state_dir / "actions.jsonl")

    @property
    def path(self) -> Path:
        """The log file path (may not exist until the first append)."""
        return self._path

    def append(self, event: Event) -> None:
        """Append one JSON line for ``event`` and flush it to disk.

        The file is opened in append mode, so prior lines are never
        truncated or rewritten. I/O errors propagate to the caller.

        Args:
            event: The event to record.
        """
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
