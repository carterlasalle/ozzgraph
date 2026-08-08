"""Findings for the OzzGraph generic security-research runtime (V02).

Implements the v2 Findings model (docs/CHANGES_v2.md, "Key technical
changes"): richer than a flag — CWE classification, affected assets,
preconditions, evidence ids, reproduction steps, impact CIA, and
confidence. A :class:`Finding` is created ONLY when a hypothesis was
validated through a deterministic signal (the evaluator's CONFIRMED
verdict backed by evidence ids) — never because a model claimed a
vulnerability (AGENTS.md rule #3: a model claim is a hypothesis; a fact
requires deterministic evidence or evaluator acceptance tied to
evidence ids).

The authoritative copy of a finding is the ``finding`` entity in the
SQLite state graph (same convention as every other graph entity, with
mirrored ``graph.*`` events so replay reconstructs the identical graph).
:class:`FindingStore` additionally renders a compact, human-readable
``findings.json`` in the run's state directory so the operator can read
the run's output without opening the graph (V08 owns the full report;
this is the minimal V02 slice).

Design rules:

- Deterministic: every field is derived from authoritative graph state
  (the confirmed hypothesis, its supporting evidence ids, the seeded
  target set, the evidence chain's action commands) or from a fixed
  conservative default — never free-form model prose.
- Idempotent: the finding id is a pure function of the validated
  hypothesis id (``finding-<hypothesis id>``), so repeated production
  writes nothing new.
- Small kernel (AGENTS.md rule #10): this module owns only the model
  and the JSON renderer; the runner decides WHEN a finding is produced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Entity type for findings persisted in the state graph
#: (docs/DATA_STRATEGY.md, lowercase by convention).
ENTITY_FINDING = "finding"

#: Edge type linking a finding to the hypothesis it validates
#: (docs/DATA_STRATEGY.md, uppercase by convention).
EDGE_FINDING_VALIDATES_HYPOTHESIS = "FINDING VALIDATES HYPOTHESIS"

#: Conservative default CWE classification for an evidence-backed
#: exposure finding. V02 keeps classification deterministic and
#: conservative (the runner stamps ``CWE-200`` on hypotheses whose
#: evidence output matched the configured flag pattern — the lab's
#: sensitive-data signal); the security-brain milestone (v2/6) replaces
#: it with real classification.
DEFAULT_FINDING_CWE = "CWE-200: Exposure of Sensitive Information to an Unauthorized Actor"

#: Conservative impact vocabulary for the V02 minimal impact CIA model.
ImpactLevel = Literal["none", "low", "medium", "high", "unknown"]

#: Bounded reproduction text cap (the evidence chain's action commands).
REPRODUCTION_LIMIT = 1000


class ImpactCIA(BaseModel):
    """The finding's impact assessment across the CIA triad.

    V02 keeps each axis a bounded conservative label (``none`` /
    ``low`` / ``medium`` / ``high`` / ``unknown``); the security-brain
    milestone replaces these with measured impact analysis.
    """

    model_config = ConfigDict(extra="forbid")

    confidentiality: ImpactLevel = "unknown"
    integrity: ImpactLevel = "unknown"
    availability: ImpactLevel = "unknown"


class Finding(BaseModel):
    """One validated, evidence-backed finding (docs/CHANGES_v2.md).

    Attributes:
        id: Deterministic finding id (``finding-<hypothesis id>``).
        cwe: CWE classification of the validated issue.
        affected_assets: Entity ids of the assets the finding affects
            (the seeded ``target`` entities of the run).
        preconditions: Bounded list of conditions that had to hold for
            the issue to be observable.
        evidence_ids: Evidence entity ids backing the finding — the
            confirmed hypothesis's supporting evidence.
        reproduction: Bounded, deterministic reproduction steps (the
            action commands whose observations produced the supporting
            evidence).
        impact: Conservative impact CIA assessment.
        confidence: The validated hypothesis's confidence in [0.0, 1.0].
        hypothesis_id: The confirmed hypothesis entity the finding
            validates.
        target_id: The seeded target entity the finding is scoped to
            (``None`` when the run seeded no targets).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    cwe: str = Field(min_length=1)
    affected_assets: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    reproduction: str = ""
    impact: ImpactCIA = Field(default_factory=ImpactCIA)
    confidence: float = Field(ge=0.0, le=1.0)
    hypothesis_id: str | None = None
    target_id: str | None = None


class FindingStore:
    """Renders every produced finding to ``state_dir/findings.json``.

    The graph is the authoritative store (AGENTS.md rule #1); this is
    the compact operator-facing renderer. ``save`` appends one finding
    and rewrites the JSON document deterministically (sorted keys, one
    finding per list entry), so the file always mirrors the graph's
    ``finding`` entities at the time of the last save.

    Args:
        path: The JSON document path (normally
            ``state_dir / "findings.json"`` via :meth:`for_run`).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._findings: list[Finding] = []

    @classmethod
    def for_run(cls, state_dir: Path) -> FindingStore:
        """The standard run store at ``state_dir / 'findings.json'``."""
        return cls(state_dir / "findings.json")

    @property
    def path(self) -> Path:
        """The rendered JSON document path."""
        return self._path

    @property
    def findings(self) -> tuple[Finding, ...]:
        """Every finding saved so far, in save order."""
        return tuple(self._findings)

    def save(self, finding: Finding) -> None:
        """Append ``finding`` (idempotent per id) and rewrite the JSON.

        Raises:
            OSError: If the document cannot be written (fail loudly,
                AGENTS.md rule #9).
        """
        if any(existing.id == finding.id for existing in self._findings):
            return
        self._findings.append(finding)
        payload = [json.loads(existing.model_dump_json()) for existing in self._findings]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
