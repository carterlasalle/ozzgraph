"""Tests for the append-only structured event log (PR4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.events import BOOTSTRAP, SCHEMA_VERSION, TERMINATION, Event, EventLog

_FIXED_TS = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
_REQUIRED_FIELDS = frozenset(
    {"event_id", "run_id", "timestamp", "event_type", "producer", "schema_version", "payload"}
)


def _event(
    *,
    run_id: str = "run-1",
    event_type: str = BOOTSTRAP,
    producer: str = "supervisor",
    timestamp: datetime | None = None,
    payload: dict[str, object] | None = None,
) -> Event:
    """Build an event with a fixed UTC timestamp unless overridden."""
    return Event(
        run_id=run_id,
        timestamp=timestamp if timestamp is not None else _FIXED_TS,
        event_type=event_type,
        producer=producer,
        payload={} if payload is None else payload,
    )


def _records(path: Path) -> list[dict[str, object]]:
    """Parse every line of the log at ``path`` into dicts."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_append_creates_file(tmp_path: Path) -> None:
    """Appending to a fresh log creates the file at the given path."""
    log = EventLog(tmp_path / "actions.jsonl")
    log.append(_event())
    assert log.path.is_file()
    assert log.path.read_text(encoding="utf-8").strip() != ""


def test_for_run_points_at_actions_jsonl(tmp_path: Path) -> None:
    """The standard run log lives at ``state_dir / 'actions.jsonl'``."""
    log = EventLog.for_run(tmp_path)
    assert log.path == tmp_path / "actions.jsonl"


def test_every_line_is_valid_json_with_required_fields(tmp_path: Path) -> None:
    """Each appended line parses as JSON and carries all required fields."""
    log = EventLog.for_run(tmp_path)
    log.append(_event())
    log.append(_event(event_type=TERMINATION, payload={"reason": "interrupted"}))
    records = _records(log.path)
    assert len(records) == 2
    for record in records:
        assert _REQUIRED_FIELDS.issubset(record.keys())
        assert record["run_id"] == "run-1"
        assert record["schema_version"] == SCHEMA_VERSION
        assert record["task_id"] is None
        assert record["worker_id"] is None
        parsed = datetime.fromisoformat(str(record["timestamp"]))
        assert parsed.tzinfo is not None


def test_event_ids_unique_across_appends(tmp_path: Path) -> None:
    """Every appended event carries a fresh, unique event ID."""
    log = EventLog.for_run(tmp_path)
    log.append(_event())
    log.append(_event(event_type=TERMINATION))
    ids = [str(record["event_id"]) for record in _records(log.path)]
    assert len(ids) == len(set(ids))


def test_append_only_keeps_first_line_intact(tmp_path: Path) -> None:
    """A second append never rewrites the first line."""
    path = tmp_path / "actions.jsonl"
    log = EventLog(path)
    first = _event()
    log.append(first)
    first_line = path.read_text(encoding="utf-8").rstrip("\n")
    log.append(_event(event_type=TERMINATION))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line
    assert json.loads(lines[0])["event_id"] == first.event_id


def test_reopening_log_appends_without_truncation(tmp_path: Path) -> None:
    """A fresh EventLog on the same path appends, preserving prior lines."""
    path = tmp_path / "actions.jsonl"
    EventLog(path).append(_event())
    EventLog(path).append(_event(event_type=TERMINATION))
    records = _records(path)
    assert len(records) == 2
    assert records[0]["event_type"] == BOOTSTRAP
    assert records[1]["event_type"] == TERMINATION


def test_naive_timestamp_rejected() -> None:
    """A naive timestamp is rejected loudly rather than silently stored."""
    with pytest.raises(ValidationError):
        Event(
            run_id="run-1",
            timestamp=datetime(2026, 8, 6, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            event_type=BOOTSTRAP,
            producer="supervisor",
        )


def test_timestamp_normalized_to_utc() -> None:
    """A non-UTC offset is normalized to UTC on validation."""
    offset = timezone(timedelta(hours=2))
    event = _event(timestamp=datetime(2026, 8, 6, 12, 0, 0, tzinfo=offset))
    assert event.timestamp.utcoffset() == timedelta(0)
