"""Deterministic planner and planning schemas for OzzGraph (PR19).

Implements the PLANNER layer (docs/ARCHITECTURE.md, "Planner"; PR step
19 of docs/IMPLEMENTATION_PLAN.md, the first slice of Phase 7
Planner-Executor-Evaluator): the deterministic bridge between the
graph-driven :class:`~ozzgraph.router.PhaseRouter` and the executor
(PR20). :meth:`Planner.plan` reads the authoritative SQLite state graph
plus the routed :class:`~ozzgraph.router.PhaseRoute`, and returns a
bounded, ranked :class:`Plan` — but ONLY when the graph is in a
branching state with multiple strategic paths; otherwise it returns a
typed :class:`NoPlanDecision` instead of fabricating a plan.

Design rules:

- Predicates, not counts (AGENTS.md rule #8): the branching decision is
  a pure function of the graph — at least :data:`MIN_STRATEGIC_PATHS`
  evidenced hypotheses (a ``hypothesis`` entity with an incoming
  ``EVIDENCE SUPPORTS HYPOTHESIS`` or ``EVIDENCE CONTRADICTS
  HYPOTHESIS`` edge) or at least :data:`MIN_STRATEGIC_PATHS`
  uncharacterized ``service`` entities. The planner holds no counters,
  reads no timestamps, and never counts actions. The same graph state
  always yields the same plan.

- Deterministic ranking: hypotheses are ranked by ``confidence``
  (descending), then net evidence weight (supporting minus
  contradicting evidence counts, descending), then entity id
  (ascending) as the final tiebreak. No randomness, no model calls.

- Bounded plan: :data:`MAX_PLAN_STEPS` caps the ordered step list; the
  ranked hypotheses list is unbounded by design (the evaluator, PR21,
  needs the full ranking to decide revise/abandon). Every step carries a
  completion condition and an abandon condition so the evaluator can
  decide deterministically.

- Skill interop (AGENTS.md rule #6): steps select skills from the
  route's :attr:`~ozzgraph.router.PhaseRoute.skills` — the registry
  summaries the router already resolved for the routed phase (PR18) —
  assigned round-robin in the registry's deterministic sorted order.
  :meth:`Planner.skills_for` is the explicit registry interop surface
  for callers that need a fresh lookup.

- Loud, typed failures (AGENTS.md rule #9): the planner validates the
  payload fields it reads. A wrong-typed or out-of-range payload field
  (e.g. ``confidence: "high"``, ``confidence: 5.0``) raises
  :class:`InvalidGraphStateError`; a ``hypothesis`` entity with no
  evidence refs — no incoming support or contradict edge — raises
  :class:`MissingRequiredStateError` once a plan is actually being
  built (a lone bare hypothesis in a non-branching graph simply yields
  :class:`NoPlanDecision`, mirroring the router's soft handling); a
  branching graph routed to a phase with no skill packs raises
  :class:`PlannerSkillUnavailableError`. Nothing is swallowed.

- Small kernel (AGENTS.md rule #10): the planner only derives plans;
  it never writes to the graph (the executor, PR20, persists plans as
  entities) and nothing is wired into the supervisor here.

Payload conventions (lowercase entity types, uppercase edge types, per
docs/DATA_STRATEGY.md; the full table is in
docs/API_AND_INTEGRATIONS.md, "Planner"):

- ``hypothesis``: ``confidence`` (float in [0.0, 1.0], missing defaults
  to 0.0), ``objective`` (bounded statement of the claim, optional),
  ``exploitation_direction`` (bounded exploitation direction, optional).
- ``service``: ``characterized`` (strict bool, same field the router
  reads).

Entity and edge types the planner reads reuse the router's canonical
constants where they exist (``hypothesis``, ``service``,
``EVIDENCE SUPPORTS HYPOTHESIS``); the planner defines its own for the
relationships the router does not read (``EVIDENCE CONTRADICTS
HYPOTHESIS``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.phases import Phase
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    ENTITY_HYPOTHESIS,
    ENTITY_SERVICE,
    FIELD_CHARACTERIZED,
    PhaseRoute,
)
from ozzgraph.skills import SkillRegistry, SkillSummary
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Edge type the planner reads for negative evidence
#: (docs/DATA_STRATEGY.md, uppercase by convention).
EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS = "EVIDENCE CONTRADICTS HYPOTHESIS"

#: Payload fields the planner reads (docs/API_AND_INTEGRATIONS.md,
#: "Planner").
FIELD_CONFIDENCE = "confidence"
FIELD_EXPLOITATION_DIRECTION = "exploitation_direction"
FIELD_OBJECTIVE = "objective"

#: Branching is meaningful only with at least this many concurrent
#: strategic paths (evidenced hypotheses or uncharacterized services).
#: One path is a linear pipeline; two or more require a plan (AGENTS.md
#: rule #8: a graph-state predicate, never an action count).
MIN_STRATEGIC_PATHS = 2

#: A plan carries at most this many ordered steps. The ranked
#: hypotheses list is unbounded — the evaluator (PR21) needs the full
#: ranking — but execution is bounded to the top-ranked paths.
MAX_PLAN_STEPS = 5

#: Deterministic plan-level completion conditions, evaluated by the
#: evaluator (PR21). The plan is complete when every step reached its
#: completion condition, or when a ranked hypothesis was confirmed.
PLAN_COMPLETION_CONDITIONS: tuple[str, ...] = (
    "every plan step reached its completion condition",
    "a ranked hypothesis gained new supporting evidence with no contradictions",
)

#: Deterministic plan-level abandonment conditions, evaluated by the
#: evaluator (PR21). The plan is abandoned when every ranked hypothesis
#: is contradicted, or when the graph no longer routes to the plan's
#: phase.
PLAN_ABANDONMENT_CONDITIONS: tuple[str, ...] = (
    "every ranked hypothesis gained contradicting evidence",
    "the graph no longer routes to the plan's phase",
)


class PlannerError(RuntimeError):
    """Base error for the planner layer (AGENTS.md rule #9)."""


class InvalidGraphStateError(PlannerError):
    """A payload field the planner reads has an invalid type or value.

    The planner depends on strict payload fields: ``confidence`` must
    be a number in [0.0, 1.0], and ``objective`` /
    ``exploitation_direction`` must be non-empty strings. A present
    field that violates its contract (e.g. ``confidence: "high"``,
    ``confidence: 5.0``) is an invalid graph state that cannot be
    ranked and fails loudly instead of being coerced.
    """


class MissingRequiredStateError(PlannerError):
    """A hypothesis entity lacks the evidence refs the planner must rank.

    Raised while a plan is being built when a ``hypothesis`` entity has
    no incoming ``EVIDENCE SUPPORTS HYPOTHESIS`` or ``EVIDENCE
    CONTRADICTS HYPOTHESIS`` edge: without evidence refs the hypothesis
    cannot be ranked, and silently dropping it would hide graph state
    the plan is supposed to account for. A bare hypothesis in a
    NON-branching graph never triggers this — the planner returns
    :class:`NoPlanDecision` instead, mirroring the router's soft
    handling of unsupported claims.
    """


class PlannerSkillUnavailableError(PlannerError):
    """The routed phase has no skill packs, so no step can get a skill.

    Raised when the graph is branching but the route's phase has no
    registered skill summaries (e.g. ``REPLAN``). A plan whose steps
    carry no skill would be silently unexecutable, so the planner fails
    loudly instead.
    """


class Hypothesis(BaseModel):
    """One ranked hypothesis in a plan.

    Attributes:
        id: The ``hypothesis`` entity id in the graph.
        phase: Scope: the routed phase the plan serves.
        objective: Scope: bounded statement of what the hypothesis
            claims (payload ``objective``, or derived from the id).
        rank: 1-based priority position; 1 is the highest-ranked path.
        confidence: Payload ``confidence`` in [0.0, 1.0]; missing
            payloads default to 0.0 (weak).
        supporting_evidence: Evidence entity ids linked by ``EVIDENCE
            SUPPORTS HYPOTHESIS``, ordered by edge id.
        contradicting_evidence: Evidence entity ids linked by
            ``EVIDENCE CONTRADICTS HYPOTHESIS``, ordered by edge id.
        exploitation_direction: Payload ``exploitation_direction``, a
            bounded exploitation direction, when the hypothesis has one.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    phase: Phase
    objective: str = Field(min_length=1)
    rank: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    exploitation_direction: str | None = None


class PlanStep(BaseModel):
    """One bounded, ordered step of a plan.

    Attributes:
        id: Plan-scoped step id (``<plan id>-step-<n>``).
        hypothesis_id: The hypothesis this step tests, or ``None`` for
            pre-hypothesis steps (characterizing an uncharacterized
            service so a hypothesis can be formed).
        objective: Bounded action objective: the hypothesis's
            exploitation direction when set, else a derived evidence-
            gathering objective; for service steps,
            ``characterize service <id>``.
        skill_id: The skill selected for this step, round-robin over
            the route's phase skills in registry order.
        completion_condition: Deterministic condition under which the
            step is complete (evaluated by the evaluator, PR21).
        abandon_condition: Deterministic condition under which the step
            is abandoned (evaluated by the evaluator, PR21).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    hypothesis_id: str | None = None
    objective: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)
    abandon_condition: str = Field(min_length=1)


class Plan(BaseModel):
    """A bounded, ranked plan for one routed phase.

    Attributes:
        id: Run-scoped id, deterministic: ``plan-<phase>-<graph hash
            prefix>``, so the same graph state always yields the same
            plan id.
        phase: The routed phase the plan serves.
        hypotheses: Ranked hypotheses (confidence, then evidence
            weight, then id); unbounded so the evaluator can decide
            revise/abandon over the full ranking.
        steps: Ordered, bounded steps (at most :data:`MAX_PLAN_STEPS`);
            one per ranked hypothesis (rank order), then one per
            uncharacterized service, truncated to the cap.
        completion_conditions: Plan-level completion conditions
            (:data:`PLAN_COMPLETION_CONDITIONS`).
        abandonment_conditions: Plan-level abandonment conditions
            (:data:`PLAN_ABANDONMENT_CONDITIONS`).
        skills: The route's phase skills (registry summaries) the steps
            select from.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    phase: Phase
    hypotheses: tuple[Hypothesis, ...] = ()
    steps: tuple[PlanStep, ...] = ()
    completion_conditions: tuple[str, ...] = ()
    abandonment_conditions: tuple[str, ...] = ()
    skills: tuple[SkillSummary, ...] = ()


class NoPlanDecision(BaseModel):
    """The typed decision that the graph is not in a branching state.

    Returned by :meth:`Planner.plan` instead of fabricating a plan when
    the graph holds fewer than :data:`MIN_STRATEGIC_PATHS` strategic
    paths of either kind (evidenced hypotheses, uncharacterized
    services).
    """

    model_config = ConfigDict(extra="forbid")

    phase: Phase
    reason: str = Field(min_length=1)


class Planner:
    """Deterministic planner over a graph-driven phase route.

    Args:
        registry: Skill registry used by :meth:`skills_for`; defaults
            to a fresh :class:`~ozzgraph.skills.SkillRegistry` snapshot
            of the module-level :data:`ozzgraph.skills.SKILLS`. Plan
            step skills are selected from ``route.skills`` — the
            summaries the router already resolved for the routed phase
            (PR18) — so no second lookup happens during planning.
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self._registry = registry if registry is not None else SkillRegistry()

    def skills_for(self, phase: Phase) -> tuple[SkillSummary, ...]:
        """Skill summaries covering ``phase`` (registry ``list_summaries``).

        The interop surface for callers that need a fresh registry
        lookup (AGENTS.md rule #6); the router already performed this
        lookup when building the route, so :meth:`plan` consumes
        ``route.skills`` instead.
        """
        return tuple(self._registry.list_summaries(phase))

    async def plan(self, graph: StateGraph, route: PhaseRoute) -> Plan | NoPlanDecision:
        """Plan for ``graph`` under the routed phase, or no plan.

        First evaluates the branching predicate: at least
        :data:`MIN_STRATEGIC_PATHS` evidenced hypotheses (a hypothesis
        with an incoming ``EVIDENCE SUPPORTS HYPOTHESIS`` or
        ``EVIDENCE CONTRADICTS HYPOTHESIS`` edge) or at least
        :data:`MIN_STRATEGIC_PATHS` uncharacterized services. A graph
        that is not branching yields a :class:`NoPlanDecision`; a plan
        is never fabricated.

        On a branching graph the planner ranks every hypothesis
        (confidence descending, then net evidence weight descending —
        supporting minus contradicting evidence counts — then entity id
        ascending), builds one bounded step per ranked hypothesis plus
        one per uncharacterized service, truncates the step list to
        :data:`MAX_PLAN_STEPS`, and assigns skills round-robin from the
        route's phase skills. The plan id derives from the graph hash,
        so the same graph state always yields the same plan.

        Args:
            graph: The authoritative SQLite state graph to plan on.
            route: The graph-driven phase route to plan under.

        Raises:
            InvalidGraphStateError: If a payload field the planner
                reads is present but wrong-typed or out of range (e.g.
                ``confidence: "high"``, ``confidence: 5.0``,
                ``exploitation_direction: 42``).
            MissingRequiredStateError: If a hypothesis entity has no
                evidence refs while a plan is being built.
            PlannerSkillUnavailableError: If the routed phase has no
                skill packs, so no step could receive a skill.
        """
        evidenced, uncharacterized = await _strategic_path_counts(graph)
        if evidenced < MIN_STRATEGIC_PATHS and uncharacterized < MIN_STRATEGIC_PATHS:
            return NoPlanDecision(
                phase=route.phase,
                reason=(
                    f"no branching: {evidenced} evidenced hypotheses and "
                    f"{uncharacterized} uncharacterized services (branching needs "
                    f">= {MIN_STRATEGIC_PATHS} of either)"
                ),
            )
        ranked = await _rank_hypotheses(graph, route.phase)
        plan_id = await _plan_id(graph, route)
        steps = await _plan_steps(graph, route, plan_id, ranked)
        return Plan(
            id=plan_id,
            phase=route.phase,
            hypotheses=tuple(ranked),
            steps=tuple(steps),
            completion_conditions=PLAN_COMPLETION_CONDITIONS,
            abandonment_conditions=PLAN_ABANDONMENT_CONDITIONS,
            skills=route.skills,
        )


async def _strategic_path_counts(graph: StateGraph) -> tuple[int, int]:
    """Count strategic paths: evidenced hypotheses, uncharacterized services.

    An evidenced hypothesis is a ``hypothesis`` entity with at least
    one incoming ``EVIDENCE SUPPORTS HYPOTHESIS`` or ``EVIDENCE
    CONTRADICTS HYPOTHESIS`` edge. An uncharacterized service is a
    ``service`` entity without ``characterized: true`` (strict bool, per
    the router's payload convention).
    """
    evidenced = 0
    for record in await graph.list_entities(ENTITY_HYPOTHESIS):
        supports = await _incoming_evidence_ids(graph, record.id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS)
        contradicts = await _incoming_evidence_ids(
            graph, record.id, EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
        )
        if supports or contradicts:
            evidenced += 1
    uncharacterized = 0
    for record in await graph.list_entities(ENTITY_SERVICE):
        if not _payload_bool(record, FIELD_CHARACTERIZED):
            uncharacterized += 1
    return evidenced, uncharacterized


async def _rank_hypotheses(graph: StateGraph, phase: Phase) -> list[Hypothesis]:
    """Rank every hypothesis entity: confidence, evidence weight, id.

    Sorting key is ``(-confidence, contradicting - supporting,
    entity_id)``: confidence descending, then net evidence weight
    (supporting minus contradicting edge counts) descending, then
    entity id ascending — deterministic, no randomness.

    Raises:
        MissingRequiredStateError: If a hypothesis entity has no
            evidence refs (no incoming support or contradict edge).
        InvalidGraphStateError: If a payload field the planner reads is
            wrong-typed or out of range.
    """
    records = await graph.list_entities(ENTITY_HYPOTHESIS)
    candidates: list[tuple[float, int, str, EntityRecord, tuple[str, ...], tuple[str, ...]]] = []
    for record in records:
        supporting = await _incoming_evidence_ids(
            graph, record.id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS
        )
        contradicting = await _incoming_evidence_ids(
            graph, record.id, EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
        )
        if not supporting and not contradicting:
            raise MissingRequiredStateError(
                f"hypothesis {record.id!r} has no evidence refs: expected an "
                f"incoming {EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS!r} or "
                f"{EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS!r} edge"
            )
        confidence = _payload_confidence(record)
        candidates.append(
            (
                -confidence,
                len(contradicting) - len(supporting),  # negated net weight
                record.id,
                record,
                supporting,
                contradicting,
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    ranked: list[Hypothesis] = []
    for index, candidate in enumerate(candidates, start=1):
        neg_confidence, _neg_weight, _entity_id, record, supporting, contradicting = candidate
        ranked.append(
            Hypothesis(
                id=record.id,
                phase=phase,
                objective=_payload_optional_str(record, FIELD_OBJECTIVE)
                or f"resolve hypothesis {record.id}",
                rank=index,
                confidence=-neg_confidence,
                supporting_evidence=supporting,
                contradicting_evidence=contradicting,
                exploitation_direction=_payload_optional_str(record, FIELD_EXPLOITATION_DIRECTION),
            )
        )
    return ranked


async def _plan_steps(
    graph: StateGraph,
    route: PhaseRoute,
    plan_id: str,
    hypotheses: list[Hypothesis],
) -> list[PlanStep]:
    """Build the bounded, ordered step list for a plan.

    One step per ranked hypothesis (rank order, objective from the
    exploitation direction when set), then one step per uncharacterized
    service (``hypothesis_id=None``), truncated to
    :data:`MAX_PLAN_STEPS`. Skills are assigned round-robin over the
    route's phase skills in the registry's deterministic sorted order,
    so identical graphs yield identical assignments.

    Raises:
        PlannerSkillUnavailableError: If the routed phase has no skill
            packs.
        InvalidGraphStateError: If a ``service`` payload field the
            planner reads is wrong-typed.
    """
    skills = route.skills
    if not skills:
        raise PlannerSkillUnavailableError(
            f"no skills cover phase {route.phase.value}; cannot select a skill for plan steps"
        )
    raw: list[tuple[str, str | None, str, str]] = []
    for hypothesis in hypotheses:
        raw.append(
            (
                hypothesis.exploitation_direction
                or f"gather evidence for hypothesis {hypothesis.id}",
                hypothesis.id,
                f"hypothesis {hypothesis.id} gains new supporting evidence",
                f"hypothesis {hypothesis.id} gains new contradicting evidence",
            )
        )
    for record in await graph.list_entities(ENTITY_SERVICE):
        if _payload_bool(record, FIELD_CHARACTERIZED):
            continue
        raw.append(
            (
                f"characterize service {record.id}",
                None,
                f"service {record.id} is characterized",
                f"service {record.id} is unreachable or absent",
            )
        )
    return [
        PlanStep(
            id=f"{plan_id}-step-{index}",
            hypothesis_id=hypothesis_id,
            objective=objective,
            skill_id=skills[(index - 1) % len(skills)].skill_id,
            completion_condition=completion,
            abandon_condition=abandon,
        )
        for index, (objective, hypothesis_id, completion, abandon) in enumerate(
            raw[:MAX_PLAN_STEPS], start=1
        )
    ]


async def _plan_id(graph: StateGraph, route: PhaseRoute) -> str:
    """Deterministic run-scoped plan id: ``plan-<phase>-<hash prefix>``.

    Derived from the graph's canonical content hash plus the routed
    phase, so the same graph state always yields the same plan id. The
    graph hash is computed on demand and never stored.
    """
    digest = await graph.graph_hash()
    return f"plan-{route.phase.value.lower()}-{digest[:12]}"


async def _incoming_evidence_ids(
    graph: StateGraph, entity_id: str, edge_type: str
) -> tuple[str, ...]:
    """Evidence entity ids with an incoming ``edge_type`` edge to ``entity_id``.

    Ordered by edge id (``StateGraph.neighbors`` orders deterministically),
    so the tuple is stable for a given graph state.
    """
    neighbors = await graph.neighbors(entity_id, edge_type)
    return tuple(edge.src_id for edge in neighbors.incoming)


def _payload_bool(record: EntityRecord, key: str) -> bool:
    """Read a strict-boolean payload field, defaulting to False.

    The planner reads exactly the boolean payload fields documented in
    docs/API_AND_INTEGRATIONS.md ("Planner"). A field that is present
    but not a bool is an invalid graph state and fails loudly (AGENTS.md
    rule #9) rather than being coerced.

    Raises:
        InvalidGraphStateError: If ``key`` is present on the record's
            payload and is not a bool.
    """
    value = record.data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {key!r} must be a bool, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def _payload_confidence(record: EntityRecord) -> float:
    """Read the hypothesis ``confidence`` payload field, defaulting to 0.0.

    ``bool`` is excluded from the numeric check (it is an ``int``
    subclass in Python). A present non-number, or a number outside
    [0.0, 1.0], is an invalid graph state that cannot be ranked and
    fails loudly rather than being clamped or coerced.

    Raises:
        InvalidGraphStateError: If ``confidence`` is present and not a
            number in [0.0, 1.0].
    """
    value = record.data.get(FIELD_CONFIDENCE)
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {FIELD_CONFIDENCE!r} must be a "
            f"number in [0.0, 1.0], got {type(value).__name__} ({value!r})"
        )
    if not 0.0 <= value <= 1.0:
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {FIELD_CONFIDENCE!r} must be in "
            f"[0.0, 1.0], got {value!r}"
        )
    return float(value)


def _payload_optional_str(record: EntityRecord, key: str) -> str | None:
    """Read an optional non-empty string payload field.

    Raises:
        InvalidGraphStateError: If ``key`` is present and is not a
            non-empty string.
    """
    value = record.data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidGraphStateError(
            f"entity {record.id!r} payload field {key!r} must be a non-empty "
            f"string, got {type(value).__name__} ({value!r})"
        )
    return value
