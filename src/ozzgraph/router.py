"""Graph-driven phase router for OzzGraph (PR18).

Implements the PHASE ROUTER layer (docs/ARCHITECTURE.md, "Phase
Router"; PR step 18 of docs/IMPLEMENTATION_PLAN.md): the deterministic
bridge between the authoritative SQLite state graph and the rest of the
harness. :meth:`PhaseRouter.route` reads the graph and returns the next
:class:`~ozzgraph.phases.Phase` — but ONLY through graph-state
predicates, never through action counts or timers (AGENTS.md rule #8).

Design rules:

- Predicates, not counts (AGENTS.md rule #8): every transition in
  :data:`TRANSITIONS` is a pure function of the graph — the presence or
  absence of typed entities, typed edges, and payload fields. The
  router holds no counters, reads no timestamps, and never consults a
  stored ``phase`` payload field (that would make routing
  self-referential). The same graph state always routes to the same
  phase.

- Deterministic ordering: :data:`TRANSITIONS` is evaluated top to
  bottom and the first matching predicate wins. Terminal states (DONE)
  outrank working phases, and the trailing default matches every
  non-empty graph, so :meth:`PhaseRouter.route` always terminates.

- Loud, typed failures (AGENTS.md rule #9): the router validates the
  payload fields it reads (strict booleans) and the invariant-critical
  edges it relies on. A wrong-typed payload field raises
  :class:`InvalidGraphStateError`; an accepted submission without its
  ``SUBMISSION SUBMITS FLAG_CANDIDATE`` edge raises
  :class:`MissingRequiredStateError`. Nothing is swallowed.

- Skill interop (AGENTS.md rule #6): the router resolves the skill
  summaries covering a phase through the
  :class:`~ozzgraph.skills.SkillRegistry` (``list_summaries``) and
  carries them on the returned :class:`PhaseRoute`, so a downstream
  planner selects skills without a second lookup.

- Small kernel (AGENTS.md rule #10): the router owns only the
  transition table and its predicates. Nothing is wired into the
  supervisor here — the executor (PR20) consumes :class:`PhaseRouter`.

Payload conventions (lowercase entity types, uppercase edge types, per
docs/DATA_STRATEGY.md; the full table is in
docs/API_AND_INTEGRATIONS.md, "Phase Router"):

- ``target``: ``confirmed`` (recon complete), ``pivot`` (discovered
  from the foothold), ``reachable`` (reachability confirmed).
- ``service``: ``characterized`` (enumeration complete).
- ``hypothesis``: ``exploitable`` (has an exploitation direction).
- ``credential``: ``valid`` (usable access), ``explored`` (post-
  exploitation already consumed it).
- ``objective``: ``completed`` (the generic DONE predicate — all
  objectives completed, V01 docs/adr/0008).
- ``submission``: ``accepted`` (the run's terminal signal).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.phases import Phase
from ozzgraph.skills import SkillRegistry, SkillSummary
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Entity types the router reads (docs/DATA_STRATEGY.md, lowercase by
#: convention).
ENTITY_TARGET = "target"
ENTITY_SERVICE = "service"
ENTITY_HYPOTHESIS = "hypothesis"
ENTITY_CREDENTIAL = "credential"
ENTITY_OBJECTIVE = "objective"
ENTITY_SUBMISSION = "submission"

#: Edge types the router reads (docs/DATA_STRATEGY.md, uppercase by
#: convention).
EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE = "SUBMISSION SUBMITS FLAG_CANDIDATE"
EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS = "EVIDENCE SUPPORTS HYPOTHESIS"

#: Payload fields the router reads, as strict booleans. A field that is
#: present but not a bool is an invalid graph state (AGENTS.md rule #9).
FIELD_ACCEPTED = "accepted"
FIELD_CHARACTERIZED = "characterized"
FIELD_COMPLETED = "completed"
FIELD_CONFIRMED = "confirmed"
FIELD_EXPLOITABLE = "exploitable"
FIELD_EXPLORED = "explored"
FIELD_PIVOT = "pivot"
FIELD_REACHABLE = "reachable"
FIELD_VALID = "valid"


class PhaseRouterError(RuntimeError):
    """Base error for the phase router layer (AGENTS.md rule #9)."""


class InvalidGraphStateError(PhaseRouterError):
    """A payload field the router reads has an invalid type.

    The router depends on strict boolean payload fields; a present
    non-boolean value (e.g. ``confirmed: "yes"``) is an invalid graph
    state that cannot be routed around and fails loudly instead of
    being coerced.
    """


class MissingRequiredStateError(PhaseRouterError):
    """An invariant-critical relationship required for routing is absent.

    Raised when a transition's decision input violates an AGENTS.md data
    invariant: an accepted submission without its ``SUBMISSION SUBMITS
    FLAG_CANDIDATE`` edge. The check applies to the entities that drive
    the transition (accepted submissions); rejected or unverified
    entities never trigger it.
    """


class PhaseRoute(BaseModel):
    """The deterministic routing decision for one graph state.

    Attributes:
        phase: The next phase to execute.
        predicate: Name of the transition predicate that matched — the
            same names as :data:`TRANSITIONS` and the transition table
            in docs/API_AND_INTEGRATIONS.md.
        skills: Skill summaries covering ``phase``, resolved through the
            router's :class:`~ozzgraph.skills.SkillRegistry` so a
            planner can select skills without a second lookup.
    """

    model_config = ConfigDict(extra="forbid")

    phase: Phase
    predicate: str = Field(min_length=1)
    skills: tuple[SkillSummary, ...] = ()


@dataclass(frozen=True)
class Transition:
    """One graph-driven transition rule, in evaluation order.

    Attributes:
        predicate: The predicate's documented name (appears on
            :attr:`PhaseRoute.predicate` and in the transition table of
            docs/API_AND_INTEGRATIONS.md).
        phase: The phase this transition routes to when ``check`` passes.
        check: The graph-state predicate — pure, deterministic, and
            never dependent on action counts or wall-clock time
            (AGENTS.md rule #8).
    """

    predicate: str
    phase: Phase
    check: Callable[[StateGraph], Awaitable[bool]]


async def _is_empty(graph: StateGraph) -> bool:
    """True when the graph holds no entities at all (the bootstrap state)."""
    return not await graph.list_entities()


async def _has_accepted_submission(graph: StateGraph) -> bool:
    """True when a submission was accepted; the run is done.

    Raises:
        MissingRequiredStateError: If an accepted submission has no
            ``SUBMISSION SUBMITS FLAG_CANDIDATE`` edge (AGENTS.md data
            invariant: every submission references a flag candidate).
    """
    for record in await graph.list_entities(ENTITY_SUBMISSION):
        if not _payload_bool(record, FIELD_ACCEPTED):
            continue
        if not await _has_outgoing_edge(graph, record.id, EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE):
            raise MissingRequiredStateError(
                f"submission {record.id!r} is accepted but has no "
                f"{EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE!r} edge"
            )
        return True
    return False


async def _all_objectives_completed(graph: StateGraph) -> bool:
    """True when every ``objective`` entity is completed (generic DONE).

    The generic terminal predicate (docs/adr/0008): a run is done when
    the environment's objectives are all satisfied — the authoritative
    state is the graph's ``objective`` entities, which the runner seeds
    from the environment adapter and flips to ``completed`` only through
    deterministic evidence paths. A graph with NO objectives is never
    done by this predicate (a run without objectives is not a completed
    run); an empty graph is routed to BOOTSTRAP by the leading
    predicate, so this only ever decides non-empty graphs.
    """
    objectives = await graph.list_entities(ENTITY_OBJECTIVE)
    if not objectives:
        return False
    return all(_payload_bool(record, FIELD_COMPLETED) for record in objectives)


async def _targets_unconfirmed(graph: StateGraph) -> bool:
    """True when the primary target surface is not fully confirmed.

    No targets at all, or a non-pivot target without ``confirmed: true``,
    means recon is incomplete. Pivot-discovered targets are excluded:
    a ``pivot: true`` target is routed by the PIVOT transition instead.
    """
    targets = await graph.list_entities(ENTITY_TARGET)
    if not targets:
        return True
    return any(
        not _payload_bool(record, FIELD_CONFIRMED) and not _payload_bool(record, FIELD_PIVOT)
        for record in targets
    )


async def _has_uncharacterized_services(graph: StateGraph) -> bool:
    """True when a service has not been characterized (enumeration backlog)."""
    for record in await graph.list_entities(ENTITY_SERVICE):
        if not _payload_bool(record, FIELD_CHARACTERIZED):
            return True
    return False


async def _has_supported_exploitable_hypothesis(graph: StateGraph) -> bool:
    """True when an exploitable hypothesis has supporting evidence.

    A hypothesis opens the exploitation phase only when it carries
    ``exploitable: true`` AND has at least one incoming ``EVIDENCE
    SUPPORTS HYPOTHESIS`` edge — a bare claim without support is a soft
    condition that simply does not match, never an error.
    """
    for record in await graph.list_entities(ENTITY_HYPOTHESIS):
        if not _payload_bool(record, FIELD_EXPLOITABLE):
            continue
        if await _has_incoming_edge(graph, record.id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS):
            return True
    return False


async def _has_new_access(graph: StateGraph) -> bool:
    """True when a valid credential has not yet been explored.

    ``explored: true`` means post-exploitation already consumed the
    access; a fresh valid credential is what opens POST_EXPLOITATION.
    """
    for record in await graph.list_entities(ENTITY_CREDENTIAL):
        if _payload_bool(record, FIELD_VALID) and not _payload_bool(record, FIELD_EXPLORED):
            return True
    return False


async def _has_new_reachable_targets(graph: StateGraph) -> bool:
    """True when a pivot-discovered target is reachable.

    The executor marks a target ``pivot: true`` when it is discovered
    from the foothold and ``reachable: true`` once reachability is
    confirmed — only then does the harness pivot to it.
    """
    for record in await graph.list_entities(ENTITY_TARGET):
        if _payload_bool(record, FIELD_PIVOT) and _payload_bool(record, FIELD_REACHABLE):
            return True
    return False


async def _default_replan(graph: StateGraph) -> bool:
    """The fallback transition: every non-empty graph matches REPLAN.

    Reaching this predicate means no earlier transition matched, so the
    harness stops and re-evaluates its strategy instead of guessing.
    """
    return bool(await graph.list_entities())


#: The full transition table, in evaluation order (first match wins).
#: Terminal states (DONE) outrank working phases, and the trailing
#: default matches every non-empty graph, so :meth:`PhaseRouter.route`
#: always terminates. Mirrored in docs/API_AND_INTEGRATIONS.md
#: ("Phase Router"). V01 (docs/adr/0008): FLAG_HUNT /
#: VERIFY_AND_SUBMIT transitions were removed — the generic DONE
#: predicate is ``all_objectives_completed`` (plus the accepted-
#: submission terminal signal kept for the HalCTF submission path).
TRANSITIONS: tuple[Transition, ...] = (
    Transition("graph_is_empty", Phase.BOOTSTRAP, _is_empty),
    Transition("has_accepted_submission", Phase.DONE, _has_accepted_submission),
    Transition("all_objectives_completed", Phase.DONE, _all_objectives_completed),
    Transition("targets_unconfirmed", Phase.RECON, _targets_unconfirmed),
    Transition("has_uncharacterized_services", Phase.ENUMERATION, _has_uncharacterized_services),
    Transition(
        "has_supported_exploitable_hypothesis",
        Phase.EXPLOITATION,
        _has_supported_exploitable_hypothesis,
    ),
    Transition("has_new_access", Phase.POST_EXPLOITATION, _has_new_access),
    Transition("has_new_reachable_targets", Phase.PIVOT, _has_new_reachable_targets),
    Transition("default_replan", Phase.REPLAN, _default_replan),
)


class PhaseRouter:
    """Deterministic, graph-driven phase router (AGENTS.md rule #8).

    Args:
        registry: Skill registry used to resolve per-phase skill
            summaries; defaults to a fresh
            :class:`~ozzgraph.skills.SkillRegistry` snapshot of the
            module-level :data:`ozzgraph.skills.SKILLS`.
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self._registry = registry if registry is not None else SkillRegistry()

    def skills_for(self, phase: Phase) -> tuple[SkillSummary, ...]:
        """Skill summaries covering ``phase`` (registry ``list_summaries``).

        The interop surface for downstream planners (AGENTS.md rule #6):
        the registry advertises compact per-phase summaries, and the
        full skill card is loaded only after the model selects a skill.
        """
        return tuple(self._registry.list_summaries(phase))

    async def route(self, graph: StateGraph) -> PhaseRoute:
        """Route ``graph`` to the next phase.

        Evaluates :data:`TRANSITIONS` top to bottom and returns the
        first match. The leading emptiness predicate matches the empty
        graph (BOOTSTRAP) and the trailing default matches every
        non-empty graph (REPLAN), so routing always terminates.

        Args:
            graph: The authoritative SQLite state graph to route on.

        Raises:
            InvalidGraphStateError: If a payload field the router reads
                is present but not a bool.
            MissingRequiredStateError: If an accepted submission lacks
                its invariant-critical provenance edge.
        """
        for transition in TRANSITIONS:
            if await transition.check(graph):
                return PhaseRoute(
                    phase=transition.phase,
                    predicate=transition.predicate,
                    skills=self.skills_for(transition.phase),
                )
        raise PhaseRouterError("no transition predicate matched")  # pragma: no cover


def _payload_bool(record: EntityRecord, key: str) -> bool:
    """Read a strict-boolean payload field, defaulting to False.

    The router reads exactly the boolean payload fields documented in
    docs/API_AND_INTEGRATIONS.md ("Phase Router"). A field that is
    present but not a bool is an invalid graph state and fails loudly
    (AGENTS.md rule #9) rather than being coerced.

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


async def _has_outgoing_edge(graph: StateGraph, entity_id: str, edge_type: str) -> bool:
    """True when ``entity_id`` is the source of an edge of ``edge_type``."""
    neighbors = await graph.neighbors(entity_id, edge_type)
    return any(edge.src_id == entity_id for edge in neighbors.outgoing)


async def _has_incoming_edge(graph: StateGraph, entity_id: str, edge_type: str) -> bool:
    """True when ``entity_id`` is the destination of an edge of ``edge_type``."""
    neighbors = await graph.neighbors(entity_id, edge_type)
    return any(edge.dst_id == entity_id for edge in neighbors.incoming)
