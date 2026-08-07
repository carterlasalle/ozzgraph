"""Tests for the bounded context compiler (PR16).

Covers the six context layers rendered into the adapter contract
shape, the bounded relevant-subgraph projection (anchors, one-hop
neighbors, phase sweep; recency, confidence, and contradiction
filters), graceful empty-graph degradation, loud missing-reference
errors, deterministic budget truncation with markers, byte-level
determinism, and the advertised-skill cap — the success and failure
paths AGENTS.md expects of kernel changes.

Every test uses its own ``tmp_path`` database and pinned timestamps;
recency-sensitive compiles pass an explicit ``now`` so results are
byte-deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ozzgraph.context import (
    CompiledContext,
    ContextBudgetError,
    ContextError,
    ContextReferenceError,
    ContextRequest,
    compile_context,
)
from ozzgraph.profiles import FALLBACK_PROFILE, GPT_PROFILE, ModelProfile
from ozzgraph.state_graph import StateGraph

T0 = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 6, 10, 5, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 6, 10, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 6, 10, 15, 0, tzinfo=UTC)

TRUNCATION_MARKER = "[TRUNCATED: context budget exceeded]"


async def _seed(graph: StateGraph) -> None:
    """Seed a small deterministic CTF-style graph.

    Timestamps are pinned so recency filtering is reproducible: T1
    entities fall outside a 5-minute window ending at NOW, T2
    entities fall inside it. ``ev-old`` is stale evidence, ``ev-2`` is
    low-confidence (0.3) and contradicts ``hyp-1``, ``art-1`` is an
    explicitly referenced anchor with low confidence, and ``tgt-2`` is
    only reachable through the phase sweep (payload ``phase``).
    """
    await graph.create_entity("run-1", "run", {"phase": "RECON"}, at=T0)
    await graph.create_entity("task-1", "task", {"description": "enumerate svc"}, at=T1)
    await graph.create_entity("tgt-1", "target", {"host": "10.0.0.5"}, at=T1)
    await graph.create_entity("svc-1", "service", {"port": 80}, at=T1)
    await graph.create_entity("hyp-1", "hypothesis", {"claim": "httpd", "confidence": 0.8}, at=T1)
    await graph.create_entity(
        "hyp-2", "hypothesis", {"claim": "httpd-v2", "confidence": 0.85}, at=T2
    )
    await graph.create_entity(
        "art-1", "artifact", {"path": "/tmp/x.txt", "confidence": 0.05}, at=T1
    )
    await graph.create_entity("tgt-2", "target", {"host": "10.0.0.6", "phase": "RECON"}, at=T2)
    await graph.create_entity(
        "ev-1", "evidence", {"detail": "Apache/2.4", "confidence": 0.95}, at=T2
    )
    await graph.create_entity("ev-2", "evidence", {"detail": "nginx", "confidence": 0.3}, at=T2)
    await graph.create_entity(
        "ev-old", "evidence", {"detail": "old banner", "confidence": 0.9}, at=T1
    )
    await graph.create_edge("e-task-tgt", "TASK TARGETS TARGET", "task-1", "tgt-1", at=T1)
    await graph.create_edge("e-svc-tgt", "SERVICE OBSERVED_ON TARGET", "svc-1", "tgt-1", at=T1)
    await graph.create_edge("e-hyp-svc", "HYPOTHESIS ABOUT SERVICE", "hyp-1", "svc-1", at=T1)
    await graph.create_edge("e-hyp2-svc", "HYPOTHESIS ABOUT SERVICE", "hyp-2", "svc-1", at=T1)
    await graph.create_edge("e-ev-hyp", "EVIDENCE SUPPORTS HYPOTHESIS", "ev-1", "hyp-1", at=T1)
    await graph.create_edge("e-ev2-hyp", "EVIDENCE CONTRADICTS HYPOTHESIS", "ev-2", "hyp-1", at=T1)
    await graph.create_edge("e-evold-hyp", "EVIDENCE SUPPORTS HYPOTHESIS", "ev-old", "hyp-1", at=T1)


def _request(**overrides: object) -> ContextRequest:
    """A full request against the seeded graph (test (a) shape)."""
    base = ContextRequest(
        mission="Capture the flag on the isolated target.",
        active_task_id="task-1",
        target_ids=("tgt-1",),
        service_ids=("svc-1",),
        hypothesis_ids=("hyp-1",),
        artifact_ids=("art-1",),
        phase="RECON",
        recency_window=timedelta(minutes=5),
        confidence_floor=0.7,
        transcript_tail="> banner: Apache/2.4\n",
        skills=("recon: nmap basics", "web: curl probing"),
        output_contract="Respond with exactly three lines: THOUGHT, ACTION, PAYLOAD.",
    )
    return base.model_copy(update=dict(overrides))


async def _compile(
    path: Path,
    profile: ModelProfile = GPT_PROFILE,
    request: ContextRequest | None = None,
    *,
    now: datetime | None = NOW,
    seed: bool = True,
) -> CompiledContext:
    """Open the graph DB at ``path`` (optionally seeding it) and compile."""
    async with StateGraph(path) as graph:
        if seed:
            await _seed(graph)
        return await compile_context(graph, profile, request or _request(), now=now)


# ---------------------------------------------------------------------------
# (a) success path — all six layers, content matches the seeded graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_renders_all_six_layers(tmp_path: Path) -> None:
    """Every layer renders; projection matches the seeded graph exactly."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await _seed(graph)
        request = _request()
        compiled = await compile_context(graph, GPT_PROFILE, request, now=NOW)

    # Layer 1 (mission), 4 (tail), 5 (skills), 6 (contract) pass through.
    assert compiled.mission == request.mission
    assert compiled.transcript_tail == request.transcript_tail
    assert compiled.skills == request.skills
    assert compiled.output_contract == request.output_contract
    assert not compiled.truncated

    summary = compiled.graph_summary
    # Layer 2 (active task context) heads the graph summary.
    assert summary.startswith("ACTIVE CONTEXT\n")
    assert "- phase: RECON" in summary
    assert "- active task: task-1 (task)" in summary
    assert "- targets: tgt-1" in summary
    assert "- recency window: 300s" in summary
    assert "- confidence floor: 0.7" in summary
    assert "PROJECTED ENTITIES\n" in summary
    assert "PROJECTED EDGES\n" in summary

    # Layer 3: anchors survive regardless of age/confidence...
    assert "- task-1 (task) updated=" in summary
    assert "- tgt-1 (target) updated=" in summary
    assert "- hyp-1 (hypothesis) updated=" in summary
    assert "- art-1 (artifact) updated=" in summary
    # ...one-hop neighbors pass recency + confidence...
    assert "- ev-1 (evidence) updated=" in summary
    assert "- hyp-2 (hypothesis) updated=" in summary
    # ...the phase sweep pulls phase-tagged relevant entities...
    assert "- tgt-2 (target) updated=" in summary
    # ...and filtered candidates stay out.
    assert "- ev-2 (evidence)" not in summary  # confidence 0.3 < 0.7 floor
    assert "- ev-old (evidence)" not in summary  # stale (T1, outside 5-minute window)
    assert "- run-1 (run)" not in summary  # not a relevant type, no anchor reference

    # Edges appear only when both endpoints are projected.
    assert "- task-1 --[TASK TARGETS TARGET]--> tgt-1" in summary
    assert "- ev-1 --[EVIDENCE SUPPORTS HYPOTHESIS]--> hyp-1" in summary
    assert "- hyp-2 --[HYPOTHESIS ABOUT SERVICE]--> svc-1" in summary
    assert "EVIDENCE CONTRADICTS HYPOTHESIS" not in summary  # ev-2 was filtered out

    # Exact projection accounting: 5 anchors + ev-1 + hyp-2 + tgt-2 = 8
    # entities; 5 edges with both endpoints selected.
    assert compiled.entities_included == 8
    assert compiled.edges_included == 5
    assert compiled.omitted_entity_count == 0
    assert compiled.omitted_edge_count == 0
    assert compiled.budget_chars == GPT_PROFILE.context_soft_limit
    assert compiled.used_chars <= GPT_PROFILE.context_soft_limit


@pytest.mark.asyncio
async def test_anchors_bypass_recency_and_confidence_filters(tmp_path: Path) -> None:
    """Explicitly referenced anchors are never filtered out."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await _seed(graph)
        compiled = await compile_context(
            graph,
            GPT_PROFILE,
            _request(recency_window=timedelta(minutes=1), confidence_floor=0.9),
            now=NOW,
        )
    assert "- art-1 (artifact) updated=" in compiled.graph_summary
    assert "- task-1 (task) updated=" in compiled.graph_summary
    # Neighbors are NOT exempt: ev-1 (0.95 confidence) passes the 0.9
    # floor but is stale inside a 1-minute window, so it is dropped.
    assert "- ev-1 (evidence)" not in compiled.graph_summary


# ---------------------------------------------------------------------------
# (b) empty graph — graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_graph_degrades_gracefully(tmp_path: Path) -> None:
    """An empty graph compiles without crashing and marks the projection empty."""
    compiled = await _compile(
        tmp_path / "graph.db", request=ContextRequest(mission="M"), seed=False
    )
    assert compiled.mission == "M"
    assert compiled.graph_summary == "PROJECTED ENTITIES\n(none)\nPROJECTED EDGES\n(none)\n"
    assert compiled.transcript_tail == ""
    assert compiled.skills == ()
    assert compiled.output_contract == ""
    assert not compiled.truncated
    assert compiled.entities_included == 0
    assert compiled.edges_included == 0
    assert compiled.used_chars <= GPT_PROFILE.context_soft_limit


# ---------------------------------------------------------------------------
# (c) missing entities referenced in the request — loud error, no KeyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_anchor_raises_context_reference_error(tmp_path: Path) -> None:
    """Any anchor ID missing from the graph fails loudly."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await _seed(graph)
        for field in ("active_task_id", "artifact_ids"):
            overrides = {field: "ghost-1"} if field == "active_task_id" else {field: ("ghost-1",)}
            with pytest.raises(ContextReferenceError, match="ghost-1"):
                await compile_context(graph, GPT_PROFILE, _request(**overrides), now=NOW)
            with pytest.raises(ContextError):
                await compile_context(graph, GPT_PROFILE, _request(**overrides), now=NOW)


# ---------------------------------------------------------------------------
# (d) budget truncation — deterministic marker, budget respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_truncation_produces_marker_and_respects_budget(tmp_path: Path) -> None:
    """A projection larger than the soft limit truncates deterministically."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await graph.create_entity("task-1", "task", {"description": "big sweep"}, at=T1)
        for index in range(40):
            await graph.create_entity(
                f"obs-{index:02d}", "observation", {"note": f"row {index}"}, at=T2
            )
            await graph.create_edge(
                f"e-task-obs-{index:02d}",
                "TASK PRODUCED OBSERVATION",
                "task-1",
                f"obs-{index:02d}",
                at=T2,
            )
        profile = GPT_PROFILE.model_copy(update={"context_soft_limit": 700})
        request = ContextRequest(
            mission="M",
            active_task_id="task-1",
            output_contract="OUT",
        )
        compiled = await compile_context(graph, profile, request, now=NOW)

    assert compiled.truncated
    assert TRUNCATION_MARKER in compiled.graph_summary
    # The anchor (relevance tier 0) survives truncation.
    assert "- task-1 (task) updated=" in compiled.graph_summary
    # Accounting is exact and the budget invariant holds.
    assert compiled.entities_included + compiled.omitted_entity_count == 41
    assert compiled.edges_included + compiled.omitted_edge_count == 40
    assert compiled.omitted_entity_count > 0 or compiled.omitted_edge_count > 0
    assert compiled.used_chars <= 700
    assert compiled.used_chars == len(compiled.mission) + len(compiled.graph_summary) + len(
        compiled.transcript_tail
    ) + sum(len(skill) for skill in compiled.skills) + len(compiled.output_contract)


@pytest.mark.asyncio
async def test_fixed_layers_exceeding_budget_fail_loudly(tmp_path: Path) -> None:
    """An over-long mission cannot fit: ContextBudgetError, never silent drop."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        profile = GPT_PROFILE.model_copy(update={"context_soft_limit": 10})
        with pytest.raises(ContextBudgetError) as excinfo:
            await compile_context(graph, profile, ContextRequest(mission="x" * 50), now=NOW)
    assert excinfo.value.budget == 10
    assert excinfo.value.required == 50


@pytest.mark.asyncio
async def test_transcript_tail_is_truncated_to_its_budget_share(tmp_path: Path) -> None:
    """A long tail keeps its END and carries a truncation marker."""
    tail = "\n".join(f"line {index}" for index in range(300)) + "\n"
    compiled = await _compile(
        tmp_path / "graph.db",
        profile=GPT_PROFILE.model_copy(update={"context_soft_limit": 700}),
        request=ContextRequest(mission="M", transcript_tail=tail),
        seed=False,
    )
    assert compiled.truncated
    assert compiled.transcript_tail.startswith("[TRANSCRIPT TRUNCATED:")
    assert compiled.transcript_tail.rstrip().endswith("line 299")
    # Tail budget share is remaining // 4 with the empty projection.
    assert len(compiled.transcript_tail) == (700 - 1) // 4
    assert compiled.used_chars <= 700


# ---------------------------------------------------------------------------
# (e) determinism — same graph + request -> identical bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_graph_and_request_compile_identically(tmp_path: Path) -> None:
    """Compiling the same graph twice yields byte-identical output."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await _seed(graph)
        request = _request()
        first = await compile_context(graph, GPT_PROFILE, request, now=NOW)
        second = await compile_context(graph, GPT_PROFILE, request, now=NOW)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.graph_summary.encode() == second.graph_summary.encode()
    assert first == second


@pytest.mark.asyncio
async def test_identical_graphs_compile_identically(tmp_path: Path) -> None:
    """Two fresh databases in the same state produce identical bytes."""
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    for path in (first_path, second_path):
        async with StateGraph(path) as graph:
            await _seed(graph)
    request = _request()
    first = await _compile(first_path, request=request, seed=False)
    second = await _compile(second_path, request=request, seed=False)
    assert first.model_dump_json() == second.model_dump_json()


# ---------------------------------------------------------------------------
# (f) skill cap — deterministic truncation by max_advertised_skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_cap_truncates_in_request_order(tmp_path: Path) -> None:
    """More skills than the profile advertises truncates to the first N."""
    skills = ("s1: recon", "s2: web", "s3: exploit", "s4: pivot", "s5: submit")
    async with StateGraph(tmp_path / "graph.db") as graph:
        await _seed(graph)
        capped_profile = GPT_PROFILE.model_copy(update={"max_advertised_skills": 3})
        compiled = await compile_context(graph, capped_profile, _request(skills=skills), now=NOW)
        assert compiled.skills == ("s1: recon", "s2: web", "s3: exploit")

        # A larger cap keeps everything; the cap is per profile, not global.
        compiled_all = await compile_context(graph, GPT_PROFILE, _request(skills=skills), now=NOW)
        assert compiled_all.skills == tuple(skills)


@pytest.mark.asyncio
async def test_zero_skill_cap_advertises_nothing(tmp_path: Path) -> None:
    """The fallback profile (cap 0) never advertises skills."""
    compiled = await _compile(
        tmp_path / "graph.db",
        profile=FALLBACK_PROFILE,
        request=_request(skills=("s1: recon",)),
    )
    assert compiled.skills == ()


# ---------------------------------------------------------------------------
# contradiction state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contradiction_edges_excluded_on_request(tmp_path: Path) -> None:
    """include_contradictions=False drops CONTRADICT edges and endpoints."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        await _seed(graph)
        # No floor/window so only the contradiction state decides.
        request = _request(
            recency_window=None,
            confidence_floor=0.0,
            include_contradictions=False,
        )
        without = await compile_context(graph, GPT_PROFILE, request, now=NOW)
        with_contradictions = await compile_context(
            graph,
            GPT_PROFILE,
            _request(recency_window=None, confidence_floor=0.0),
            now=NOW,
        )

    assert "- ev-2 (evidence)" not in without.graph_summary
    assert "EVIDENCE CONTRADICTS HYPOTHESIS" not in without.graph_summary
    assert "- ev-2 (evidence)" in with_contradictions.graph_summary
    assert "EVIDENCE CONTRADICTS HYPOTHESIS" in with_contradictions.graph_summary
    assert "- contradictions: excluded" in without.graph_summary
