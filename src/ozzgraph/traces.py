"""Golden traces for OzzGraph (PR28): capture + deterministic replay verify.

Implements the "Golden Traces" section of docs/TESTING_AND_QA.md and the
runner described there: a golden trace is a single JSON document that
captures a real run's challenge input, model responses, tool outputs,
expected graph events, expected final graph, and expected metrics. The
verifier replays the captured events through :mod:`ozzgraph.replay` into
a fresh database and compares the reconstructed graph (entity set, edge
set, graph hash) and the metrics against the trace's expectations,
reporting every mismatch as a structured diff — the prompt-regression
visibility docs/TESTING_AND_QA.md requires.

Design rules (AGENTS.md):

- Deterministic: capture snapshots the live graph via the same canonical
  reads the graph hash uses (``list_entities`` / ``list_edges`` ordered
  by id), so the same run always captures the same trace bytes; verify
  replays through the standard replay path, so the same trace always
  yields the same verification. No network, no clock, no randomness.

- Fail loudly (AGENTS.md rule #9): a corrupt or schema-invalid trace
  raises :class:`TraceError`; verification never silently skips a
  comparison. Mismatches are *reported* (a :class:`TraceVerification`
  with a structured diff) so prompt regressions stay visible, and
  :meth:`TraceVerification.assert_ok` turns them into a loud
  :class:`TraceVerificationError` for gate contexts.

- Replay-consistent (AGENTS.md data invariant): the trace's ``events``
  are the run's own append-only JSONL lines, embedded as parsed objects;
  verify rewrites them to a fresh JSONL file and calls
  :func:`~ozzgraph.replay.replay_into`, so the expected graph is exactly
  what replay reconstructs — never a parallel implementation.

- Schema migration compatibility: the trace records the graph schema
  version observed at capture time; verify compares it against the
  replayed database's version, so a trace captured under an older
  schema fails loudly with a ``schema`` mismatch instead of silently
  re-hashing under the new schema.

Metrics: a trace stores the run's expected nine metrics
(:class:`TraceMetrics`, the Model-Harness Matrix contract computed by
:mod:`ozzgraph.matrix`). Replay reconstructs the graph, not the
metrics, so verification compares metrics against the metrics the
verification context supplies (``actual_metrics`` — recomputed
deterministically from the recorded interactions, e.g. by re-running
the same deterministic matrix client): exact equality is the contract.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ozzgraph.replay import ReplayMalformedEventError, replay_into
from ozzgraph.state_graph import EdgeRecord, EntityRecord, StateGraph

#: Format marker for every golden trace document (versioned separately
#: from the graph schema version, so trace-format evolution never needs
#: to touch the graph).
TRACE_FORMAT: Literal["ozzgraph.golden_trace"] = "ozzgraph.golden_trace"

#: Current trace document format version.
TRACE_FORMAT_VERSION = 1


class TraceError(RuntimeError):
    """Base error for the golden-trace layer (AGENTS.md rule #9)."""


class TraceVerificationError(TraceError):
    """Raised by :meth:`TraceVerification.assert_ok` on any mismatch."""


class TraceChallenge(BaseModel):
    """The challenge input the run was pointed at.

    Deliberately carries no flag value and no target URL (lab ports are
    ephemeral): the challenge identity is the target's registry metadata
    plus the flag pattern the run accepted, so a trace stays stable
    across processes and never leaks challenge answers beyond what the
    recorded events already contain.

    Attributes:
        target_name: The registry key of the synthetic target (or the
            challenge id for a production run).
        target_category: The challenge category.
        target_description: Human-readable description of the challenge.
        flag_pattern: The regex the run used to recognize flags
            (e.g. ``OZ\\{[^{}\\s]+\\}`` for the synthetic lab).
    """

    model_config = ConfigDict(extra="forbid")

    target_name: str = Field(min_length=1)
    target_category: str = Field(min_length=1)
    target_description: str = Field(min_length=1)
    flag_pattern: str | None = None


class TraceMetrics(BaseModel):
    """The nine documented Model-Harness Matrix metrics (PR28).

    Every field is a deterministic function of the recorded interactions
    (see docs/GOLDEN_TRACES.md, "Metrics"); rates are in ``[0, 1]`` and
    ratios are ``>= 0``. Exact float equality is the comparison
    contract: metrics are computed from the same recorded interactions,
    so a re-verification must reproduce them exactly.
    """

    model_config = ConfigDict(extra="forbid")

    valid_output_rate: float = Field(ge=0.0, le=1.0)
    correct_tool_selection: float = Field(ge=0.0, le=1.0)
    repetition_rate: float = Field(ge=0.0, le=1.0)
    recovery_rate: float = Field(ge=0.0, le=1.0)
    output_tokens_per_decision: float = Field(ge=0.0)
    steps_per_objective: float = Field(ge=0.0)
    solve_rate: float = Field(ge=0.0, le=1.0)
    unsupported_fact_rate: float = Field(ge=0.0, le=1.0)
    unsupported_flag_rate: float = Field(ge=0.0, le=1.0)


class TraceEntity(BaseModel):
    """One expected graph entity, mirroring :class:`EntityRecord`.

    Attributes:
        id: Stable entity id.
        type: Entity kind (e.g. ``run``, ``action``, ``observation``).
        data: Entity payload.
        created_at: UTC creation timestamp (replay reproduces it from
            the event's ``at`` field).
        updated_at: UTC last-update timestamp.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    data: dict[str, object]
    created_at: datetime
    updated_at: datetime


class TraceEdge(BaseModel):
    """One expected graph edge, mirroring :class:`EdgeRecord`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    src_id: str
    dst_id: str
    data: dict[str, object]
    created_at: datetime


class ExpectedGraph(BaseModel):
    """The expected final graph: entity set, edge set, and graph hash.

    The entity and edge lists are ordered by id (the graph's canonical
    read order), so the JSON document is deterministic; set membership
    is what verification compares.

    Attributes:
        entities: Expected entities, ordered by id.
        edges: Expected edges, ordered by id.
        graph_hash: Expected :meth:`StateGraph.graph_hash` digest.
    """

    model_config = ConfigDict(extra="forbid")

    entities: list[TraceEntity]
    edges: list[TraceEdge]
    graph_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TraceToolOutput(BaseModel):
    """One recorded tool output (a bounded shell run).

    Attributes:
        turn: 1-based turn index of the action in the run.
        command: The exact command line that ran.
        exit_code: Process exit code (``None`` if never exited).
        stdout: Captured stdout, bounded.
        stderr: Captured stderr, bounded.
        timeout_state: True when the run was killed by its timeout.
    """

    model_config = ConfigDict(extra="forbid")

    turn: int = Field(ge=1)
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    timeout_state: bool = False


class GoldenTrace(BaseModel):
    """The complete golden trace document (docs/GOLDEN_TRACES.md).

    Attributes:
        format: Fixed :data:`TRACE_FORMAT` marker.
        format_version: Trace document format version.
        schema_version: Graph schema version observed at capture time.
        challenge: The challenge input (target identity + flag pattern).
        events: The run's append-only event log lines, in order, as
            parsed JSON objects (the replayable artifact).
        model_responses: The raw model completions, in order.
        tool_outputs: The recorded tool outputs, in order.
        expected_graph: The expected final entity/edge set and hash.
        expected_metrics: The expected nine metrics.
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["ozzgraph.golden_trace"] = TRACE_FORMAT
    format_version: int = TRACE_FORMAT_VERSION
    schema_version: int = Field(ge=1)
    challenge: TraceChallenge
    events: list[dict[str, object]]
    model_responses: list[str]
    tool_outputs: list[TraceToolOutput]
    expected_graph: ExpectedGraph
    expected_metrics: TraceMetrics

    @field_validator("events")
    @classmethod
    def _events_are_objects(cls, events: list[dict[str, object]]) -> list[dict[str, object]]:
        """Reject non-object event entries loudly.

        Raises:
            TypeError: If any event entry is not a JSON object.
        """
        for entry in events:
            if not isinstance(entry, dict):
                raise TypeError("every event must be a JSON object")
        return events


class TraceMismatch(BaseModel):
    """One structured comparison failure (prompt-regression visibility).

    Attributes:
        path: Where the mismatch lives, e.g. ``expected_graph.graph_hash``
            or ``expected_metrics.solve_rate``.
        kind: Mismatch class: ``hash``, ``entity_set``, ``edge_set``,
            ``metric``, or ``schema``.
        detail: Human-readable description of expected vs actual.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str
    detail: str


class TraceVerification(BaseModel):
    """Outcome of verifying one golden trace.

    Attributes:
        ok: True when every comparison matched exactly.
        replayed_hash: The graph hash replay reconstructed.
        replayed_schema_version: The graph schema version replay
            reconstructed (the current migrations).
        mismatches: The structured diff; empty when ``ok``.
        trace_path: Path of the verified trace file, when known.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    replayed_hash: str
    replayed_schema_version: int
    mismatches: list[TraceMismatch]
    trace_path: str | None = None

    def assert_ok(self) -> None:
        """Raise :class:`TraceVerificationError` when the trace mismatches.

        The error message is the full structured diff, so gate contexts
        (CI, commit hooks) fail loudly with the regression visible.
        """
        if self.ok:
            return
        lines = [f"golden trace mismatch ({len(self.mismatches)}):"]
        for mismatch in self.mismatches:
            lines.append(f"- {mismatch.path} [{mismatch.kind}]: {mismatch.detail}")
        raise TraceVerificationError("\n".join(lines))


def _load_events(events_path: Path) -> list[dict[str, object]]:
    """Read the append-only JSONL log into a list of parsed objects.

    Raises:
        TraceError: If the log is unreadable or any line is not a JSON
            object (fail loudly — a capture must never silently drop
            events).
    """
    events: list[dict[str, object]] = []
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise TraceError(
                        f"event log line {line_number} is not valid JSON: {exc}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise TraceError(f"event log line {line_number} is not a JSON object")
                events.append(parsed)
    except OSError as exc:
        raise TraceError(f"could not read event log {events_path}: {exc}") from exc
    return events


def _write_events(events: list[dict[str, object]], events_path: Path) -> None:
    """Rewrite the trace's events as one JSONL line each (for replay)."""
    with events_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _canonical_record(record: TraceEntity | TraceEdge) -> str:
    """One canonical JSON line per record, for set comparison.

    Uses the same canonical serialization style as the graph hash
    (sorted keys, compact separators) so comparisons depend only on
    content, never on dict insertion order.
    """
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _compare_sets(
    expected: list[TraceEntity] | list[TraceEdge],
    actual: list[TraceEntity] | list[TraceEdge],
) -> tuple[list[str], list[str], list[str]]:
    """Diff two id-ordered record lists.

    Returns ``(missing, extra, changed)``: ``missing`` ids expected but
    absent, ``extra`` ids present but unexpected, ``changed`` ids whose
    canonical content differs.
    """
    expected_by_id = {record.id: _canonical_record(record) for record in expected}
    actual_by_id = {record.id: _canonical_record(record) for record in actual}
    missing = sorted(expected_by_id.keys() - actual_by_id.keys())
    extra = sorted(actual_by_id.keys() - expected_by_id.keys())
    changed = sorted(
        record_id
        for record_id in expected_by_id.keys() & actual_by_id.keys()
        if expected_by_id[record_id] != actual_by_id[record_id]
    )
    return missing, extra, changed


def _entity_to_trace(record: EntityRecord) -> TraceEntity:
    """Convert an :class:`EntityRecord` to the trace's JSON shape."""

    return TraceEntity(
        id=record.id,
        type=record.type,
        data=record.data,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _edge_to_trace(record: EdgeRecord) -> TraceEdge:
    """Convert an :class:`EdgeRecord` to the trace's JSON shape."""

    return TraceEdge(
        id=record.id,
        type=record.type,
        src_id=record.src_id,
        dst_id=record.dst_id,
        data=record.data,
        created_at=record.created_at,
    )


async def _snapshot_graph(graph: StateGraph) -> ExpectedGraph:
    """Snapshot the live graph's canonical content (entities, edges, hash)."""

    return ExpectedGraph(
        entities=[_entity_to_trace(record) for record in await graph.list_entities()],
        edges=[_edge_to_trace(record) for record in await graph.list_edges()],
        graph_hash=await graph.graph_hash(),
    )


async def capture_trace(
    trace_path: Path,
    *,
    events_path: Path,
    graph: StateGraph,
    challenge: TraceChallenge,
    metrics: TraceMetrics,
    model_responses: list[str] | tuple[str, ...] = (),
    tool_outputs: list[TraceToolOutput] | tuple[TraceToolOutput, ...] = (),
) -> GoldenTrace:
    """Capture a real run's events, final graph, and metrics into a trace.

    The event log is embedded verbatim (parsed objects, in file order),
    the live graph is snapshotted through the canonical reads, and the
    resulting document is written to ``trace_path`` as deterministic
    JSON (``indent=2``, fixed field order).

    Args:
        trace_path: Where to write the trace JSON document.
        events_path: The run's append-only JSONL event log.
        graph: The OPEN live graph to snapshot (entities, edges, hash,
            schema version).
        challenge: The challenge input the run was pointed at.
        metrics: The run's nine metrics (typically from
            :mod:`ozzgraph.matrix`).
        model_responses: The raw model completions, in order.
        tool_outputs: The recorded tool outputs, in order.

    Returns:
        The captured trace document (also written to ``trace_path``).

    Raises:
        TraceError: If the event log is unreadable or malformed, or the
            trace cannot be written.
    """
    events = _load_events(events_path)
    trace = GoldenTrace(
        schema_version=await graph.schema_version(),
        challenge=challenge,
        events=events,
        model_responses=list(model_responses),
        tool_outputs=list(tool_outputs),
        expected_graph=await _snapshot_graph(graph),
        expected_metrics=metrics,
    )
    try:
        trace_path.write_text(trace.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise TraceError(f"could not write trace {trace_path}: {exc}") from exc
    return trace


async def _compare_graphs(
    trace: GoldenTrace, replayed: StateGraph
) -> tuple[list[TraceMismatch], str, int]:
    """Compare the expected graph against the replayed graph.

    Compares the trace's captured schema version, the entity set, the
    edge set, and the graph hash. Every comparison is deterministic; a
    schema mismatch is reported separately from the resulting hash
    mismatch so migration regressions are visible as such.

    Returns:
        ``(mismatches, replayed_hash, replayed_schema_version)``.
    """
    mismatches: list[TraceMismatch] = []
    replayed_schema = await replayed.schema_version()
    if replayed_schema != trace.schema_version:
        mismatches.append(
            TraceMismatch(
                path="schema_version",
                kind="schema",
                detail=(
                    f"trace captured under graph schema version {trace.schema_version}, "
                    f"replay produced {replayed_schema} (schema migration since capture?)"
                ),
            )
        )
    replayed_hash = await replayed.graph_hash()
    if replayed_hash != trace.expected_graph.graph_hash:
        mismatches.append(
            TraceMismatch(
                path="expected_graph.graph_hash",
                kind="hash",
                detail=(
                    f"expected {trace.expected_graph.graph_hash}, replay produced {replayed_hash}"
                ),
            )
        )
    actual_entities = [_entity_to_trace(record) for record in await replayed.list_entities()]
    actual_edges = [_edge_to_trace(record) for record in await replayed.list_edges()]
    missing, extra, changed = _compare_sets(trace.expected_graph.entities, actual_entities)
    if missing or extra or changed:
        mismatches.append(
            TraceMismatch(
                path="expected_graph.entities",
                kind="entity_set",
                detail=_set_diff_detail("entities", missing, extra, changed),
            )
        )
    missing, extra, changed = _compare_sets(trace.expected_graph.edges, actual_edges)
    if missing or extra or changed:
        mismatches.append(
            TraceMismatch(
                path="expected_graph.edges",
                kind="edge_set",
                detail=_set_diff_detail("edges", missing, extra, changed),
            )
        )
    return mismatches, replayed_hash, replayed_schema


def _set_diff_detail(label: str, missing: list[str], extra: list[str], changed: list[str]) -> str:
    """One human-readable line for an entity/edge set mismatch."""
    parts: list[str] = []
    if missing:
        parts.append(f"missing {label}: {', '.join(missing)}")
    if extra:
        parts.append(f"unexpected {label}: {', '.join(extra)}")
    if changed:
        parts.append(f"changed {label}: {', '.join(changed)}")
    return "; ".join(parts)


def _compare_metrics(expected: TraceMetrics, actual: TraceMetrics) -> list[TraceMismatch]:
    """Compare every metric field with exact equality.

    Returns one :class:`TraceMismatch` per differing field. Exact float
    equality is the contract: metrics are deterministic functions of the
    recorded interactions, so a re-verification must reproduce them
    exactly (docs/GOLDEN_TRACES.md, "Metrics").
    """
    mismatches: list[TraceMismatch] = []
    for field in TraceMetrics.model_fields:
        expected_value = getattr(expected, field)
        actual_value = getattr(actual, field)
        if expected_value == actual_value:
            continue
        mismatches.append(
            TraceMismatch(
                path=f"expected_metrics.{field}",
                kind="metric",
                detail=f"expected {expected_value!r}, replayed {actual_value!r}",
            )
        )
    return mismatches


async def verify_trace(
    trace_path: Path,
    *,
    db_path: Path | None = None,
    actual_metrics: TraceMetrics | None = None,
) -> TraceVerification:
    """Replay a golden trace and compare it against its expectations.

    The trace's events are rewritten to a fresh JSONL file and replayed
    through :func:`~ozzgraph.replay.replay_into` (a fresh database runs
    the current migrations, preserving the schema version), then the
    reconstructed entity set, edge set, and graph hash are compared with
    the trace's expected graph. When ``actual_metrics`` is supplied
    (the run's metrics, recomputed deterministically from the recorded
    interactions), they are compared field by field against the trace's
    expected metrics. Every mismatch is reported as a structured
    :class:`TraceMismatch`; nothing is silently skipped.

    Args:
        trace_path: Path to the trace JSON document.
        db_path: Optional path for the replayed database. When None a
            fresh temporary file is used and removed afterwards.
        actual_metrics: The metrics of the run being verified, for the
            metric comparison; when None the metric comparison is
            skipped (replay reconstructs the graph, not the metrics).

    Returns:
        The :class:`TraceVerification` carrying the structured diff.

    Raises:
        TraceError: If the trace document is unreadable or
            schema-invalid (fail loudly, AGENTS.md rule #9).
    """
    try:
        raw = trace_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceError(f"could not read trace {trace_path}: {exc}") from exc
    try:
        trace = GoldenTrace.model_validate_json(raw)
    except Exception as exc:  # pydantic ValidationError or JSONDecodeError
        raise TraceError(f"trace {trace_path} is not a valid golden trace: {exc}") from exc

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        if db_path is None:
            temporary_directory = tempfile.TemporaryDirectory(prefix="ozz-trace-verify-")
            events_path = Path(temporary_directory.name) / "actions.jsonl"
            replay_db_path = Path(temporary_directory.name) / "replay.db"
        else:
            events_path = db_path.parent / f"{db_path.name}.events.jsonl"
            replay_db_path = db_path
        _write_events(trace.events, events_path)
        try:
            replayed = await replay_into(events_path, replay_db_path)
        except ReplayMalformedEventError as exc:
            raise TraceError(f"trace {trace_path} events fail replay: {exc}") from exc
        try:
            mismatches, replayed_hash, replayed_schema = await _compare_graphs(trace, replayed)
            if actual_metrics is not None:
                mismatches.extend(_compare_metrics(trace.expected_metrics, actual_metrics))
            return TraceVerification(
                ok=not mismatches,
                replayed_hash=replayed_hash,
                replayed_schema_version=replayed_schema,
                mismatches=mismatches,
                trace_path=str(trace_path),
            )
        finally:
            await replayed.close()
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def verify_metrics(expected: TraceMetrics, actual: TraceMetrics) -> list[TraceMismatch]:
    """Compare two metric sets with the trace's exact-equality contract.

    Returns the structured diff (empty when identical). Exposed so a
    capture-then-verify flow can check a run's metrics without
    re-running anything: metrics are deterministic functions of the
    recorded interactions (docs/GOLDEN_TRACES.md, "Metrics").
    """
    return _compare_metrics(expected, actual)


def load_trace(trace_path: Path) -> GoldenTrace:
    """Load and validate a trace document without replaying it.

    Raises:
        TraceError: If the document is unreadable or schema-invalid.
    """
    try:
        raw = trace_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceError(f"could not read trace {trace_path}: {exc}") from exc
    try:
        return GoldenTrace.model_validate_json(raw)
    except Exception as exc:
        raise TraceError(f"trace {trace_path} is not a valid golden trace: {exc}") from exc
