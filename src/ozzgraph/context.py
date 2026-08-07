"""Bounded context compiler for OzzGraph (PR16).

Implements the CONTEXT COMPILER layer (docs/ARCHITECTURE.md, "Context
Compiler"; docs/DATA_STRATEGY.md, "Context Retrieval"; PR step 16 of
docs/IMPLEMENTATION_PLAN.md): the deterministic bridge between the
authoritative SQLite state graph and the model adapters.

The compiler turns one structured :class:`ContextRequest` into a
BOUNDED, model-specific view of the graph, rendering the six context
layers into exactly the shape :meth:`ModelAdapter.compile_prompt`
consumes:

1. immutable mission context -> ``mission``
2. active task context (phase, task, targets, services, hypotheses,
   artifacts) -> head of ``graph_summary``
3. relevant graph projection -> ``graph_summary``
4. recent transcript tail -> ``transcript_tail``
5. loaded skills -> ``skills`` (capped by
   :attr:`ModelProfile.max_advertised_skills`)
6. output contract -> ``output_contract``

Relevance (docs/DATA_STRATEGY.md): the projection starts from the
explicitly referenced anchor entities (active task, targets, services,
hypotheses, artifacts), expands each anchor ONE hop through its typed
edges (:meth:`StateGraph.neighbors`), and sweeps phase-tagged entities
of the relevant types when the request names a phase
(:meth:`StateGraph.list_entities`). Candidate entities must pass the
recency window and confidence floor filters; contradiction edges
(types containing ``CONTRADICT``, e.g. ``EVIDENCE CONTRADICTS
HYPOTHESIS``) are excluded on request. The complete graph is never
dumped: entities are ranked anchor > neighbor > phase-tagged and
rendered under the profile's ``context_soft_limit`` budget with
deterministic truncation markers.

The module is deterministic: every ordering is explicit (anchors
sorted by id, entities by relevance tier then ``(type, id)``, edges by
``(type, src_id, dst_id)``), rendering uses canonical JSON and ISO-8601
timestamps, and no set iteration order leaks into output. The same
graph, request, and (when a recency window is set) ``now`` always
produce byte-identical output; callers replaying a turn must pin
``now`` exactly as :meth:`StateGraph` callers pin ``at``.

Failures are loud, mirroring the :class:`~ozzgraph.state_graph.StateGraphError`
hierarchy: :class:`ContextReferenceError` when the request references
an entity that does not exist in the graph, :class:`ContextBudgetError`
when the fixed layers (mission, output contract, capped skills) alone
cannot fit the profile's soft limit. The module holds no global
mutable state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.profiles import ModelProfile
from ozzgraph.state_graph import EdgeRecord, EntityRecord, StateGraph

#: Fixed marker appended to the graph projection (and reported via
#: :attr:`CompiledContext.truncated`) when the profile's soft limit
#: cannot fit the whole selected projection. Fixed text keeps
#: truncation deterministic: the exact omitted counts are carried in
#: :attr:`CompiledContext.omitted_entity_count` /
#: :attr:`CompiledContext.omitted_edge_count`.
_TRUNCATION_MARKER = "[TRUNCATED: context budget exceeded]"

#: Per-entity payload rendered at most this many characters in one
#: projection line (plus ``...``). Keeps every entity line bounded so
#: anchors always fit small budgets.
_MAX_DATA_CHARS = 200

#: Transcript tail receives at most one quarter of the variable budget
#: (the graph projection keeps at least three quarters), a
#: deterministic split that never starves the projection.
_TAIL_BUDGET_DIVISOR = 4

#: Entity types the phase sweep considers relevant (the anchor kinds
#: from docs/DATA_STRATEGY.md, stored lowercase by convention). A
#: phase-tagged entity of one of these types is projected even when
#: nothing else connects it to an anchor.
_PHASE_SWEEP_TYPES = ("task", "target", "service", "hypothesis", "artifact")

#: Substring marking contradiction edges, e.g. ``EVIDENCE CONTRADICTS
#: HYPOTHESIS`` (docs/DATA_STRATEGY.md core relationships).
_CONTRADICTS = "CONTRADICT"


class ContextError(RuntimeError):
    """Base error for the context compiler layer (AGENTS.md rule #9)."""


class ContextReferenceError(ContextError):
    """A request references an entity that does not exist in the graph.

    Anchors (active task, targets, services, hypotheses, artifacts)
    are explicit references; a missing anchor is a broken request, not
    something to paper over silently.
    """

    def __init__(self, entity_id: str) -> None:
        super().__init__(
            f"request references entity {entity_id!r} which does not exist in the graph"
        )
        self.entity_id = entity_id


class ContextBudgetError(ContextError):
    """The fixed context layers alone exceed the profile's soft limit.

    Mission, output contract, and capped skills are never truncated
    (the mission is immutable by design), so a request whose fixed
    layers cannot fit the profile's ``context_soft_limit`` fails
    loudly instead of silently dropping content.

    Attributes:
        budget: The profile's ``context_soft_limit`` in characters.
        required: Characters required by the fixed layers alone.
    """

    def __init__(self, *, budget: int, required: int) -> None:
        super().__init__(
            f"fixed context ({required} chars) exceeds context_soft_limit ({budget} chars)"
        )
        self.budget = budget
        self.required = required


class ContextRequest(BaseModel):
    """One structured request to the context compiler.

    Carries every query dimension from docs/DATA_STRATEGY.md
    ("Context Retrieval") plus the pass-through layers: mission (layer
    1), transcript tail (layer 4), skill summaries (layer 5), and
    output contract (layer 6). Anchors are explicit entity IDs; each
    must exist in the graph or compilation raises
    :class:`ContextReferenceError`.
    """

    model_config = ConfigDict(extra="forbid")

    mission: str = Field(min_length=1)
    active_task_id: str | None = None
    target_ids: tuple[str, ...] = ()
    service_ids: tuple[str, ...] = ()
    hypothesis_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    phase: str | None = None
    recency_window: timedelta | None = None
    confidence_floor: float = Field(default=0.0, ge=0.0, le=1.0)
    include_contradictions: bool = True
    transcript_tail: str = ""
    skills: tuple[str, ...] = ()
    output_contract: str = ""


class CompiledContext(BaseModel):
    """The compiled, bounded context in the adapter contract shape.

    ``mission``, ``graph_summary``, ``transcript_tail``, ``skills``,
    and ``output_contract`` are passed straight to
    :meth:`ModelAdapter.compile_prompt`. The remaining fields are
    deterministic accounting metadata for callers and tests: whether
    any layer was truncated, how much of the projection survived, and
    how the rendered output spends the profile budget. The invariant
    ``used_chars <= budget_chars`` always holds.
    """

    model_config = ConfigDict(extra="forbid")

    mission: str
    graph_summary: str
    transcript_tail: str
    skills: tuple[str, ...]
    output_contract: str
    truncated: bool
    entities_included: int = Field(ge=0)
    edges_included: int = Field(ge=0)
    omitted_entity_count: int = Field(ge=0)
    omitted_edge_count: int = Field(ge=0)
    budget_chars: int = Field(ge=1)
    used_chars: int = Field(ge=0)


@dataclass(frozen=True)
class _ProjectionRender:
    """Result of rendering the bounded projection under a char budget."""

    text: str
    truncated: bool
    entities_included: int
    edges_included: int
    omitted_entity_count: int
    omitted_edge_count: int


@dataclass(frozen=True)
class _Filters:
    """Recency and confidence filters applied to candidate entities.

    Anchors (explicitly referenced entities) bypass these filters: an
    explicitly requested entity is included even when stale or
    low-confidence. Only neighborhood and phase-sweep candidates are
    filtered.
    """

    now: datetime
    recency_window: timedelta | None
    confidence_floor: float

    def passes(self, record: EntityRecord) -> bool:
        return _passes_recency(record, self.now, self.recency_window) and _passes_confidence(
            record, self.confidence_floor
        )


def _now(at: datetime | None) -> datetime:
    """The recency reference instant: ``at`` normalized to UTC, else now.

    Raises:
        ValueError: If ``at`` is naive (missing timezone information).
    """
    if at is None:
        return datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    return at.astimezone(UTC)


def _passes_recency(record: EntityRecord, now: datetime, window: timedelta | None) -> bool:
    """True when the entity was updated within ``window`` of ``now``.

    No window means every entity passes (recency is not enforced).
    The comparison is inclusive on the boundary (``updated_at >= now -
    window``), mirroring the ``>=`` recency of the request semantics.
    """
    if window is None:
        return True
    return record.updated_at >= now - window


def _passes_confidence(record: EntityRecord, floor: float) -> bool:
    """True when the entity's declared confidence is at or above ``floor``.

    Entities whose payload carries no numeric ``confidence`` key are
    not judged and always pass; a payload confidence below the floor
    fails. ``bool`` is excluded from the numeric check (it is an
    ``int`` subclass in Python).
    """
    if floor <= 0:
        return True
    confidence = record.data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return True
    return confidence >= floor


def _cap_skills(skills: Sequence[str], cap: int) -> tuple[str, ...]:
    """Deterministically cap the advertised skill list.

    Keeps the first ``cap`` entries in request order: the caller
    orders skills by priority, and the compiler never reorders or
    hash-scrambles them. ``cap == 0`` (e.g. the fallback profile)
    yields no skills.
    """
    return tuple(skills[:cap])


def _truncate_tail(tail: str, cap: int) -> tuple[str, bool, int]:
    """Truncate the transcript tail to its budget share, keeping the END.

    A transcript tail is the most RECENT lines, so truncation keeps
    the last ``cap`` characters and prepends a marker naming the
    omitted leading characters. When the marker cannot fit the cap the
    tail is dropped entirely (the marker and the budget share would
    both be meaningless).
    """
    if len(tail) <= cap:
        return tail, False, 0
    omitted = len(tail) - cap
    marker = f"[TRANSCRIPT TRUNCATED: omitted {omitted} leading chars]"
    if len(marker) + 1 >= cap:
        return "", True, omitted
    keep = cap - len(marker) - 1
    return marker + "\n" + tail[-keep:], True, omitted


def _anchor_ids(request: ContextRequest) -> tuple[str, ...]:
    """The deduplicated, sorted anchor entity IDs from a request."""
    ids: list[str] = []
    if request.active_task_id is not None:
        ids.append(request.active_task_id)
    ids.extend(request.target_ids)
    ids.extend(request.service_ids)
    ids.extend(request.hypothesis_ids)
    ids.extend(request.artifact_ids)
    return tuple(sorted(set(ids)))


def _fmt_iso(value: datetime) -> str:
    """Deterministic ISO-8601 rendering (same format as storage)."""
    return value.isoformat()


def _entity_line(record: EntityRecord) -> str:
    """One bounded projection line for an entity.

    Payload JSON is canonical (sorted keys, compact separators) and
    capped at ``_MAX_DATA_CHARS`` so a single line can never blow the
    budget by itself.
    """
    payload = json.dumps(record.data, sort_keys=True, separators=(",", ":")) if record.data else ""
    if len(payload) > _MAX_DATA_CHARS:
        payload = payload[:_MAX_DATA_CHARS] + "..."
    suffix = f" data={payload}" if payload else ""
    return f"- {record.id} ({record.type}) updated={_fmt_iso(record.updated_at)}{suffix}"


def _edge_line(record: EdgeRecord) -> str:
    """One projection line for an edge: ``src --[TYPE]--> dst``."""
    return f"- {record.src_id} --[{record.type}]--> {record.dst_id}"


def _render_active_context(request: ContextRequest, anchors: Mapping[str, EntityRecord]) -> str:
    """Layer 2: the active task context, or "" when nothing is named.

    Rendered as the head of ``graph_summary``; every line is derived
    deterministically from the request and the resolved anchors.
    """
    lines: list[str] = []
    if request.phase is not None:
        lines.append(f"- phase: {request.phase}")
    if request.active_task_id is not None:
        record = anchors[request.active_task_id]
        lines.append(f"- active task: {record.id} ({record.type})")
    if request.target_ids:
        lines.append(f"- targets: {', '.join(request.target_ids)}")
    if request.service_ids:
        lines.append(f"- services: {', '.join(request.service_ids)}")
    if request.hypothesis_ids:
        lines.append(f"- hypotheses: {', '.join(request.hypothesis_ids)}")
    if request.artifact_ids:
        lines.append(f"- artifacts: {', '.join(request.artifact_ids)}")
    if request.recency_window is not None:
        lines.append(f"- recency window: {int(request.recency_window.total_seconds())}s")
    if request.confidence_floor > 0:
        lines.append(f"- confidence floor: {request.confidence_floor:g}")
    if not request.include_contradictions:
        lines.append("- contradictions: excluded")
    if not lines:
        return ""
    return "ACTIVE CONTEXT\n" + "\n".join(lines) + "\n"


def _render_projection(
    active_section: str,
    entities: Sequence[EntityRecord],
    edges: Sequence[EdgeRecord],
    graph_budget: int,
) -> _ProjectionRender:
    """Render the projection under a deterministic char budget.

    Section headers and ``(none)`` placeholders are fixed overhead;
    entity lines (anchors first, by relevance tier) are greedy-added
    before edge lines, each reserved against the truncation marker.
    The result always satisfies ``len(text) <= graph_budget``: when
    every line fits no marker is emitted, otherwise the fixed marker
    closes the projection and the omitted counts are reported through
    the returned struct.
    """
    entity_lines = [_entity_line(record) for record in entities]
    edge_lines = [_edge_line(record) for record in edges]
    overhead_parts: list[str] = []
    if active_section:
        overhead_parts.append(active_section)
    overhead_parts.append("PROJECTED ENTITIES\n")
    if not entity_lines:
        overhead_parts.append("(none)\n")
    overhead_parts.append("PROJECTED EDGES\n")
    if not edge_lines:
        overhead_parts.append("(none)\n")
    overhead = "".join(overhead_parts)
    if len(overhead) >= graph_budget:
        # Pathological budget: not even the section headers fit.
        truncated = bool(entity_lines) or bool(edge_lines)
        return _ProjectionRender(
            overhead[:graph_budget],
            truncated,
            len(entities) - (len(entities) if truncated else 0),
            len(edges) - (len(edges) if truncated else 0),
            len(entities) if truncated else 0,
            len(edges) if truncated else 0,
        )
    marker = _TRUNCATION_MARKER
    if len(marker) > graph_budget - len(overhead):
        marker = marker[: graph_budget - len(overhead)]
    content_budget = graph_budget - len(overhead) - len(marker)
    content = entity_lines + edge_lines
    included: list[str] = []
    used = 0
    for line in content:
        if used + len(line) + 1 <= content_budget:
            included.append(line)
            used += len(line) + 1
        else:
            break
    truncated = len(included) < len(content)
    if truncated:
        included_count = len(included)
        if included_count >= len(entity_lines):
            omitted_entities = 0
            omitted_edges = len(edge_lines) - (included_count - len(entity_lines))
        else:
            omitted_entities = len(entity_lines) - included_count
            omitted_edges = len(edge_lines)
        text = overhead + "".join(f"{line}\n" for line in included) + marker
    else:
        omitted_entities = 0
        omitted_edges = 0
        text = overhead + "".join(f"{line}\n" for line in content)
    return _ProjectionRender(
        text=text,
        truncated=truncated,
        entities_included=len(entities) - omitted_entities,
        edges_included=len(edges) - omitted_edges,
        omitted_entity_count=omitted_entities,
        omitted_edge_count=omitted_edges,
    )


async def _resolve_anchors(graph: StateGraph, anchor_ids: Sequence[str]) -> dict[str, EntityRecord]:
    """Fetch every anchor, failing loudly on any missing reference."""
    anchors: dict[str, EntityRecord] = {}
    for entity_id in anchor_ids:
        record = await graph.get_entity(entity_id)
        if record is None:
            raise ContextReferenceError(entity_id)
        anchors[entity_id] = record
    return anchors


async def _expand_neighbors(
    graph: StateGraph,
    anchors: Mapping[str, EntityRecord],
    filters: _Filters,
    include_contradictions: bool,
) -> dict[str, EntityRecord]:
    """One-hop expansion: neighbors of anchors passing the filters.

    Anchor endpoints of an edge are skipped (already selected). When
    the request excludes contradictions, edges whose type carries
    ``CONTRADICT`` are skipped entirely, so an entity reachable ONLY
    through contradiction edges is never projected; entities with any
    other connection still qualify.
    """
    candidates: dict[str, EntityRecord] = {}
    for anchor_id in sorted(anchors):
        neighbors = await graph.neighbors(anchor_id)
        for edge in (*neighbors.outgoing, *neighbors.incoming):
            if not include_contradictions and _CONTRADICTS in edge.type:
                continue
            other_id = edge.dst_id if edge.src_id == anchor_id else edge.src_id
            if other_id in anchors:
                continue
            if other_id in candidates:
                continue
            record = await graph.get_entity(other_id)
            if record is None:  # pragma: no cover - FK guarantees endpoints exist
                continue
            if filters.passes(record):
                candidates[other_id] = record
    return candidates


async def _sweep_phase(
    graph: StateGraph,
    phase: str,
    filters: _Filters,
) -> dict[str, EntityRecord]:
    """Phase dimension: phase-tagged entities of the relevant types.

    Scans :meth:`StateGraph.list_entities` per relevant type and keeps
    entities whose payload ``phase`` field equals the request's phase,
    passing the filters. Anchors/neighbors are never re-added (the
    caller deduplicates).
    """
    found: dict[str, EntityRecord] = {}
    for entity_type in _PHASE_SWEEP_TYPES:
        for record in await graph.list_entities(entity_type):
            if record.data.get("phase") != phase:
                continue
            if filters.passes(record):
                found[record.id] = record
    return found


async def _projection_edges(
    graph: StateGraph,
    selected_ids: set[str],
    include_contradictions: bool,
) -> list[EdgeRecord]:
    """Edges whose endpoints are both selected, deduplicated and sorted.

    Contradiction edges are dropped entirely when the request excludes
    them (both the edge and its neighbor endpoint vanish from the
    projection).
    """
    edge_map: dict[tuple[str, str, str], EdgeRecord] = {}
    for entity_id in sorted(selected_ids):
        neighbors = await graph.neighbors(entity_id)
        for edge in (*neighbors.outgoing, *neighbors.incoming):
            if not include_contradictions and _CONTRADICTS in edge.type:
                continue
            if edge.src_id in selected_ids and edge.dst_id in selected_ids:
                edge_map[(edge.type, edge.src_id, edge.dst_id)] = edge
    return sorted(edge_map.values(), key=lambda edge: (edge.type, edge.src_id, edge.dst_id))


async def compile_context(
    graph: StateGraph,
    profile: ModelProfile,
    request: ContextRequest,
    *,
    now: datetime | None = None,
) -> CompiledContext:
    """Compile the bounded, model-specific context for one request.

    Queries ``graph`` exclusively through its public API
    (:meth:`StateGraph.get_entity`, :meth:`StateGraph.neighbors`,
    :meth:`StateGraph.list_entities`) and renders the six context
    layers into the adapter contract shape.

    Budget accounting (characters): the mission, output contract, and
    skill list (capped by ``profile.max_advertised_skills``) are fixed
    and never truncated; the transcript tail is capped at a quarter of
    the remaining budget (keeping its END); the graph projection gets
    the rest and truncates deterministically with a marker when it
    cannot fit. The invariant ``CompiledContext.used_chars <=
    profile.context_soft_limit`` always holds.

    Determinism: identical graph state, request, and ``now`` yield
    byte-identical output. When ``request.recency_window`` is set,
    callers that replay a turn must pass the same ``now`` (default:
    UTC now, matching :meth:`StateGraph` timestamp semantics) — pass
    an explicit value for reproducible compilation.

    Args:
        graph: The open state graph (PR7).
        profile: The model profile bounding the view (PR13).
        request: What to include and how to filter it.
        now: Optional recency reference instant; defaults to UTC now.
            Naive datetimes are rejected.

    Raises:
        ContextReferenceError: If an anchor ID in ``request`` does not
            exist in the graph.
        ContextBudgetError: If mission + output contract + capped
            skills alone exceed ``profile.context_soft_limit``.
        ValueError: If ``now`` is naive.
        StateGraphError: If the graph is closed or a read fails.
    """
    reference_now = _now(now)
    capped_skills = _cap_skills(request.skills, profile.max_advertised_skills)
    fixed = (
        len(request.mission)
        + len(request.output_contract)
        + sum(len(skill) for skill in capped_skills)
    )
    budget = profile.context_soft_limit
    if fixed > budget:
        raise ContextBudgetError(budget=budget, required=fixed)
    remaining = budget - fixed
    tail_cap = remaining // _TAIL_BUDGET_DIVISOR
    transcript_tail, tail_truncated, _ = _truncate_tail(request.transcript_tail, tail_cap)
    graph_budget = remaining - len(transcript_tail)

    anchors = await _resolve_anchors(graph, _anchor_ids(request))
    filters = _Filters(
        now=reference_now,
        recency_window=request.recency_window,
        confidence_floor=request.confidence_floor,
    )
    selected: dict[str, EntityRecord] = dict(anchors)
    neighbors = await _expand_neighbors(graph, anchors, filters, request.include_contradictions)
    selected.update(neighbors)
    phase_sweep: dict[str, EntityRecord] = {}
    if request.phase is not None:
        phase_sweep = await _sweep_phase(graph, request.phase, filters)
        for entity_id, record in phase_sweep.items():
            if entity_id not in selected:
                selected[entity_id] = record

    # Relevance tiers for deterministic truncation: anchors (explicitly
    # referenced) survive first, then one-hop neighbors, then
    # phase-tagged entities. Within a tier, (type, id) keeps the order
    # explicit.
    tier: dict[str, int] = {entity_id: 0 for entity_id in anchors}
    for entity_id in neighbors:
        tier.setdefault(entity_id, 1)
    for entity_id in phase_sweep:
        tier.setdefault(entity_id, 2)
    ordered_entities = sorted(
        selected.values(), key=lambda record: (tier[record.id], record.type, record.id)
    )
    edges = await _projection_edges(graph, set(selected), request.include_contradictions)

    active_section = _render_active_context(request, anchors)
    render = _render_projection(active_section, ordered_entities, edges, graph_budget)
    used_chars = (
        len(request.mission)
        + len(render.text)
        + len(transcript_tail)
        + sum(len(skill) for skill in capped_skills)
        + len(request.output_contract)
    )
    return CompiledContext(
        mission=request.mission,
        graph_summary=render.text,
        transcript_tail=transcript_tail,
        skills=capped_skills,
        output_contract=request.output_contract,
        truncated=render.truncated or tail_truncated,
        entities_included=render.entities_included,
        edges_included=render.edges_included,
        omitted_entity_count=render.omitted_entity_count,
        omitted_edge_count=render.omitted_edge_count,
        budget_chars=budget,
        used_chars=used_chars,
    )
