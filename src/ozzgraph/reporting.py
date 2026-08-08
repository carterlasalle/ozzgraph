"""Report bundle for a completed OzzGraph run (V08, v2/local-assessment).

When a local run terminates COMPLETED, the runner renders the full
report bundle into ``state_dir`` (docs/adr/0010):

- ``report.md`` — human-readable per-finding writeup: finding id, CWE,
  severity (derived from the impact CIA), affected assets,
  preconditions, evidence ids, reproduction commands, impact CIA, and
  confidence.
- ``report.json`` — structured report: the same finding payloads as the
  V02 ``findings.json`` plus graph metadata (run id, environment,
  model id, targets, scope, termination reason, entity counts).
- ``report.sarif`` — a SARIF 2.1.0 document: one result per finding
  mapped to its CWE rule, with locations pointing at the materialized
  evidence artifacts, driver ``ozzgraph``.
- ``evidence/`` — a copy of every artifact referenced by the run's
  finding evidence chains (from the authoritative artifact store),
  named by artifact id.
- ``graph.sqlite`` + ``events.jsonl`` — deterministic snapshots of the
  authoritative ``graph.db`` (SQLite online-backup API) and the
  append-only ``actions.jsonl`` event log under the milestone's
  canonical names. The authoritative files are never modified, so
  replay compatibility is preserved (the bundle is derived output).

Design rules:

- Deterministic: every byte derives from authoritative graph state
  (finding entities, the run/scope/target entities, the evidence
  chains) — never from model prose or wall-clock time. Sorted keys,
  sorted findings, sorted rules.
- Fail loudly (AGENTS.md rule #9): a missing evidence entity or
  artifact, an unreadable event log, or a graph that cannot be
  snapshotted raises :class:`ReportError` — the runner records it as a
  ``runner.report_failed`` event.
- Small kernel (AGENTS.md rule #10): reporting owns only the renderers;
  the runner decides WHEN to render.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ozzgraph import __version__
from ozzgraph.artifacts import ArtifactStore, ArtifactStoreError
from ozzgraph.findings import ENTITY_FINDING, Finding, ImpactCIA
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Canonical bundle file names (docs/adr/0010).
REPORT_MD_NAME = "report.md"
REPORT_JSON_NAME = "report.json"
REPORT_SARIF_NAME = "report.sarif"
EVIDENCE_DIR_NAME = "evidence"
GRAPH_SQLITE_NAME = "graph.sqlite"
EVENTS_JSONL_NAME = "events.jsonl"

#: The authoritative event log the bundle snapshot mirrors.
_EVENTS_SOURCE_NAME = "actions.jsonl"

#: The authoritative graph file the bundle snapshot mirrors.
_GRAPH_SOURCE_NAME = "graph.db"

#: SARIF 2.1.0 schema URL (fixed by the spec).
_SARIF_SCHEMA_URL = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)

#: Entity types whose counts land in ``report.json`` (zero-filled).
_COUNT_TYPES = (
    "run",
    "scope",
    "target",
    "objective",
    "action",
    "observation",
    "evidence",
    "hypothesis",
    "plan",
    "plan_step",
    "evaluation",
    "finding",
)

#: SARIF result levels, ordered weakest -> strongest (deterministic
#: mapping, docs/adr/0010).
_SARIF_LEVEL = Literal["error", "warning", "note"]

#: CWE id extraction: ``CWE-200`` from ``CWE-200: Exposure of ...``.
_CWE_ID_RE = re.compile(r"CWE-\d+", re.IGNORECASE)


class ReportError(RuntimeError):
    """A report-bundle render failed loudly (AGENTS.md rule #9)."""


@dataclass(frozen=True)
class ReportMetadata:
    """Run-level facts every renderer shares (all derived from state).

    Attributes:
        run_id: The run identifier recorded on every event.
        environment: The environment adapter's scope name.
        model_id: The model identifier the runner called.
        status: The terminal status (``completed`` when the bundle is
            rendered).
        reason: The human-readable termination reason.
        turns: Loop iterations executed.
        model_calls: Model calls consumed (from the budget tracker).
        tool_calls: Tool calls consumed (from the budget tracker).
        scope_mode: The scope's assessment mode (``url`` / ``network`` /
            ``host`` / ``repository`` / ``docker-compose`` / ``hybrid``).
        generated_at: The run entity's creation timestamp (deterministic
            — never wall-clock time), or None without a run entity.
    """

    run_id: str
    environment: str
    model_id: str
    status: str
    reason: str
    turns: int
    model_calls: int
    tool_calls: int
    scope_mode: str
    generated_at: str | None


@dataclass(frozen=True)
class ReportBundle:
    """The rendered bundle's paths inside ``state_dir``."""

    state_dir: Path
    report_md: Path
    report_json: Path
    report_sarif: Path
    evidence_dir: Path
    graph_sqlite: Path
    events_jsonl: Path


# ----------------------------------------------------------------------
# deterministic renderers
# ----------------------------------------------------------------------


def _cwe_id(cwe: str) -> str:
    """The SARIF rule id for a finding's CWE string.

    ``CWE-200: Exposure ...`` -> ``CWE-200``; a classification that is
    not CWE-shaped falls back to the bounded string itself so the rule
    id is always non-empty and deterministic.
    """
    match = _CWE_ID_RE.search(cwe)
    if match is not None:
        return match.group(0).upper()
    return cwe[:64] or "CWE-UNKNOWN"


def sarif_level(impact: ImpactCIA) -> _SARIF_LEVEL:
    """The SARIF result level derived from the impact CIA assessment.

    Deterministic mapping (docs/adr/0010): any ``high`` axis is an
    ``error``; else any ``medium`` is a ``warning``; else any ``low``
    is a ``note``; a fully ``none``/``unknown`` impact is a conservative
    ``warning`` (a validated finding is still a finding).
    """
    axes = (impact.confidentiality, impact.integrity, impact.availability)
    if "high" in axes:
        return "error"
    if "medium" in axes:
        return "warning"
    if "low" in axes:
        return "note"
    return "warning"


def render_markdown(
    findings: tuple[Finding, ...],
    metadata: ReportMetadata,
    target_addresses: tuple[str, ...],
    counts: dict[str, int],
) -> str:
    """The human-readable per-finding writeup (``report.md``)."""
    lines = [
        "# OzzGraph Assessment Report",
        "",
        f"- Run: {metadata.run_id}",
        f"- Environment: {metadata.environment}",
        f"- Model: {metadata.model_id}",
        f"- Targets: {', '.join(target_addresses) or '(none)'}",
        f"- Scope mode: {metadata.scope_mode}",
        f"- Termination: {metadata.status} — {metadata.reason}",
        (
            f"- Counts: {counts['finding']} finding(s), "
            f"{counts['evidence']} evidence, {counts['action']} action(s)"
        ),
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("No findings were produced by this run.")
    for finding in findings:
        level = sarif_level(finding.impact)
        lines.extend(
            [
                f"### {finding.id} — {finding.cwe}",
                "",
                (
                    f"- Severity: {level} (CIA: "
                    f"confidentiality={finding.impact.confidentiality}, "
                    f"integrity={finding.impact.integrity}, "
                    f"availability={finding.impact.availability})"
                ),
                f"- Confidence: {finding.confidence}",
                f"- Affected assets: {', '.join(finding.affected_assets) or '(none)'}",
                f"- Preconditions: {'; '.join(finding.preconditions) or '(none)'}",
                f"- Evidence: {', '.join(finding.evidence_ids) or '(none)'}",
                f"- Hypothesis: {finding.hypothesis_id or '(none)'}",
                f"- Target: {finding.target_id or '(none)'}",
                "",
                "### Reproduction",
                "",
                "```",
                finding.reproduction or "(none)",
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def render_json(
    findings: tuple[Finding, ...],
    metadata: ReportMetadata,
    targets: tuple[EntityRecord, ...],
    scope: EntityRecord | None,
    counts: dict[str, int],
) -> str:
    """The structured report (``report.json``).

    The ``findings`` array carries the same payloads as the V02
    ``findings.json`` (the graph entities are the authoritative store;
    this document adds the graph metadata around them).
    """
    document: dict[str, object] = {
        "schema_version": 1,
        "run": {
            "id": metadata.run_id,
            "environment": metadata.environment,
            "model_id": metadata.model_id,
        },
        "targets": [
            {
                "id": record.id,
                "type": record.data.get("type"),
                "address": record.data.get("address"),
                "metadata": record.data.get("metadata", {}),
            }
            for record in targets
        ],
        "scope": _scope_summary(scope),
        "termination": {
            "status": metadata.status,
            "reason": metadata.reason,
            "turns": metadata.turns,
            "model_calls": metadata.model_calls,
            "tool_calls": metadata.tool_calls,
        },
        "counts": counts,
        "findings": [json.loads(finding.model_dump_json()) for finding in findings],
    }
    if metadata.generated_at is not None:
        document["generated_at"] = metadata.generated_at
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_sarif(
    findings: tuple[Finding, ...],
    metadata: ReportMetadata,
    evidence_artifacts: dict[str, tuple[str, ...]],
) -> str:
    """The SARIF 2.1.0 document (``report.sarif``).

    One result per finding, ruleId mapped to the finding's CWE, level
    derived from the impact CIA, and locations pointing at the
    materialized ``evidence/`` artifacts (empty when a finding carries
    no evidence chain). Driver: ``ozzgraph``.
    """
    rules: dict[str, str] = {}
    for finding in findings:
        rules.setdefault(_cwe_id(finding.cwe), finding.cwe)
    rule_ids = sorted(rules)
    rule_index = {rule_id: index for index, rule_id in enumerate(rule_ids)}

    results: list[dict[str, object]] = []
    for finding in sorted(findings, key=lambda item: item.id):
        rule_id = _cwe_id(finding.cwe)
        results.append(
            {
                "ruleId": rule_id,
                "ruleIndex": rule_index[rule_id],
                "level": sarif_level(finding.impact),
                "message": {"text": finding.cwe},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f"{EVIDENCE_DIR_NAME}/{name}"}
                        }
                    }
                    for name in evidence_artifacts.get(finding.id, ())
                ],
                "partialFingerprints": {"findingId": finding.id},
                "properties": {
                    "findingId": finding.id,
                    "confidence": finding.confidence,
                    "affectedAssets": list(finding.affected_assets),
                    "hypothesisId": finding.hypothesis_id,
                    "targetId": finding.target_id,
                },
            }
        )

    document = {
        "$schema": _SARIF_SCHEMA_URL,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ozzgraph",
                        "version": __version__,
                        "rules": [
                            {
                                "id": rule_id,
                                "shortDescription": {"text": description},
                            }
                            for rule_id, description in rules.items()
                        ],
                    }
                },
                "results": results,
                "automationDetails": {"id": metadata.run_id},
                "properties": {
                    "runId": metadata.run_id,
                    "environment": metadata.environment,
                    "modelId": metadata.model_id,
                    "status": metadata.status,
                    "reason": metadata.reason,
                    "scopeMode": metadata.scope_mode,
                },
            }
        ],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _scope_summary(scope: EntityRecord | None) -> dict[str, object] | None:
    """The scope entity's surface as JSON, or None without a scope."""
    if scope is None:
        return None
    return {
        "id": scope.id,
        "name": scope.data.get("name"),
        "hosts": scope.data.get("hosts", []),
        "urls": scope.data.get("urls", []),
        "networks": scope.data.get("networks", []),
        "credentials": scope.data.get("credentials", []),
        "capabilities": scope.data.get("capabilities", []),
    }


# ----------------------------------------------------------------------
# materialization + orchestration
# ----------------------------------------------------------------------


async def _materialize_evidence(
    graph: StateGraph,
    artifacts: ArtifactStore,
    evidence_dir: Path,
) -> dict[str, tuple[str, ...]]:
    """Copy every finding-referenced artifact into ``evidence_dir``.

    Returns a mapping of finding id -> the artifact filenames copied
    for it (in evidence-id order). The artifact store is authoritative
    (AGENTS.md rule #1): a finding referencing a missing evidence
    entity or a missing artifact raises :class:`ReportError` loudly.

    Raises:
        ReportError: If an evidence reference does not resolve or the
            artifact cannot be copied.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    copied: set[str] = set()
    per_finding: dict[str, list[str]] = {}
    for record in await graph.list_entities(ENTITY_FINDING):
        finding_id = record.id
        names: list[str] = []
        raw_evidence = record.data.get("evidence_ids")
        evidence_ids = raw_evidence if isinstance(raw_evidence, (list, tuple)) else ()
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or not evidence_id:
                continue
            evidence = await graph.get_entity(evidence_id)
            if evidence is None:
                raise ReportError(
                    f"finding {finding_id!r} references missing evidence "
                    f"{evidence_id!r}; cannot materialize evidence"
                )
            artifact_id = evidence.data.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                continue
            try:
                artifact_path = artifacts.path_for(artifact_id)
            except ArtifactStoreError as exc:
                raise ReportError(
                    f"finding {finding_id!r} references missing artifact {artifact_id!r}: {exc}"
                ) from exc
            if artifact_id not in copied:
                try:
                    shutil.copyfile(artifact_path, evidence_dir / artifact_id)
                except OSError as exc:
                    raise ReportError(
                        f"failed to copy evidence artifact {artifact_id!r} "
                        f"into {evidence_dir}: {exc}"
                    ) from exc
                copied.add(artifact_id)
            names.append(artifact_id)
        per_finding[finding_id] = names
    return {finding_id: tuple(names) for finding_id, names in per_finding.items()}


def _snapshot_graph(graph: StateGraph, state_dir: Path) -> None:
    """Snapshot the authoritative graph into ``graph.sqlite``.

    Uses the sqlite3 online-backup API so the copy is consistent even
    while the source connection (WAL) is open. A non-file-backed graph
    (e.g. ``:memory:`` in tests) has nothing to snapshot and is skipped.

    Raises:
        ReportError: If the backup fails.
    """
    source_path = graph.path
    if not source_path.is_file():
        return
    destination = state_dir / GRAPH_SQLITE_NAME
    try:
        source = sqlite3.connect(str(source_path))
        try:
            target = sqlite3.connect(str(destination))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error as exc:
        raise ReportError(f"failed to snapshot graph into {destination}: {exc}") from exc


def _snapshot_events(state_dir: Path) -> None:
    """Copy the authoritative event log into ``events.jsonl``.

    Raises:
        ReportError: If the source log is missing or cannot be copied.
    """
    source = state_dir / _EVENTS_SOURCE_NAME
    if not source.is_file():
        raise ReportError(f"event log {source} is missing; cannot materialize events.jsonl")
    try:
        shutil.copyfile(source, state_dir / EVENTS_JSONL_NAME)
    except OSError as exc:
        raise ReportError(
            f"failed to copy event log into {state_dir / EVENTS_JSONL_NAME}: {exc}"
        ) from exc


def _entity_counts(records: list[EntityRecord]) -> dict[str, int]:
    """Deterministic per-type entity counts (zero-filled)."""
    counts: dict[str, int] = {}
    for entity_type in _COUNT_TYPES:
        counts[entity_type] = sum(1 for record in records if record.type == entity_type)
    counts["total"] = len(records)
    return counts


def _findings(records: list[EntityRecord]) -> tuple[Finding, ...]:
    """The validated findings, sorted by id.

    Raises:
        ReportError: If a finding entity's payload does not validate
            against the :class:`~ozzgraph.findings.Finding` model (the
            authoritative store must not be silently skipped).
    """
    findings: list[Finding] = []
    for record in records:
        if record.type != ENTITY_FINDING:
            continue
        try:
            findings.append(Finding.model_validate(record.data))
        except Exception as exc:  # loud, rule #9
            raise ReportError(
                f"finding entity {record.id!r} has an invalid payload: {exc}"
            ) from exc
    return tuple(sorted(findings, key=lambda finding: finding.id))


def _metadata(
    run_id: str,
    status: str,
    reason: str,
    turns: int,
    model_calls: int,
    tool_calls: int,
    records: list[EntityRecord],
) -> ReportMetadata:
    """Run metadata derived from graph entities (never wall-clock time)."""
    run_record = next((record for record in records if record.type == "run"), None)
    environment = "unknown"
    model_id = "unknown"
    generated_at: str | None = None
    if run_record is not None:
        raw_environment = run_record.data.get("environment")
        if isinstance(raw_environment, str) and raw_environment:
            environment = raw_environment
        raw_model = run_record.data.get("model_id")
        if isinstance(raw_model, str) and raw_model:
            model_id = raw_model
        generated_at = run_record.created_at.isoformat()
    scope_record = next((record for record in records if record.type == "scope"), None)
    raw_mode = "none"
    if scope_record is not None:
        raw_constraints = scope_record.data.get("constraints")
        if isinstance(raw_constraints, dict):
            raw_mode = raw_constraints.get("mode", "none")
        if not isinstance(raw_mode, str) or not raw_mode:
            raw_mode = "none"
    return ReportMetadata(
        run_id=run_id,
        environment=environment,
        model_id=model_id,
        status=status,
        reason=reason,
        turns=turns,
        model_calls=model_calls,
        tool_calls=tool_calls,
        scope_mode=raw_mode,
        generated_at=generated_at,
    )


async def render_report_bundle(
    *,
    state_dir: Path,
    graph: StateGraph,
    artifacts: ArtifactStore,
    run_id: str,
    status: str,
    reason: str,
    turns: int,
    model_calls: int,
    tool_calls: int,
) -> ReportBundle:
    """Render the full report bundle into ``state_dir``.

    Everything derives from authoritative graph state: the finding
    entities, the run/scope/target entities, the evidence chains, and
    the authoritative ``graph.db`` / ``actions.jsonl`` files. Rendering
    is idempotent and deterministic; the authoritative files are never
    modified (replay compatibility, docs/adr/0010).

    Args:
        state_dir: The run's state directory (created when missing).
        graph: The open authoritative state graph.
        artifacts: The run's artifact store.
        run_id: The run identifier.
        status: The terminal status (``completed`` for the bundle).
        reason: The human-readable termination reason.
        turns: Loop iterations executed.
        model_calls: Model calls consumed.
        tool_calls: Tool calls consumed.

    Raises:
        ReportError: If any required state is missing or unreadable
            (AGENTS.md rule #9 — never a silent partial bundle).

    Returns:
        The rendered bundle's paths.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    records = await graph.list_entities()

    findings = _findings(records)
    metadata = _metadata(run_id, status, reason, turns, model_calls, tool_calls, records)
    targets = tuple(record for record in records if record.type == "target")
    scope = next((record for record in records if record.type == "scope"), None)
    counts = _entity_counts(records)
    evidence_artifacts = await _materialize_evidence(
        graph, artifacts, state_dir / EVIDENCE_DIR_NAME
    )
    _snapshot_graph(graph, state_dir)
    _snapshot_events(state_dir)

    target_addresses: tuple[str, ...] = ()
    addresses: list[str] = []
    for record in targets:
        raw_address = record.data.get("address")
        if isinstance(raw_address, str):
            addresses.append(raw_address)
    target_addresses = tuple(addresses)
    (state_dir / REPORT_MD_NAME).write_text(
        render_markdown(findings, metadata, target_addresses, counts),
        encoding="utf-8",
    )
    (state_dir / REPORT_JSON_NAME).write_text(
        render_json(findings, metadata, targets, scope, counts), encoding="utf-8"
    )
    (state_dir / REPORT_SARIF_NAME).write_text(
        render_sarif(findings, metadata, evidence_artifacts), encoding="utf-8"
    )

    return ReportBundle(
        state_dir=state_dir,
        report_md=state_dir / REPORT_MD_NAME,
        report_json=state_dir / REPORT_JSON_NAME,
        report_sarif=state_dir / REPORT_SARIF_NAME,
        evidence_dir=state_dir / EVIDENCE_DIR_NAME,
        graph_sqlite=state_dir / GRAPH_SQLITE_NAME,
        events_jsonl=state_dir / EVENTS_JSONL_NAME,
    )
