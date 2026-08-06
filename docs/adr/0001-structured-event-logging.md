# ADR-0001: Structured Event Logging

Status: accepted

Date: 2026-08-06

## Context

AGENTS.md mandates that authoritative state live outside model context and
that state changes be representable as append-only events. docs/DATA_STRATEGY.md
defines an append-only event log at `state_dir/actions.jsonl` carrying event
ID, run ID, timestamp, event type, producer, task and worker IDs, schema
version, and payload. PR4 introduces the first concrete implementation of that
log — the first persistent storage technology in the repository — so this ADR
records the decision.

The PR3 supervisor already owns lifecycle and termination reasons; the event
log must capture bootstrap and termination without coupling to future
challenge-category logic (AGENTS.md architecture rule 10, "keep the kernel
small").

## Decision

We will implement the event log as an append-only JSONL file:

- One `Event` pydantic v2 model: `event_id` (uuid4 hex, unique per event),
  `run_id`, `timestamp` (timezone-aware UTC, serialized as ISO-8601),
  `event_type`, `producer`, `schema_version` (default 1), optional
  `task_id`/`worker_id`, and a free-form `payload` dict.
- One `EventLog` class that opens the file in append mode, writes exactly one
  JSON line per event, and flushes after every write. Reopening the log
  appends and never truncates or rewrites prior lines.
- The standard run log lives at `state_dir/actions.jsonl`, exposed via
  `EventLog.for_run(state_dir)`.
- Event types are module constants: `bootstrap` and `termination`.
- The supervisor mints a `run_id` at construction, appends a `bootstrap`
  event in `start()` (payload: `hal_user_id`, `state_dir`, `artifact_dir`,
  and a budget summary), and appends a `termination` event (payload: the
  reason) in `stop()` before clearing its started flag — so both `run()`
  terminal paths (budget exhausted, interrupted) end with a termination
  event.
- Naive timestamps are rejected; non-UTC offsets are normalized to UTC.
  Validation and I/O errors propagate — no silent exception swallowing.

## Consequences

Easier:

- The run lifecycle is replayable and auditable (bootstrap through
  termination), satisfying DATA_STRATEGY's replay requirements.
- Every termination produces the structured event that AGENTS.md rule 9
  ("fail loudly") requires.
- The log format is trivially parseable and forward-compatible via
  `schema_version`.

Harder:

- The log grows monotonically during a run; compaction is not attempted
  (retention keeps all events during a run).
- Schema changes must be forward-only migrations that bump `schema_version`;
  consumers must tolerate unknown versions.
- The JSONL log is a write-optimized record, not a queryable store; the
  SQLite state graph (a later PR) remains the projection for queries.
