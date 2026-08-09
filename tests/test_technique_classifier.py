"""Tests for the HAL-009 TechniqueClassifier and category-routed skills.

Covers the deterministic challenge-category -> skill-id mapping
(case-insensitive substring rules, union semantics, sorted output),
the unknown/absent-category default (recon/enum core), lazy-load
integrity of category routing (summaries advertised, cards only on
load), the loud failure for classifier mappings that reference
unregistered skills, PhaseRouter category-constrained advertising, and
registration/validity of every new Tottori lesson card.
"""

from __future__ import annotations

import pytest

from ozzgraph.phases import Phase
from ozzgraph.router import PhaseRouter
from ozzgraph.skills import (
    SKILLS,
    Skill,
    SkillRegistry,
    SkillRegistryError,
    SkillSummary,
)
from ozzgraph.techniques import (
    CATEGORY_RULES,
    DEFAULT_CATEGORY_SKILL_IDS,
    TechniqueClassifier,
)
from ozzgraph.toolplane import ToolCatalog

#: Every skill id the classifier can route (rules + default core).
ROUTED_SKILL_IDS = tuple(
    sorted(
        {skill_id for rule in CATEGORY_RULES for skill_id in rule.skill_ids}
        | set(DEFAULT_CATEGORY_SKILL_IDS)
    )
)

#: The eight HAL-009 Tottori lesson cards.
NEW_SKILL_IDS = (
    "exploit_sqli_enumeration",
    "exploit_jwt",
    "exploit_ssrf",
    "exploit_xxe",
    "exploit_deserialization",
    "exploit_protocol_reversing",
    "forensics_file_analysis",
    "exploit_cloud_iam",
)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_classifier_routes_web_ssrf_category() -> None:
    """\"Web / SSRF\" advertises the SSRF card and excludes forensics."""
    ids = TechniqueClassifier().skill_ids_for("Web / SSRF")
    assert "exploit_ssrf" in ids
    assert "enum_http_application" in ids
    assert "forensics_file_analysis" not in ids
    assert "exploit_sqli_enumeration" not in ids


def test_classifier_routes_sql_injection_category() -> None:
    """\"SQL Injection\" selects the SQLi enumeration + parameter cards."""
    ids = TechniqueClassifier().skill_ids_for("SQL Injection")
    assert ids == ("exploit_parameter_injection", "exploit_sqli_enumeration")


def test_classifier_routes_web_sqli_category() -> None:
    """\"Web / SQLi\" unions the web and sql rule contributions."""
    ids = TechniqueClassifier().skill_ids_for("Web / SQLi")
    assert "exploit_sqli_enumeration" in ids
    assert "exploit_parameter_injection" in ids
    assert "exploit_jwt" in ids  # the web rule rides along
    assert "exploit_ssrf" not in ids


def test_classifier_routes_jwt_and_crypto_categories() -> None:
    """JWT and crypto categories select the JWT + auth-bypass cards."""
    for category in ("Web / JWT", "JWT", "Crypto"):
        ids = TechniqueClassifier().skill_ids_for(category)
        assert "exploit_jwt" in ids
        assert "exploit_auth_bypass" in ids


def test_classifier_routes_xxe_category() -> None:
    """\"XXE\" selects the XXE card plus HTTP application analysis."""
    ids = TechniqueClassifier().skill_ids_for("XXE")
    assert "exploit_xxe" in ids
    assert "enum_http_application" in ids


def test_classifier_routes_deserialization_category() -> None:
    """Deserialization labels select the deserialization card."""
    for category in ("Insecure Deserialization", "Deser", "Deserialization"):
        assert TechniqueClassifier().skill_ids_for(category) == ("exploit_deserialization",)


def test_classifier_routes_reverse_engineering_and_network() -> None:
    """Reverse-engineering and network categories select protocol reversing."""
    assert TechniqueClassifier().skill_ids_for("Reverse Engineering") == (
        "exploit_protocol_reversing",
    )
    assert TechniqueClassifier().skill_ids_for("Network") == (
        "exploit_protocol_reversing",
        "recon_port_probe",
    )


def test_classifier_routes_forensics_category() -> None:
    """\"Forensics\" selects the forensics card and nothing web-specific."""
    ids = TechniqueClassifier().skill_ids_for("Forensics")
    assert ids == ("forensics_file_analysis",)
    assert "exploit_ssrf" not in ids


def test_classifier_routes_cloud_iam_category() -> None:
    """\"Cloud IAM\" selects the cloud IAM card (cloud and iam rules)."""
    ids = TechniqueClassifier().skill_ids_for("Cloud IAM")
    assert ids == ("exploit_cloud_iam",)


def test_classifier_unknown_category_uses_default() -> None:
    """Unknown or absent categories degrade to the recon/enum core."""
    classifier = TechniqueClassifier()
    for category in (None, "", "Miscellaneous", "OSINT", "Pwn"):
        ids = classifier.skill_ids_for(category)
        assert ids == DEFAULT_CATEGORY_SKILL_IDS
        assert ids  # never an empty advertisement


def test_classifier_is_case_insensitive() -> None:
    """Matching is case-insensitive substring matching."""
    assert TechniqueClassifier().skill_ids_for("web / ssrf") == TechniqueClassifier().skill_ids_for(
        "Web / SSRF"
    )
    assert "exploit_sqli_enumeration" in TechniqueClassifier().skill_ids_for("sqli")


def test_classifier_is_deterministic() -> None:
    """Same category -> same ordered ids, across calls and instances."""
    first = TechniqueClassifier()
    second = TechniqueClassifier()
    samples = ("Web / SSRF", "SQL Injection", "Forensics", "Cloud IAM", "xxe", None, "zzz-unknown")
    for category in samples:
        ids = first.skill_ids_for(category)
        assert ids == second.skill_ids_for(category)
        assert ids == first.skill_ids_for(category)  # repeated calls identical
        assert ids == tuple(sorted(ids))  # output is sorted


# ---------------------------------------------------------------------------
# SkillRegistry.list_for_category: lazy advertisements + loud failures
# ---------------------------------------------------------------------------


def test_list_for_category_returns_lazy_summaries() -> None:
    """Category routing advertises summaries; full cards arrive only on load."""
    registry = SkillRegistry()
    summaries = registry.list_for_category("Web / SSRF")
    ids = [summary.skill_id for summary in summaries]
    assert "exploit_ssrf" in ids
    assert "forensics_file_analysis" not in ids
    assert ids == sorted(ids)
    for summary in summaries:
        assert isinstance(summary, SkillSummary)
        assert set(summary.model_dump()) == {"skill_id", "name", "phases", "description"}
        assert not hasattr(summary, "card")
    loaded = registry.load("exploit_ssrf")
    assert loaded.card
    assert "Do NOT" in loaded.card


def test_list_for_category_unknown_and_none_use_default() -> None:
    """The registry exposes the same default core for unknown/absent labels."""
    registry = SkillRegistry()
    for category in (None, "", "Miscellaneous", "OSINT"):
        ids = [summary.skill_id for summary in registry.list_for_category(category)]
        assert ids == list(DEFAULT_CATEGORY_SKILL_IDS)
        assert ids


def test_list_for_category_is_deterministic() -> None:
    """Two registry instances route the same category identically."""
    for category in ("Web / SSRF", "SQL Injection", "Forensics", "Cloud IAM", None):
        assert SkillRegistry().list_for_category(category) == SkillRegistry().list_for_category(
            category
        )


def test_list_for_category_unregistered_mapping_fails_loudly() -> None:
    """A classifier mapping referencing an unregistered skill is a loud error."""
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
    with pytest.raises(SkillRegistryError, match="unregistered skill ids"):
        registry.list_for_category("SQL Injection")


def test_classifier_summaries_matches_registry_listing() -> None:
    """TechniqueClassifier.summaries and list_for_category agree exactly."""
    registry = SkillRegistry()
    classifier = TechniqueClassifier()
    for category in ("Web / SSRF", "SQL Injection", "Forensics", "Cloud IAM", None):
        assert classifier.summaries(registry, category) == registry.list_for_category(category)


def test_every_classifier_reference_is_registered() -> None:
    """Every id the classifier can route is a registered skill (no drift)."""
    for skill_id in ROUTED_SKILL_IDS:
        assert skill_id in SKILLS, f"classifier routes unregistered skill {skill_id!r}"


# ---------------------------------------------------------------------------
# PhaseRouter wiring (production reachability)
# ---------------------------------------------------------------------------


def test_router_skills_for_category_constrains_advertisement() -> None:
    """The router advertises the category's subset for the routed phase."""
    router = PhaseRouter()
    ssrf_ids = [summary.skill_id for summary in router.skills_for(Phase.EXPLOITATION, "Web / SSRF")]
    assert "exploit_ssrf" in ssrf_ids
    assert "forensics_file_analysis" not in ssrf_ids
    forensics_ids = [
        summary.skill_id for summary in router.skills_for(Phase.ENUMERATION, "Forensics")
    ]
    assert "forensics_file_analysis" in forensics_ids
    assert "exploit_ssrf" not in forensics_ids


def test_router_skills_for_intersects_phase_and_category() -> None:
    """Category constraint intersects the phase set, never replaces it."""
    router = PhaseRouter()
    enum_ids = [summary.skill_id for summary in router.skills_for(Phase.ENUMERATION, "Web / SSRF")]
    # enum_http_application is in both the phase and the category...
    assert "enum_http_application" in enum_ids
    # ...but a phase card outside the category is not advertised.
    assert "forensics_file_analysis" not in enum_ids
    # The EXPLOITATION subset for the category is exactly the union of
    # the web and ssrf rule contributions, sorted by skill_id.
    assert [
        summary.skill_id for summary in router.skills_for(Phase.EXPLOITATION, "Web / SSRF")
    ] == [
        "exploit_auth_bypass",
        "exploit_jwt",
        "exploit_ssrf",
    ]


def test_router_skills_for_without_category_is_unchanged() -> None:
    """No category keeps the full per-phase advertisement (pre-HAL-009)."""
    router = PhaseRouter()
    registry = SkillRegistry()
    for phase in Phase:
        assert router.skills_for(phase) == tuple(registry.list_summaries(phase))


# ---------------------------------------------------------------------------
# the eight new Tottori lesson cards
# ---------------------------------------------------------------------------


def test_new_skills_register_and_are_enumerable_per_phase() -> None:
    """Every new card is registered and advertised for each of its phases."""
    registry = SkillRegistry()
    for skill_id in NEW_SKILL_IDS:
        assert skill_id in SKILLS
        skill = registry.load(skill_id)
        assert skill.skill_id == skill_id
        for phase in skill.phases:
            assert skill_id in {summary.skill_id for summary in registry.list_summaries(phase)}


def test_new_skills_are_valid_bounded_cards() -> None:
    """New cards obey the bounded card shape and the tool-plane contract."""
    vocabulary = ToolCatalog().capabilities()
    registry = SkillRegistry()
    for skill_id in NEW_SKILL_IDS:
        skill = registry.load(skill_id)
        assert len(skill.skill_id) <= 64
        assert 1 <= len(skill.description) <= 200
        assert skill.name
        assert "Purpose:" in skill.card
        assert "Do NOT" in skill.card
        assert skill.timeout_seconds >= 1
        assert skill.parsers == (("shell", "text"),)
        unknown = set(skill.required_capabilities) - vocabulary
        assert not unknown, f"skill {skill_id!r} requires unknown capabilities: {unknown}"
