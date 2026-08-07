"""Tests for the deterministic planner and planning schemas (PR19).

Covers the branching predicate (multiple evidenced hypotheses or
multiple uncharacterized services produce a plan), the typed no-plan
decision (non-branching graphs), deterministic ranking (confidence,
then evidence weight, then id tiebreak), the plan step cap, skill
selection through the SkillRegistry summaries carried by the route,
service-characterization steps, and the typed failure paths (wrong-typed
payloads, hypotheses without evidence refs, phases without skill packs
— AGENTS.md rule #9).

Every test uses its own in-memory SQLite graph (":memory:").
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ozzgraph.phases import Phase
from ozzgraph.planner import (
    EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS,
    MAX_PLAN_STEPS,
    MIN_STRATEGIC_PATHS,
    PLAN_ABANDONMENT_CONDITIONS,
    PLAN_COMPLETION_CONDITIONS,
    AbandonCondition,
    Hypothesis,
    InvalidGraphStateError,
    MissingRequiredStateError,
    NoPlanDecision,
    Plan,
    Planner,
    PlannerError,
    PlannerSkillUnavailableError,
    PlanStep,
)
from ozzgraph.router import EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, PhaseRouter
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


async def _seed_hypothesis(
    graph: StateGraph,
    hypothesis_id: str,
    supporting: tuple[str, ...] = (),
    contradicting: tuple[str, ...] = (),
    *,
    data: dict[str, object] | None = None,
) -> None:
    """Seed one hypothesis with evidence entities and edges.

    Evidence entities are created as ``ev-<id>`` and linked with
    ``EVIDENCE SUPPORTS HYPOTHESIS`` / ``EVIDENCE CONTRADICTS
    HYPOTHESIS`` edges, mirroring the DATA_STRATEGY.md relationships.
    """
    await _entity(graph, hypothesis_id, "hypothesis", data)
    for evidence_id in supporting:
        await _entity(graph, f"ev-{evidence_id}", "evidence")
        await _edge(
            graph,
            f"ev-{evidence_id}->{hypothesis_id}",
            EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
            f"ev-{evidence_id}",
            hypothesis_id,
        )
    for evidence_id in contradicting:
        await _entity(graph, f"ev-{evidence_id}", "evidence")
        await _edge(
            graph,
            f"ev-{evidence_id}->{hypothesis_id}",
            EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS,
            f"ev-{evidence_id}",
            hypothesis_id,
        )


# ---------------------------------------------------------------------------
# branching state produces a ranked plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branching_hypotheses_produce_ranked_plan() -> None:
    """Two evidenced exploitable hypotheses yield a ranked, bounded plan."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8}
        )
        await _seed_hypothesis(
            graph,
            "hyp-b",
            ("b1", "b2"),
            data={"exploitable": True, "confidence": 0.9},
        )
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
    assert route.phase == Phase.EXPLOITATION
    assert isinstance(plan, Plan)
    assert [h.id for h in plan.hypotheses] == ["hyp-b", "hyp-a"]  # confidence desc
    assert [h.rank for h in plan.hypotheses] == [1, 2]
    assert plan.steps
    assert len(plan.steps) <= MAX_PLAN_STEPS
    assert [step.hypothesis_id for step in plan.steps] == ["hyp-b", "hyp-a"]
    assert plan.skills == route.skills
    assert plan.completion_conditions == PLAN_COMPLETION_CONDITIONS
    assert plan.abandonment_conditions == PLAN_ABANDONMENT_CONDITIONS


def test_branching_constants_are_documented() -> None:
    """The branching floor and step cap are explicit module constants."""
    assert MIN_STRATEGIC_PATHS == 2
    assert MAX_PLAN_STEPS == 5


# ---------------------------------------------------------------------------
# non-branching state produces no plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_branching_graph_returns_no_plan_decision() -> None:
    """A single evidenced hypothesis is one path, not a branch: no plan."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(graph, "hyp-1", ("a1",), data={"exploitable": True})
        route = await PhaseRouter().route(graph)
        decision = await Planner().plan(graph, route)
    assert route.phase == Phase.EXPLOITATION
    assert isinstance(decision, NoPlanDecision)
    assert decision.phase == Phase.EXPLOITATION
    assert "no branching" in decision.reason


@pytest.mark.asyncio
async def test_single_uncharacterized_service_is_not_branching() -> None:
    """One uncharacterized service is a linear pipeline, not a branch."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "svc-2", "service", {"characterized": False})
        route = await PhaseRouter().route(graph)
        decision = await Planner().plan(graph, route)
    assert route.phase == Phase.ENUMERATION
    assert isinstance(decision, NoPlanDecision)
    assert "no branching" in decision.reason


@pytest.mark.asyncio
async def test_baseline_only_graph_returns_no_plan_decision() -> None:
    """A graph with no strategic paths at all produces no plan."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        route = await PhaseRouter().route(graph)
        decision = await Planner().plan(graph, route)
    assert isinstance(decision, NoPlanDecision)
    assert decision.phase == Phase.REPLAN


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_graph_yields_identical_plan() -> None:
    """The same graph state twice yields the identical plan."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.8}
        )
        await _seed_hypothesis(
            graph,
            "hyp-b",
            ("b1",),
            data={"exploitable": True, "confidence": 0.6, "exploitation_direction": "SQLi"},
        )
        route = await PhaseRouter().route(graph)
        planner = Planner()
        first = await planner.plan(graph, route)
        second = await planner.plan(graph, route)
    assert isinstance(first, Plan)
    assert isinstance(second, Plan)
    assert first == second
    assert first.id == second.id
    assert first.id.startswith("plan-exploitation-")


# ---------------------------------------------------------------------------
# bounded plan steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_step_cap_is_respected() -> None:
    """Ranking covers every hypothesis; steps are capped at MAX_PLAN_STEPS."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        for index in range(MAX_PLAN_STEPS + 3):
            await _seed_hypothesis(
                graph,
                f"hyp-{index:02d}",
                (f"e{index}",),
                data={"exploitable": True, "confidence": 0.7},
            )
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
    assert isinstance(plan, Plan)
    assert len(plan.hypotheses) == MAX_PLAN_STEPS + 3
    assert len(plan.steps) == MAX_PLAN_STEPS
    assert [step.hypothesis_id for step in plan.steps] == [
        h.id for h in plan.hypotheses[:MAX_PLAN_STEPS]
    ]


# ---------------------------------------------------------------------------
# skill selection through the registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steps_select_skills_via_registry() -> None:
    """Step skills come from the route's registry summaries, round-robin."""
    registry = SkillRegistry()
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 0.6}
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.6}
        )
        route = await PhaseRouter(registry).route(graph)
        plan = await Planner().plan(graph, route)
    assert route.phase == Phase.EXPLOITATION
    assert isinstance(plan, Plan)
    summaries = registry.list_summaries(Phase.EXPLOITATION)
    assert plan.skills == tuple(summaries)
    assert {step.skill_id for step in plan.steps} <= {s.skill_id for s in summaries}
    expected = [summaries[index % len(summaries)].skill_id for index in range(len(plan.steps))]
    assert [step.skill_id for step in plan.steps] == expected


# ---------------------------------------------------------------------------
# deterministic ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ranking_uses_evidence_weight_then_id_tiebreak() -> None:
    """Confidence desc, then evidence weight desc, then id asc."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1", "a2"), data={"exploitable": True, "confidence": 0.5}
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.5}
        )
        await _seed_hypothesis(
            graph, "hyp-c", ("c1",), data={"exploitable": True, "confidence": 0.4}
        )
        await _seed_hypothesis(
            graph, "hyp-d", ("d1",), data={"exploitable": True, "confidence": 0.4}
        )
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
    assert isinstance(plan, Plan)
    assert [h.id for h in plan.hypotheses] == ["hyp-a", "hyp-b", "hyp-c", "hyp-d"]


@pytest.mark.asyncio
async def test_contradicting_evidence_lowers_rank_and_is_carried() -> None:
    """Evidence weight is net supporting count; contradictions are carried."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        # hyp-x: 2 supports + 2 contradicts -> net weight 0
        await _seed_hypothesis(
            graph,
            "hyp-x",
            ("x1", "x2"),
            ("xc1", "xc2"),
            data={"exploitable": True, "confidence": 0.6},
        )
        # hyp-y: 1 support -> net weight 1
        await _seed_hypothesis(
            graph, "hyp-y", ("y1",), data={"exploitable": True, "confidence": 0.6}
        )
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
    assert isinstance(plan, Plan)
    assert [h.id for h in plan.hypotheses] == ["hyp-y", "hyp-x"]
    assert plan.hypotheses[1].supporting_evidence == ("ev-x1", "ev-x2")
    assert plan.hypotheses[1].contradicting_evidence == ("ev-xc1", "ev-xc2")


@pytest.mark.asyncio
async def test_hypothesis_carries_exploitation_direction_and_objective() -> None:
    """Payload direction/objective flow into the schema and step objectives."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph,
            "hyp-a",
            ("a1",),
            data={
                "exploitable": True,
                "confidence": 0.9,
                "exploitation_direction": "SQL injection on /login",
                "objective": "probe the login parameter",
            },
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.8}
        )
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
    assert isinstance(plan, Plan)
    hyp_a = plan.hypotheses[0]
    assert hyp_a.exploitation_direction == "SQL injection on /login"
    assert hyp_a.objective == "probe the login parameter"
    assert plan.steps[0].objective == "SQL injection on /login"
    assert plan.steps[1].objective == "gather evidence for hypothesis hyp-b"


# ---------------------------------------------------------------------------
# service branching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncharacterized_services_branching_produces_service_steps() -> None:
    """Two uncharacterized services are a branch: service steps, no hypotheses."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "svc-2", "service", {"characterized": False})
        await _entity(graph, "svc-3", "service", {"characterized": False})
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
    assert route.phase == Phase.ENUMERATION
    assert isinstance(plan, Plan)
    assert plan.hypotheses == ()
    assert [step.hypothesis_id for step in plan.steps] == [None, None]
    assert all("characterize service" in step.objective for step in plan.steps)
    assert all("is characterized" in step.completion_condition for step in plan.steps)
    assert all(step.skill_id for step in plan.steps)


# ---------------------------------------------------------------------------
# typed failure paths (AGENTS.md rule #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_without_evidence_refs_raises() -> None:
    """A bare hypothesis is invalid graph state once a plan is being built."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _entity(graph, "hyp-bare", "hypothesis", {"exploitable": True})
        await _entity(graph, "svc-2", "service", {"characterized": False})
        await _entity(graph, "svc-3", "service", {"characterized": False})
        route = await PhaseRouter().route(graph)
        with pytest.raises(MissingRequiredStateError, match="hyp-bare"):
            await Planner().plan(graph, route)


@pytest.mark.asyncio
async def test_invalid_confidence_payload_raises() -> None:
    """A non-numeric confidence is invalid graph state, not a coercion."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph,
            "hyp-a",
            ("a1",),
            data={"exploitable": True, "confidence": "high"},
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.7}
        )
        route = await PhaseRouter().route(graph)
        with pytest.raises(InvalidGraphStateError, match="confidence"):
            await Planner().plan(graph, route)


@pytest.mark.asyncio
async def test_out_of_range_confidence_raises() -> None:
    """A confidence outside [0.0, 1.0] cannot be ranked and fails loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph, "hyp-a", ("a1",), data={"exploitable": True, "confidence": 5.0}
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.7}
        )
        route = await PhaseRouter().route(graph)
        with pytest.raises(InvalidGraphStateError, match="confidence"):
            await Planner().plan(graph, route)


@pytest.mark.asyncio
async def test_invalid_exploitation_direction_payload_raises() -> None:
    """A non-string exploitation direction is invalid graph state."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(
            graph,
            "hyp-a",
            ("a1",),
            data={"exploitable": True, "confidence": 0.8, "exploitation_direction": 42},
        )
        await _seed_hypothesis(
            graph, "hyp-b", ("b1",), data={"exploitable": True, "confidence": 0.7}
        )
        route = await PhaseRouter().route(graph)
        with pytest.raises(InvalidGraphStateError, match="exploitation_direction"):
            await Planner().plan(graph, route)


@pytest.mark.asyncio
async def test_planning_without_phase_skills_raises() -> None:
    """A branching graph routed to a phase with no skill packs fails loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_baseline(graph)
        await _seed_hypothesis(graph, "hyp-a", ("a1",), data={"confidence": 0.8})
        await _seed_hypothesis(graph, "hyp-b", ("b1",), data={"confidence": 0.7})
        route = await PhaseRouter().route(graph)
        assert route.phase == Phase.REPLAN
        with pytest.raises(PlannerSkillUnavailableError, match="REPLAN"):
            await Planner().plan(graph, route)


def test_error_hierarchy_is_typed() -> None:
    """All planner error classes derive from PlannerError(RuntimeError)."""
    assert issubclass(InvalidGraphStateError, PlannerError)
    assert issubclass(MissingRequiredStateError, PlannerError)
    assert issubclass(PlannerSkillUnavailableError, PlannerError)
    assert issubclass(PlannerError, RuntimeError)


def test_schemas_reject_extra_fields() -> None:
    """The planning schemas are strict pydantic contracts (extra='forbid')."""
    with pytest.raises(ValidationError):
        Hypothesis(
            id="hyp-1",
            phase=Phase.EXPLOITATION,
            objective="x",
            rank=1,
            confidence=0.5,
            bogus=1,
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PlanStep(
            id="s1",
            objective="x",
            skill_id="y",
            completion_condition="c",
            abandon_condition=AbandonCondition(condition="a"),
            bogus=1,
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Plan(id="p1", phase=Phase.EXPLOITATION, bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        NoPlanDecision(phase=Phase.RECON, reason="r", bogus=1)  # type: ignore[call-arg]


def test_abandon_conditions_are_typed_schema_instances() -> None:
    """Abandon conditions are AbandonCondition schemas, not bare strings.

    Step- and plan-level conditions both carry the structured schema
    (predicate text, scope, rationale), and a plain string is rejected
    by construction (pydantic ValidationError) instead of being
    coerced.
    """
    step = PlanStep(
        id="p1-step-1",
        hypothesis_id="hyp-1",
        objective="x",
        skill_id="y",
        completion_condition="c",
        abandon_condition=AbandonCondition(
            condition="hypothesis hyp-1 gains new contradicting evidence",
            scope="hyp-1",
        ),
    )
    assert isinstance(step.abandon_condition, AbandonCondition)
    assert step.abandon_condition.scope == "hyp-1"
    assert step.abandon_condition.condition.startswith("hypothesis hyp-1")
    phase_scoped = AbandonCondition(condition="x", scope=Phase.EXPLOITATION)
    assert phase_scoped.scope == Phase.EXPLOITATION
    plan = Plan(
        id="p1",
        phase=Phase.EXPLOITATION,
        steps=(step,),
        abandonment_conditions=PLAN_ABANDONMENT_CONDITIONS,
    )
    assert plan.abandonment_conditions
    assert all(isinstance(condition, AbandonCondition) for condition in plan.abandonment_conditions)
    with pytest.raises(ValidationError):
        PlanStep(
            id="p1-step-2",
            objective="x",
            skill_id="y",
            completion_condition="c",
            abandon_condition="a plain string",  # type: ignore[arg-type]
        )
