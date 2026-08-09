"""Challenge-category skill routing (HAL-009, Tottori lesson packs).

:class:`TechniqueClassifier` maps a challenge category string —
platform metadata such as ``HAL_CHALLENGE_CATEGORY`` (e.g. ``"Web /
SSRF"``, ``"SQL Injection"``, ``"Forensics"``, ``"Cloud IAM"``) — to
the deterministic subset of registered skill ids most relevant to that
category. It exists so the harness can advertise category-constrained
skills up front while keeping lazy loading intact (AGENTS.md rule #6):
routing deals only in skill ids and compact
:class:`~ozzgraph.skills.SkillSummary` advertisements; the full skill
card still loads exclusively via
:meth:`~ozzgraph.skills.SkillRegistry.load`.

Design rules (AGENTS.md):

- Deterministic: matching is case-insensitive substring matching
  against the ordered rule table :data:`CATEGORY_RULES`. Every rule
  whose keyword occurs in the category contributes its ids; the union
  is deduplicated and sorted by ``skill_id``, so the same category
  string always yields the same ids and no set-iteration order leaks
  into output. Category routing consumes only summaries, never cards.
- Kernel stays small (AGENTS.md rule #10): this module is data plus a
  pure function over strings. It imports nothing from the skill layer
  at runtime (the registry dependency is type-only, so there is no
  import cycle), and no category logic lives in the supervisor.
- Unknown categories degrade deterministically (AGENTS.md rule #9):
  a category that matches no rule — or is absent/``None`` — resolves
  to the recon/enum core (:data:`DEFAULT_CATEGORY_SKILL_IDS`), never
  an empty advertisement and never a crash. Challenge metadata is
  optional at runtime (``HAL_CHALLENGE_CATEGORY`` may be unset), so an
  unfamiliar label must not brick a run. Loud failure is reserved for
  broken REGISTRY mappings — a classifier rule referencing a skill id
  no skill registers fails loudly in
  :meth:`~ozzgraph.skills.SkillRegistry.list_for_category` and
  :meth:`TechniqueClassifier.summaries`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ozzgraph.skills import SkillRegistry, SkillSummary


class CategoryRule(BaseModel):
    """One category-routing rule: a keyword and the skill ids it selects.

    Matching is a case-insensitive substring test of ``keyword``
    against the lowercased category string, so ``"sql"`` selects the
    SQLi cards for ``"SQL Injection"`` and ``"Web / SQLi"`` alike.
    Rules are evaluated in :data:`CATEGORY_RULES` order and every
    matching rule contributes its ids (union semantics).
    """

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=64)
    skill_ids: tuple[str, ...] = Field(min_length=1)


#: Ordered category-routing rules (HAL-009). Matching is union-based:
#: ``"Web / SSRF"`` matches both the ``web`` rule (JWT / auth-bypass
#: cards) and the ``ssrf`` rule (the SSRF card plus HTTP application
#: analysis). Keyword order is documentation order only — the output
#: is always the sorted, deduplicated union.
CATEGORY_RULES: tuple[CategoryRule, ...] = (
    # Web labels carry the authentication-attack cards: JWT and the
    # generic auth-bypass checks ride along with any web category.
    CategoryRule(keyword="web", skill_ids=("exploit_jwt", "exploit_auth_bypass")),
    # SQL injection: the multi-DB enumeration card plus the generic
    # parameter-injection probe that evidences the hypothesis.
    CategoryRule(
        keyword="sql", skill_ids=("exploit_sqli_enumeration", "exploit_parameter_injection")
    ),
    # JWT / signature-and-key crypto attacks (alg confusion,
    # PEM-as-HMAC-secret, kid injection).
    CategoryRule(keyword="jwt", skill_ids=("exploit_jwt", "exploit_auth_bypass")),
    CategoryRule(keyword="crypto", skill_ids=("exploit_jwt", "exploit_auth_bypass")),
    # Server-side request forgery: multi-service probing from one URL
    # parameter plus the HTTP application analysis that maps the sink.
    CategoryRule(keyword="ssrf", skill_ids=("exploit_ssrf", "enum_http_application")),
    # XML external entities.
    CategoryRule(keyword="xxe", skill_ids=("exploit_xxe", "enum_http_application")),
    # Deserialization sinks (both spellings of the common labels).
    CategoryRule(keyword="deserialization", skill_ids=("exploit_deserialization",)),
    CategoryRule(keyword="deser", skill_ids=("exploit_deserialization",)),
    # Protocol reversing: custom network protocols, reverse
    # engineering, and network-protocol CTF categories.
    CategoryRule(keyword="protocol", skill_ids=("exploit_protocol_reversing",)),
    CategoryRule(keyword="reverse", skill_ids=("exploit_protocol_reversing",)),
    CategoryRule(keyword="network", skill_ids=("exploit_protocol_reversing", "recon_port_probe")),
    # Forensics / artifact analysis.
    CategoryRule(keyword="forensic", skill_ids=("forensics_file_analysis",)),
    # Cloud IAM: metadata service and role chaining.
    CategoryRule(keyword="cloud", skill_ids=("exploit_cloud_iam",)),
    CategoryRule(keyword="iam", skill_ids=("exploit_cloud_iam",)),
)

#: The deterministic default for unknown or absent categories: the
#: recon/enum core every run can use regardless of challenge flavor.
#: Never empty — an unlabeled run still gets a useful baseline
#: advertisement (AGENTS.md rule #9: graceful, documented degradation).
#: Ordered by ``skill_id`` so every classifier output path is sorted.
DEFAULT_CATEGORY_SKILL_IDS: tuple[str, ...] = (
    "enum_http_application",
    "enum_service_version",
    "enum_web_content",
    "recon_dns_enum",
    "recon_http_fingerprint",
    "recon_port_probe",
)


class TechniqueClassifier:
    """Deterministic challenge category -> skill-id subset router.

    Pure data plus a pure function over strings: given a category, it
    returns the sorted, deduplicated union of every matching rule's
    skill ids (or the default core for unknown/absent categories).
    Instances snapshot their rule table at construction, so later
    edits to :data:`CATEGORY_RULES` never leak into a live classifier.

    Args:
        rules: The ordered rule table; defaults to
            :data:`CATEGORY_RULES`.
        default_skill_ids: The unknown/absent-category fallback;
            defaults to :data:`DEFAULT_CATEGORY_SKILL_IDS`.
    """

    def __init__(
        self,
        rules: Sequence[CategoryRule] | None = None,
        default_skill_ids: Sequence[str] | None = None,
    ) -> None:
        self._rules: tuple[CategoryRule, ...] = tuple(CATEGORY_RULES if rules is None else rules)
        self._default: tuple[str, ...] = tuple(
            DEFAULT_CATEGORY_SKILL_IDS if default_skill_ids is None else default_skill_ids
        )

    def skill_ids_for(self, category: str | None) -> tuple[str, ...]:
        """The routed skill ids for ``category``, sorted and deduplicated.

        Case-insensitive substring matching against every rule in the
        table; all matching rules contribute their ids. A ``None``,
        blank, or unmatched category resolves to the default recon/enum
        core — deterministic, never empty, never raising.
        """
        if category is None:
            return self._default
        lowered = category.lower()
        matched: set[str] = set()
        for rule in self._rules:
            if rule.keyword in lowered:
                matched.update(rule.skill_ids)
        if not matched:
            return self._default
        return tuple(sorted(matched))

    def summaries(
        self,
        registry: SkillRegistry,
        category: str | None,
    ) -> list[SkillSummary]:
        """The compact :class:`~ozzgraph.skills.SkillSummary` ads for ``category``.

        The lazy advertisement path (AGENTS.md rule #6): resolves each
        routed id to its summary via ``registry.load`` — never
        materializing cards here — and returns them sorted by
        ``skill_id`` (the ids are already sorted). A routed id the
        registry does not know fails loudly with
        :class:`~ozzgraph.skills.SkillRegistryError` (AGENTS.md rule
        #9): a broken classifier mapping is a configuration error, not
        a silent skip.

        Raises:
            SkillRegistryError: If the classifier routes an id that is
                not registered in ``registry``.
        """
        return [registry.load(skill_id).summary() for skill_id in self.skill_ids_for(category)]
