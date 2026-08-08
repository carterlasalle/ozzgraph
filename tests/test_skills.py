"""Tests for the skill registry and initial packs (PR17).

Covers the Phase enum (canonical order, policy-gate value agreement),
lazy registry semantics (compact summaries advertised per phase, full
cards on load), deterministic ordering, parser-mapping resolution to
live Parser instances, per-skill timeouts, typed errors for unknown
skill ids and broken parser mappings, and the failure paths
(AGENTS.md testing expectations for kernel changes).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ozzgraph.observations import (
    PARSERS,
    SHELL_TEXT_PARSER,
    Parser,
)
from ozzgraph.phases import Phase
from ozzgraph.policy import PHASES as POLICY_PHASES
from ozzgraph.skills import (
    SKILLS,
    Skill,
    SkillRegistry,
    SkillRegistryError,
    SkillSummary,
    register_skill,
)

# ---------------------------------------------------------------------------
# Phase enum
# ---------------------------------------------------------------------------


def test_phase_enum_order_matches_architecture() -> None:
    """Member order is exactly the canonical ARCHITECTURE.md phase order.

    V01 (docs/adr/0008): FLAG_HUNT / VERIFY_AND_SUBMIT are removed from
    the generic kernel.
    """
    assert list(Phase) == [
        Phase.BOOTSTRAP,
        Phase.RECON,
        Phase.ENUMERATION,
        Phase.EXPLOITATION,
        Phase.POST_EXPLOITATION,
        Phase.PIVOT,
        Phase.REPLAN,
        Phase.DONE,
    ]
    assert {phase.value for phase in Phase} == {
        "BOOTSTRAP",
        "RECON",
        "ENUMERATION",
        "EXPLOITATION",
        "POST_EXPLOITATION",
        "PIVOT",
        "REPLAN",
        "DONE",
    }
    assert "FLAG_HUNT" not in {phase.value for phase in Phase}
    assert "VERIFY_AND_SUBMIT" not in {phase.value for phase in Phase}


def test_phase_values_match_policy_gate() -> None:
    """Phase values are exactly the phase names the policy gate knows."""
    assert {phase.value for phase in Phase} == set(POLICY_PHASES)


def test_phase_is_a_str_enum() -> None:
    """A Phase member compares equal to its plain-string value."""
    assert Phase.RECON == "RECON"
    assert Phase.RECON.value == "RECON"


# ---------------------------------------------------------------------------
# list_summaries
# ---------------------------------------------------------------------------


def test_list_summaries_filters_by_phase() -> None:
    """Summaries cover exactly the requested phase, bidirectionally."""
    registry = SkillRegistry()
    for phase in Phase:
        summaries = registry.list_summaries(phase)
        ids = [summary.skill_id for summary in summaries]
        assert ids == sorted(ids)
        for summary in summaries:
            assert phase in summary.phases
        registered = sorted(skill_id for skill_id, skill in SKILLS.items() if phase in skill.phases)
        assert ids == registered


def test_initial_packs_cover_the_required_phases() -> None:
    """RECON, ENUMERATION, and EXPLOITATION each have skills (V01:

    the FLAG_HUNT packs left with the removed phases).
    """
    registry = SkillRegistry()
    for phase in (Phase.RECON, Phase.ENUMERATION, Phase.EXPLOITATION):
        assert registry.list_summaries(phase)
    # The removed phases advertise no skills.
    assert "FLAG_HUNT" not in {p.value for p in Phase}
    assert "VERIFY_AND_SUBMIT" not in {p.value for p in Phase}


def test_list_summaries_is_deterministic() -> None:
    """Repeated listing yields identical, id-sorted summaries."""
    registry = SkillRegistry()
    first = registry.list_summaries(Phase.RECON)
    assert [s.skill_id for s in first] == [
        "recon_dns_enum",
        "recon_http_fingerprint",
        "recon_port_probe",
    ]
    assert first == registry.list_summaries(Phase.RECON)
    assert first == SkillRegistry().list_summaries(Phase.RECON)


def test_list_summaries_are_compact_advertisements() -> None:
    """Summaries carry only the advertised fields — no card, no timeout."""
    registry = SkillRegistry()
    for phase in Phase:
        for summary in registry.list_summaries(phase):
            assert isinstance(summary, SkillSummary)
            assert set(summary.model_dump()) == {"skill_id", "name", "phases", "description"}
            assert "card" not in SkillSummary.model_fields
            assert "timeout_seconds" not in SkillSummary.model_fields
            assert 1 <= len(summary.description) <= 200


# ---------------------------------------------------------------------------
# load / validity of every registered skill
# ---------------------------------------------------------------------------


def test_every_registered_skill_loads_and_is_valid() -> None:
    """Every registered skill loads with valid, non-empty fields."""
    registry = SkillRegistry()
    assert SKILLS  # the initial packs are registered at import
    for skill_id, expected in SKILLS.items():
        skill = registry.load(skill_id)
        assert skill is expected  # load returns the registered object
        assert skill.skill_id == skill_id
        assert skill.name
        assert skill.description
        assert skill.card
        assert skill.timeout_seconds > 0
        assert skill.phases
        assert all(isinstance(phase, Phase) for phase in skill.phases)


def test_skill_summary_matches_skill_fields() -> None:
    """The summary is derived from the skill, never free-form."""
    skill = SKILLS["recon_dns_enum"]
    summary = skill.summary()
    assert summary.skill_id == skill.skill_id
    assert summary.name == skill.name
    assert summary.phases == skill.phases
    assert summary.description == skill.description


def test_load_unknown_skill_raises_typed_error() -> None:
    """Unknown skill ids raise SkillRegistryError, not a bare KeyError."""
    with pytest.raises(SkillRegistryError, match="no skill registered"):
        SkillRegistry().load("no-such-skill")


# ---------------------------------------------------------------------------
# parsers_for
# ---------------------------------------------------------------------------


def test_parsers_for_resolves_real_parser_instances() -> None:
    """Parser mappings resolve to the registered Parser instances."""
    registry = SkillRegistry()
    for skill in SKILLS.values():
        parsers = registry.parsers_for(skill.skill_id)
        assert len(parsers) == len(skill.parsers)
        for parser, (source, kind) in zip(parsers, skill.parsers, strict=True):
            assert isinstance(parser, Parser)
            assert parser.source == source
            assert parser.kind == kind
            assert PARSERS[(source, kind)] is parser
    assert registry.parsers_for("recon_dns_enum") == [SHELL_TEXT_PARSER]
    with pytest.raises(SkillRegistryError, match="flag_hunt_submit"):
        registry.parsers_for("flag_hunt_submit")  # removed with FLAG_HUNT (V01)


def test_parsers_for_skill_without_mappings_returns_empty() -> None:
    """A skill with no parser mappings resolves to an empty list."""
    skill = Skill(
        skill_id="noparse",
        name="NoParse",
        phases=(Phase.RECON,),
        description="no parsers",
        card="card",
        timeout_seconds=10,
        parsers=(),
    )
    assert SkillRegistry({"noparse": skill}).parsers_for("noparse") == []


def test_parsers_for_unknown_skill_raises() -> None:
    """An unknown skill id fails loudly with the typed registry error."""
    with pytest.raises(SkillRegistryError, match="no-such-skill"):
        SkillRegistry().parsers_for("no-such-skill")


def test_parsers_for_unregistered_parser_mapping_fails_loudly() -> None:
    """A skill mapping an unregistered parser key is a broken registry entry."""
    broken = Skill(
        skill_id="broken",
        name="Broken",
        phases=(Phase.RECON,),
        description="maps an unknown parser",
        card="card",
        timeout_seconds=30,
        parsers=(("shell", "nonexistent"),),
    )
    registry = SkillRegistry({"broken": broken})
    with pytest.raises(SkillRegistryError, match="unregistered parser"):
        registry.parsers_for("broken")


# ---------------------------------------------------------------------------
# timeout_for
# ---------------------------------------------------------------------------


def test_timeout_for_returns_per_skill_values() -> None:
    """timeout_for returns each skill's own default timeout in seconds."""
    registry = SkillRegistry()
    for skill_id, skill in SKILLS.items():
        assert registry.timeout_for(skill_id) == skill.timeout_seconds
    with pytest.raises(SkillRegistryError, match="flag_hunt_submit"):
        registry.timeout_for("flag_hunt_submit")  # removed with FLAG_HUNT (V01)


def test_timeout_for_unknown_skill_raises() -> None:
    """An unknown skill id fails loudly for timeout lookups too."""
    with pytest.raises(SkillRegistryError, match="no-such-skill"):
        SkillRegistry().timeout_for("no-such-skill")


# ---------------------------------------------------------------------------
# lazy semantics
# ---------------------------------------------------------------------------


def test_lazy_loading_summaries_first_cards_on_demand() -> None:
    """Advertising is cheap (summaries); full cards arrive only on load."""
    registry = SkillRegistry()
    summaries = registry.list_summaries(Phase.RECON)
    assert summaries  # advertised up front
    assert all(not hasattr(summary, "card") for summary in summaries)
    loaded = registry.load("recon_dns_enum")
    assert isinstance(loaded, Skill)
    assert loaded.card  # the full card is fetched on selection
    assert "Do NOT" in loaded.card


def test_custom_registry_snapshots_without_mutating_module_state() -> None:
    """Registry instances copy their mapping; the module constant is untouched."""
    custom = Skill(
        skill_id="custom",
        name="Custom",
        phases=(Phase.RECON,),
        description="a custom skill for tests",
        card="card",
        timeout_seconds=45,
        parsers=(),
    )
    registry = SkillRegistry({"custom": custom})
    assert [s.skill_id for s in registry.list_summaries(Phase.RECON)] == ["custom"]
    assert "custom" not in SKILLS
    with pytest.raises(SkillRegistryError):
        SkillRegistry().load("custom")


# ---------------------------------------------------------------------------
# registration and model failure paths
# ---------------------------------------------------------------------------


def test_register_skill_duplicate_fails_loudly() -> None:
    """Duplicate registration is a loud typed error, not a silent overwrite."""
    with pytest.raises(SkillRegistryError, match="already registered"):
        register_skill(SKILLS["recon_dns_enum"])


def test_skill_model_rejects_invalid_fields() -> None:
    """Invalid skill fields fail loudly at construction (extra='forbid')."""
    with pytest.raises(ValidationError):
        Skill(
            skill_id="x",
            name="",
            phases=(Phase.RECON,),
            description="d",
            card="c",
            timeout_seconds=30,
        )
    with pytest.raises(ValidationError):
        Skill(
            skill_id="x",
            name="X",
            phases=(),
            description="d",
            card="c",
            timeout_seconds=30,
        )
    with pytest.raises(ValidationError):
        Skill(
            skill_id="x",
            name="X",
            phases=(Phase.RECON,),
            description="d",
            card="c",
            timeout_seconds=0,
        )
    with pytest.raises(ValidationError):
        Skill(
            skill_id="x",
            name="X",
            phases=(Phase.RECON,),
            description="",
            card="c",
            timeout_seconds=30,
        )
    with pytest.raises(ValidationError):
        Skill(
            skill_id="x",
            name="X",
            phases=(Phase.RECON,),
            description="d",
            card="c",
            timeout_seconds=30,
            bogus=1,  # type: ignore[call-arg]
        )


def test_skill_phases_are_normalized_to_canonical_order() -> None:
    """Duplicates are dropped and phases ordered by the Phase enum."""
    skill = Skill(
        skill_id="multi",
        name="Multi",
        phases=(Phase.PIVOT, Phase.RECON, Phase.PIVOT),
        description="covers multiple phases",
        card="card",
        timeout_seconds=30,
    )
    assert skill.phases == (Phase.RECON, Phase.PIVOT)
    assert skill.summary().phases == (Phase.RECON, Phase.PIVOT)
