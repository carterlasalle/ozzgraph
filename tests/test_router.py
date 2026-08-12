"""Tests for the graph-driven phase router (PR18, V01 generic runtime).

Covers every documented transition predicate (each graph state routes
to its phase), the no-transition defaults (empty graph -> BOOTSTRAP,
unmatched non-empty graph -> REPLAN), terminal-state priority (DONE
outranks working phases — via accepted submission or all objectives
completed, V01 docs/adr/0008), determinism (same state twice ->
identical route), the typed error paths (invalid payload types, missing
provenance edges — AGENTS.md rule #9), and SkillRegistry interop for
the routed phase.

Every test uses its own in-memory SQLite graph.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ozzgraph.phases import Phase
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
    TRANSITIONS,
    InvalidGraphStateError,
    MissingRequiredStateError,
    PhaseRoute,
    PhaseRouter,
    PhaseRouterError,
)
from ozzgraph.skills import SkillRegistry
from ozzgraph.state_graph import StateGraph


async def _entity(
    graph: StateGraph,
    entity_id: str,
    entity_type: str,
    data: dict[str, object] | None = None,
) -> None:
    """Create one entity."""
    await graph.create_entity(entity_id, entity_type, data)


async def _edge(
    graph: StateGraph,
    edge_id: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
) -> None:
    """Create one typed edge."""
    await graph.create_edge(edge_id, edge_type, src_id, dst_id)


async def _seed_baseline(graph: StateGraph) -> None:
    """Seed a run with a confirmed target and a characterized service.

    This is the "nothing pending" baseline: recon and enumeration are
    complete, no hypothesis, no access, no flags, no submissions.
    """
    await _entity(graph, "run-1", "run")
    await _entity(graph, "tgt-1", "target", {"confirmed": True})
    await _entity(graph, "svc-1", "service", {"characterized": True})


# ---------------------------------------------------------------------------
# defaults and the transition table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_graph_routes_to_bootstrap() -> None:
    """An empty graph is the no-transition default: BOOTSTRAP."""
    async with StateGraph(":memory:") as graph:
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.BOOTSTRAP
    assert route.predicate == "graph_is_empty"


@pytest.mark.asyncio
async def test_unmatched_non_empty_graph_routes_to_replan() -> None:
    """A non-empty graph matching no predicate falls back to REPLAN."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN
    assert route.predicate == "default_replan"


def test_transitions_cover_every_phase() -> None:
    """The transition table reaches every Phase value (DONE twice: both
    terminal predicates route to it)."""
    phases = [transition.phase for transition in TRANSITIONS]
    assert set(phases) == set(Phase)
    assert phases.count(Phase.DONE) == 2  # submission + objectives terminal


def test_transitions_are_deterministic_table() -> None:
    """First-match order is fixed: DONE predicates first, REPLAN last."""
    predicates = [transition.predicate for transition in TRANSITIONS]
    assert predicates == [
        "graph_is_empty",
        "has_accepted_submission",
        "all_objectives_completed",
        "targets_unconfirmed",
        "has_uncharacterized_services",
        "has_supported_exploitable_hypothesis",
        "has_new_access",
        "has_new_reachable_targets",
        "all_hypotheses_resolved_objectives_open",
        "default_replan",
    ]


def test_transitions_never_reference_removed_phases() -> None:
    """V01 (docs/adr/0008): FLAG_HUNT / VERIFY_AND_SUBMIT are gone."""
    routed = {transition.phase for transition in TRANSITIONS}
    routed_values = {phase.value for phase in routed}
    assert "FLAG_HUNT" not in routed_values
    assert "VERIFY_AND_SUBMIT" not in routed_values
    predicates = [transition.predicate for transition in TRANSITIONS]
    assert "has_verified_flag" not in predicates
    assert "has_access_but_no_flag" not in predicates


# ---------------------------------------------------------------------------
# each transition predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_submission_routes_to_done() -> None:
    """An accepted submission with its flag-candidate edge means DONE."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "flag-1", "flag_candidate")
        await _entity(graph, "sub-1", "submission", {"accepted": True})
        await _edge(
            graph,
            "sub-1->flag-1",
            EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
            "sub-1",
            "flag-1",
        )
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.DONE
    assert route.predicate == "has_accepted_submission"


@pytest.mark.asyncio
async def test_rejected_submission_does_not_route_to_done() -> None:
    """A rejected submission (accepted=False) is not a terminal signal."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "flag-1", "flag_candidate")
        await _entity(graph, "sub-1", "submission", {"accepted": False})
        await _edge(
            graph,
            "sub-1->flag-1",
            EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
            "sub-1",
            "flag-1",
        )
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN


@pytest.mark.asyncio
async def test_all_objectives_completed_routes_to_done() -> None:
    """The generic DONE predicate: every objective completed (V01)."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "objective-1", "objective", {"completed": True})
        await _entity(graph, "objective-2", "objective", {"completed": True})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.DONE
    assert route.predicate == "all_objectives_completed"


@pytest.mark.asyncio
async def test_incomplete_objectives_do_not_route_done() -> None:
    """A single incomplete objective keeps the run working."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "objective-1", "objective", {"completed": True})
        await _entity(graph, "objective-2", "objective", {"completed": False})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN
    assert route.predicate == "default_replan"


@pytest.mark.asyncio
async def test_no_objectives_never_routes_done_by_objectives() -> None:
    """A graph with no objectives is not 'complete' by the objective DONE."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN
    assert route.predicate == "default_replan"


@pytest.mark.asyncio
async def test_flag_candidate_state_no_longer_routes_the_kernel() -> None:
    """V01: flag candidates are HalCTF-owned; the kernel ignores them."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "obs-1", "observation")
        await _entity(graph, "ev-1", "evidence")
        await _entity(graph, "flag-1", "flag_candidate", {"verified": True})
        await _edge(graph, "ev-1->obs-1", "EVIDENCE EXTRACTED_FROM OBSERVATION", "ev-1", "obs-1")
        await _edge(
            graph,
            "flag-1->ev-1",
            "FLAG_CANDIDATE OBSERVED_IN EVIDENCE",
            "flag-1",
            "ev-1",
        )
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN
    assert route.predicate == "default_replan"
    assert route.phase.value not in ("FLAG_HUNT", "VERIFY_AND_SUBMIT")


@pytest.mark.asyncio
async def test_rejected_candidate_without_edge_is_not_an_invariant_error() -> None:
    """A rejected candidate lacking its edge is skipped, not raised (PR22).

    The provenance invariant binds candidates the harness will submit;
    a rejected candidate is excluded from routing entirely, so its
    missing edge is never an error.
    """
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "flag-1", "flag_candidate", {"verified": True, "rejected": True})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN


@pytest.mark.asyncio
async def test_unconfirmed_target_routes_to_recon() -> None:
    """A target without confirmed=True means RECON."""
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "run-1", "run")
        await _entity(graph, "tgt-1", "target", {"confirmed": False})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.RECON
    assert route.predicate == "targets_unconfirmed"


@pytest.mark.asyncio
async def test_no_targets_routes_to_recon() -> None:
    """No targets at all also means RECON (the first working phase)."""
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "run-1", "run")
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.RECON
    assert route.predicate == "targets_unconfirmed"


@pytest.mark.asyncio
async def test_uncharacterized_service_routes_to_enumeration() -> None:
    """A service without characterized=True means ENUMERATION."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "svc-2", "service", {"characterized": False})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.ENUMERATION
    assert route.predicate == "has_uncharacterized_services"


@pytest.mark.asyncio
async def test_supported_exploitable_hypothesis_routes_to_exploitation() -> None:
    """An exploitable hypothesis with supporting evidence means EXPLOITATION."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "ev-1", "evidence")
        await _entity(graph, "hyp-1", "hypothesis", {"exploitable": True})
        await _edge(
            graph,
            "ev-1->hyp-1",
            EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
            "ev-1",
            "hyp-1",
        )
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.EXPLOITATION
    assert route.predicate == "has_supported_exploitable_hypothesis"


@pytest.mark.asyncio
async def test_exploitable_hypothesis_without_support_does_not_match() -> None:
    """A bare exploitable claim without evidence is a soft non-match, not an error."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "hyp-1", "hypothesis", {"exploitable": True})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN


@pytest.mark.asyncio
async def test_new_access_routes_to_post_exploitation() -> None:
    """A valid, unexplored credential means POST_EXPLOITATION."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "cred-1", "credential", {"valid": True})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.POST_EXPLOITATION
    assert route.predicate == "has_new_access"


@pytest.mark.asyncio
async def test_new_reachable_target_routes_to_pivot() -> None:
    """A pivot-marked, reachable target means PIVOT."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(
            graph,
            "tgt-2",
            "target",
            {"confirmed": False, "pivot": True, "reachable": True},
        )
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.PIVOT
    assert route.predicate == "has_new_reachable_targets"


@pytest.mark.asyncio
async def test_explored_access_with_no_objective_routes_replan() -> None:
    """V01: access without a flag no longer routes FLAG_HUNT — it replans."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "cred-1", "credential", {"valid": True, "explored": True})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.REPLAN
    assert route.predicate == "default_replan"


# ---------------------------------------------------------------------------
# terminal-state priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_outranks_all_other_states() -> None:
    """An accepted submission wins even with recon/flag state pending."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "tgt-2", "target", {"confirmed": False})
        await _entity(graph, "flag-1", "flag_candidate", {"verified": True})
        await _entity(graph, "sub-1", "submission", {"accepted": True})
        await _edge(
            graph,
            "sub-1->flag-1",
            EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
            "sub-1",
            "flag-1",
        )
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.DONE


@pytest.mark.asyncio
async def test_objectives_completed_outranks_recon() -> None:
    """All objectives completed wins over an unconfirmed target (V01)."""
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "tgt-1", "target", {"confirmed": False})
        await _entity(graph, "objective-1", "objective", {"completed": True})
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.DONE
    assert route.predicate == "all_objectives_completed"


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_state_routes_identically() -> None:
    """The same graph state twice yields the identical route."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "cred-1", "credential", {"valid": True, "explored": True})
        router = PhaseRouter()
        first = await router.route(graph)
        second = await router.route(graph)
    assert first == second
    assert first.phase == Phase.REPLAN


@pytest.mark.asyncio
async def test_route_ignores_entity_creation_order() -> None:
    """Insertion order does not change the routed phase (deterministic)."""

    async def build(reversed_order: bool) -> Phase:
        async with StateGraph(":memory:") as graph:
            items = [
                ("run-1", "run", None),
                ("tgt-1", "target", {"confirmed": True}),
                ("svc-1", "service", {"characterized": True}),
                ("cred-1", "credential", {"valid": True, "explored": True}),
            ]
            if reversed_order:
                items.reverse()
            for entity_id, entity_type, data in items:
                await _entity(graph, entity_id, entity_type, data)
            route = await PhaseRouter().route(graph)
        return route.phase

    assert await build(False) == await build(True) == Phase.REPLAN


# ---------------------------------------------------------------------------
# typed error paths (AGENTS.md rule #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_boolean_payload_raises() -> None:
    """A non-bool payload field is invalid graph state, not a silent coercion."""
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "tgt-1", "target", {"confirmed": "yes"})
        with pytest.raises(InvalidGraphStateError, match="confirmed"):
            await PhaseRouter().route(graph)


@pytest.mark.asyncio
async def test_accepted_submission_without_edge_raises() -> None:
    """An accepted submission must reference a flag candidate (invariant)."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "sub-1", "submission", {"accepted": True})
        with pytest.raises(MissingRequiredStateError, match="SUBMISSION SUBMITS FLAG_CANDIDATE"):
            await PhaseRouter().route(graph)


@pytest.mark.asyncio
async def test_objective_with_non_bool_completed_raises() -> None:
    """A non-bool objective payload is invalid graph state (V01)."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "objective-1", "objective", {"completed": "yes"})
        with pytest.raises(InvalidGraphStateError, match="completed"):
            await PhaseRouter().route(graph)


def test_error_hierarchy_is_typed() -> None:
    """Both router error classes derive from PhaseRouterError(RuntimeError)."""
    assert issubclass(InvalidGraphStateError, PhaseRouterError)
    assert issubclass(MissingRequiredStateError, PhaseRouterError)
    assert issubclass(PhaseRouterError, RuntimeError)


def test_phase_route_rejects_extra_fields() -> None:
    """PhaseRoute is a strict pydantic contract (extra='forbid')."""
    route = PhaseRoute(phase=Phase.RECON, predicate="targets_unconfirmed")
    assert route.phase == Phase.RECON
    assert route.skills == ()
    with pytest.raises(ValidationError):
        PhaseRoute(phase=Phase.RECON, predicate="x", bogus=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# SkillRegistry interop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_carries_skills_for_routed_phase() -> None:
    """route() resolves registry summaries covering the routed phase."""
    registry = SkillRegistry()
    router = PhaseRouter(registry)
    async with StateGraph(":memory:") as graph:
        await _entity(graph, "run-1", "run")
        await _entity(graph, "tgt-1", "target", {"confirmed": False})
        route = await router.route(graph)
    assert route.phase == Phase.RECON
    assert route.skills == tuple(registry.list_summaries(Phase.RECON))
    assert route.skills
    assert all(summary.skill_id.startswith("recon_") for summary in route.skills)


@pytest.mark.asyncio
async def test_skills_for_queries_registry_directly() -> None:
    """skills_for(phase) is the explicit registry interop surface."""
    registry = SkillRegistry()
    router = PhaseRouter(registry)
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "hyp-1", "hypothesis", {"exploitable": True})
        await _entity(graph, "ev-1", "evidence")
        await _edge(
            graph,
            "ev-1->hyp-1",
            EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
            "ev-1",
            "hyp-1",
        )
        route = await router.route(graph)
    assert route.phase == Phase.EXPLOITATION
    assert router.skills_for(Phase.EXPLOITATION) == tuple(
        registry.list_summaries(Phase.EXPLOITATION)
    )
    assert route.skills == router.skills_for(Phase.EXPLOITATION)


def test_skills_for_phase_without_packs_is_empty() -> None:
    """Phases with no skill packs (e.g. BOOTSTRAP) yield an empty tuple."""
    router = PhaseRouter()
    assert router.skills_for(Phase.BOOTSTRAP) == ()
