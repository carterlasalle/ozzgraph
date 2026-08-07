"""Deterministic graph replay from the append-only event log (PR8).

Implements docs/DATA_STRATEGY.md ("Replay"): replaying all append-only
events in file order reconstructs the same entity and edge set and the
same graph hash, and preserves the schema version (a fresh database runs
the standard migrations, and the hash includes ``schema_version``).

Only the five ``graph.*`` mutation event types are applied
(:data:`GRAPH_EVENT_TYPES`); bootstrap, termination, and any future
event types are ignored, so adding new event kinds never breaks replay.
A graph event that cannot be parsed — invalid JSON, a payload that is
not an object, missing or mistyped fields, a naive timestamp — raises
:class:`ReplayMalformedEventError` immediately; replay never skips a
malformed graph event (fail loudly, AGENTS.md rule #9).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_EDGE_DELETED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_DELETED,
    GRAPH_ENTITY_UPDATED,
    GraphEdgeCreated,
    GraphEdgeDeleted,
    GraphEntityCreated,
    GraphEntityDeleted,
    GraphEntityUpdated,
)
from ozzgraph.state_graph import StateGraph

#: Event types replay applies; everything else is ignored.
GRAPH_EVENT_TYPES = frozenset(
    {
        GRAPH_ENTITY_CREATED,
        GRAPH_ENTITY_UPDATED,
        GRAPH_ENTITY_DELETED,
        GRAPH_EDGE_CREATED,
        GRAPH_EDGE_DELETED,
    }
)


class ReplayError(RuntimeError):
    """Base error for every replay failure."""


class ReplayMalformedEventError(ReplayError):
    """Raised when a graph event cannot be parsed or applied."""


async def replay_graph(events_path: Path, db_path: Path) -> str:
    """Replay ``events_path`` into a fresh database and return its hash.

    Every line of the log is applied in file order to a brand-new
    :class:`StateGraph` at ``db_path`` (the standard migrations run on
    open, so the schema version is preserved). Non-graph events are
    ignored; malformed graph events abort replay loudly.

    Args:
        events_path: Path to the append-only JSONL event log.
        db_path: Path for the fresh database; must not already hold
            graph state (replay always starts from an empty schema).

    Returns:
        The replayed graph's :meth:`StateGraph.graph_hash`. An empty or
        graph-free log yields the stable empty-graph hash.
    """
    graph = StateGraph(db_path)
    try:
        await graph.open()
        await _apply_log(events_path, graph)
        return await graph.graph_hash()
    finally:
        await graph.close()


async def replay_into(events_path: Path, db_path: Path) -> StateGraph:
    """Replay ``events_path`` and return the opened graph.

    The caller owns the returned graph and must close it. On any error
    the graph is closed before the error propagates, so no connection is
    leaked.

    Args:
        events_path: Path to the append-only JSONL event log.
        db_path: Path for the fresh database; must not already hold
            graph state.
    """
    graph = StateGraph(db_path)
    await graph.open()
    try:
        await _apply_log(events_path, graph)
    except BaseException:
        await graph.close()
        raise
    return graph


async def _apply_log(events_path: Path, graph: StateGraph) -> None:
    """Apply every graph event in ``events_path`` to ``graph``, in order."""
    with events_path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReplayMalformedEventError(
                    f"line {line_number}: not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ReplayMalformedEventError(f"line {line_number}: event is not a JSON object")
            event_type = parsed.get("event_type")
            if event_type not in GRAPH_EVENT_TYPES:
                continue
            raw_payload = parsed.get("payload")
            if not isinstance(raw_payload, dict):
                raise ReplayMalformedEventError(
                    f"line {line_number}: {event_type!r} payload is not an object"
                )
            await _apply_event(graph, str(event_type), raw_payload, line_number)


async def _apply_event(
    graph: StateGraph,
    event_type: str,
    payload: dict[str, object],
    line_number: int,
) -> None:
    """Validate one graph-event payload and apply it to ``graph``.

    Raises:
        ReplayMalformedEventError: If the payload fails validation
            (missing/mistyped fields, naive timestamp).
    """
    try:
        if event_type == GRAPH_ENTITY_CREATED:
            created = GraphEntityCreated.model_validate(payload)
            await graph.create_entity(
                created.entity_id, created.entity_type, created.data, at=created.at
            )
        elif event_type == GRAPH_ENTITY_UPDATED:
            updated = GraphEntityUpdated.model_validate(payload)
            await graph.update_entity(updated.entity_id, updated.data, at=updated.at)
        elif event_type == GRAPH_ENTITY_DELETED:
            deleted = GraphEntityDeleted.model_validate(payload)
            await graph.delete_entity(deleted.entity_id)
        elif event_type == GRAPH_EDGE_CREATED:
            edge = GraphEdgeCreated.model_validate(payload)
            await graph.create_edge(
                edge.edge_id,
                edge.edge_type,
                edge.src_id,
                edge.dst_id,
                edge.data,
                at=edge.at,
            )
        else:  # GRAPH_EDGE_DELETED
            removed = GraphEdgeDeleted.model_validate(payload)
            await graph.delete_edge(removed.edge_id)
    except ValidationError as exc:
        raise ReplayMalformedEventError(
            f"line {line_number}: invalid {event_type} payload: {exc}"
        ) from exc
