"""Tests for the V08 report bundle (docs/adr/0010).

Covers the deterministic renderers (report.md / report.json /
report.sarif) and the full ``render_report_bundle`` materialization
(evidence/ artifact copies, graph.sqlite snapshot, events.jsonl copy)
from a file-backed authoritative state graph, plus the loud failure
paths (missing evidence/artifact, missing event log, invalid finding
payload).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.events import Event, EventLog
from ozzgraph.findings import (
    DEFAULT_FINDING_CWE,
    ENTITY_FINDING,
    Finding,
    ImpactCIA,
)
from ozzgraph.reporting import (
    ReportError,
    ReportMetadata,
    render_json,
    render_markdown,
    render_report_bundle,
    render_sarif,
    sarif_level,
)
from ozzgraph.state_graph import StateGraph

RUN_ID = "user-42"


def _finding(finding_id: str = "finding-hypothesis-abc") -> Finding:
    return Finding(
        id=finding_id,
        cwe=DEFAULT_FINDING_CWE,
        affected_assets=("target-url-123",),
        preconditions=("authorized assessment scope",),
        evidence_ids=("evidence-1",),
        reproduction="curl -sS --max-time 5 http://127.0.0.1:3000/admin",
        impact=ImpactCIA(confidentiality="high", integrity="low", availability="none"),
        confidence=0.9,
        hypothesis_id="hypothesis-abc",
        target_id="target-url-123",
    )


def _metadata(**overrides) -> ReportMetadata:
    base = {
        "run_id": RUN_ID,
        "environment": "local",
        "model_id": "test-model",
        "status": "completed",
        "reason": "every objective completed",
        "turns": 4,
        "model_calls": 2,
        "tool_calls": 3,
        "scope_mode": "url",
        "generated_at": "2026-08-08T00:00:00+00:00",
    }
    base.update(overrides)
    return ReportMetadata(**base)  # type: ignore[arg-type] - test helper


async def _seed_run(
    state_dir: Path,
    *,
    findings: list[Finding] | None = None,
    with_evidence: bool = True,
) -> tuple[StateGraph, ArtifactStore]:
    """A file-backed graph + artifact store mirroring a completed run."""
    state_dir.mkdir(parents=True, exist_ok=True)
    graph = StateGraph(state_dir / "graph.db")
    await graph.open()
    at = datetime(2026, 8, 8, tzinfo=UTC)
    await graph.create_entity(
        f"run-{RUN_ID}", "run", {"environment": "local", "model_id": "test-model"}, at=at
    )
    await graph.create_entity(
        "scope-1",
        "scope",
        {
            "name": "local",
            "hosts": [],
            "urls": ["http://127.0.0.1:3000"],
            "networks": [],
            "credentials": [],
            "capabilities": ["http.request"],
            "constraints": {"mode": "url", "target_modes": ["url"]},
        },
        at=at,
    )
    await graph.create_entity(
        "target-url-123",
        "target",
        {"address": "http://127.0.0.1:3000", "type": "url", "confirmed": False},
        at=at,
    )
    await graph.create_entity(
        "objective-local-1",
        "objective",
        {"description": "Complete the assessment", "completed": True},
        at=at,
    )
    artifacts = ArtifactStore(state_dir / "artifacts")
    if with_evidence:
        await artifacts.put(
            source=b"HTTP/1.1 200 OK\nadmin secret exposed",
            artifact_id="artifact-1",
            source_action="action-fp-1",
        )
        await graph.create_entity(
            "evidence-1",
            "evidence",
            {"note": "admin page exposed", "artifact_id": "artifact-1"},
            at=at,
        )
    for finding in findings or []:
        await graph.create_entity(
            finding.id, ENTITY_FINDING, finding.model_dump(mode="json"), at=at
        )
    log = EventLog.for_run(state_dir)
    log.append(Event(run_id=RUN_ID, timestamp=at, event_type="runner.started", producer="runner"))
    log.append(
        Event(run_id=RUN_ID, timestamp=at, event_type="runner.terminated", producer="runner")
    )
    return graph, artifacts


# ---------------------------------------------------------------------------
# deterministic renderers
# ---------------------------------------------------------------------------


def test_sarif_level_maps_impact_to_levels() -> None:
    assert sarif_level(ImpactCIA(confidentiality="high")) == "error"
    assert sarif_level(ImpactCIA(integrity="medium")) == "warning"
    assert sarif_level(ImpactCIA(availability="low")) == "note"
    assert sarif_level(ImpactCIA()) == "warning"  # none/unknown -> warning


def test_render_markdown_is_deterministic_and_complete() -> None:
    finding = _finding()
    targets = ("http://127.0.0.1:3000",)
    counts = {"finding": 1, "evidence": 1, "action": 2}
    text = render_markdown((finding,), _metadata(), targets, counts)
    assert "OzzGraph Assessment Report" in text
    assert "Run: user-42" in text
    assert "Termination: completed" in text
    assert finding.id in text
    assert "CWE-200" in text
    assert "Severity: error" in text  # confidentiality=high
    assert "curl -sS --max-time 5" in text  # reproduction
    assert "evidence-1" in text
    assert render_markdown((finding,), _metadata(), targets, counts) == text


def test_render_markdown_handles_no_findings() -> None:
    text = render_markdown((), _metadata(), (), {"finding": 0, "evidence": 0, "action": 0})
    assert "No findings were produced by this run." in text


def test_render_json_carries_graph_metadata_and_findings() -> None:
    finding = _finding()
    targets = ()
    scope = None
    counts = {"finding": 1, "evidence": 1, "action": 2, "total": 5}
    document = json.loads(render_json((finding,), _metadata(), targets, scope, counts))
    assert document["schema_version"] == 1
    assert document["run"]["id"] == RUN_ID
    assert document["run"]["environment"] == "local"
    assert document["termination"]["status"] == "completed"
    assert document["termination"]["turns"] == 4
    assert document["counts"] == counts
    assert document["findings"][0]["id"] == finding.id
    # Same data as findings.json (V02): the payloads are identical.
    assert document["findings"][0] == json.loads(finding.model_dump_json())


def test_render_sarif_is_valid_210_document() -> None:
    finding = _finding()
    document = json.loads(render_sarif((finding,), _metadata(), {finding.id: ("artifact-1",)}))
    assert document["version"] == "2.1.0"
    assert document["$schema"].endswith("sarif-schema-2.1.0.json")
    run = document["runs"][0]
    assert run["tool"]["driver"]["name"] == "ozzgraph"
    assert run["tool"]["driver"]["rules"][0]["id"] == "CWE-200"
    result = run["results"][0]
    assert result["ruleId"] == "CWE-200"
    assert result["ruleIndex"] == 0
    assert result["level"] == "error"
    assert result["properties"]["findingId"] == finding.id
    assert result["properties"]["confidence"] == 0.9
    # Locations come from the materialized evidence artifacts.
    location = result["locations"][0]
    assert location["physicalLocation"]["artifactLocation"]["uri"] == "evidence/artifact-1"


def test_render_sarif_handles_non_cwe_classification() -> None:
    finding = _finding().model_copy(update={"cwe": "custom-classification"})
    document = json.loads(render_sarif((finding,), _metadata(), {}))
    assert document["runs"][0]["results"][0]["ruleId"] == "custom-classification"
    assert document["runs"][0]["results"][0]["locations"] == []


# ---------------------------------------------------------------------------
# full bundle materialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_report_bundle_produces_full_bundle(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    graph, artifacts = await _seed_run(state_dir, findings=[_finding()])
    try:
        bundle = await render_report_bundle(
            state_dir=state_dir,
            graph=graph,
            artifacts=artifacts,
            run_id=RUN_ID,
            status="completed",
            reason="every objective completed",
            turns=4,
            model_calls=2,
            tool_calls=3,
        )
    finally:
        await graph.close()

    assert bundle.report_md.is_file()
    assert bundle.report_json.is_file()
    assert bundle.report_sarif.is_file()
    assert (bundle.evidence_dir / "artifact-1").is_file()
    assert bundle.graph_sqlite.is_file()
    assert bundle.events_jsonl.is_file()

    # report.json mirrors the graph: run id, termination, counts, findings.
    report = json.loads(bundle.report_json.read_text(encoding="utf-8"))
    assert report["run"]["id"] == RUN_ID
    assert report["termination"]["reason"] == "every objective completed"
    assert report["counts"]["finding"] == 1
    assert report["counts"]["evidence"] == 1
    assert report["findings"][0]["id"] == "finding-hypothesis-abc"
    assert report["targets"][0]["address"] == "http://127.0.0.1:3000"
    assert report["scope"]["name"] == "local"

    # report.md is the human writeup of the same finding.
    markdown = bundle.report_md.read_text(encoding="utf-8")
    assert "### finding-hypothesis-abc — CWE-200" in markdown
    assert "affected" in markdown.casefold()

    # report.sarif maps the finding to its CWE with evidence locations.
    sarif = json.loads(bundle.report_sarif.read_text(encoding="utf-8"))
    assert sarif["runs"][0]["results"][0]["ruleId"] == "CWE-200"
    assert (
        sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"][
            "uri"
        ]
        == "evidence/artifact-1"
    )

    # The evidence copy matches the authoritative artifact bytes.
    assert (
        bundle.evidence_dir / "artifact-1"
    ).read_bytes() == b"HTTP/1.1 200 OK\nadmin secret exposed"

    # graph.sqlite is a consistent SQLite snapshot of the authoritative graph.
    async with StateGraph(bundle.graph_sqlite) as snapshot:
        assert len(await snapshot.list_entities("finding")) == 1
        assert len(await snapshot.list_entities("target")) == 1

    # events.jsonl mirrors the authoritative run log.
    events = bundle.events_jsonl.read_text(encoding="utf-8").splitlines()
    assert len(events) == 2
    assert json.loads(events[0])["event_type"] == "runner.started"

    # The authoritative files were never modified.
    assert (state_dir / "graph.db").is_file()
    assert (state_dir / "actions.jsonl").is_file()


@pytest.mark.asyncio
async def test_render_report_bundle_is_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    graph, artifacts = await _seed_run(state_dir, findings=[_finding()])
    try:
        first = await render_report_bundle(
            state_dir=state_dir,
            graph=graph,
            artifacts=artifacts,
            run_id=RUN_ID,
            status="completed",
            reason="done",
            turns=1,
            model_calls=0,
            tool_calls=0,
        )
        second = await render_report_bundle(
            state_dir=state_dir,
            graph=graph,
            artifacts=artifacts,
            run_id=RUN_ID,
            status="completed",
            reason="done",
            turns=1,
            model_calls=0,
            tool_calls=0,
        )
    finally:
        await graph.close()

    for attr in ("report_md", "report_json", "report_sarif", "events_jsonl"):
        assert getattr(first, attr).read_bytes() == getattr(second, attr).read_bytes()
    assert (first.evidence_dir / "artifact-1").read_bytes() == (
        second.evidence_dir / "artifact-1"
    ).read_bytes()


@pytest.mark.asyncio
async def test_render_report_bundle_without_findings(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    graph, artifacts = await _seed_run(state_dir, findings=[])
    try:
        bundle = await render_report_bundle(
            state_dir=state_dir,
            graph=graph,
            artifacts=artifacts,
            run_id=RUN_ID,
            status="completed",
            reason="done",
            turns=0,
            model_calls=0,
            tool_calls=0,
        )
    finally:
        await graph.close()

    report = json.loads(bundle.report_json.read_text(encoding="utf-8"))
    assert report["counts"]["finding"] == 0
    assert report["findings"] == []
    assert "No findings were produced by this run." in bundle.report_md.read_text(encoding="utf-8")
    assert json.loads(bundle.report_sarif.read_text(encoding="utf-8"))["runs"][0]["results"] == []


# ---------------------------------------------------------------------------
# loud failure paths (AGENTS.md rule #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_fails_loudly_on_missing_evidence(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    finding = _finding()
    # Seed WITHOUT the evidence entity the finding references.
    graph, artifacts = await _seed_run(state_dir, findings=[finding], with_evidence=False)
    try:
        with pytest.raises(ReportError, match="missing evidence"):
            await render_report_bundle(
                state_dir=state_dir,
                graph=graph,
                artifacts=artifacts,
                run_id=RUN_ID,
                status="completed",
                reason="done",
                turns=0,
                model_calls=0,
                tool_calls=0,
            )
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_render_fails_loudly_on_missing_artifact(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    graph, artifacts = await _seed_run(state_dir, findings=[_finding()])
    try:
        # The evidence references an artifact that was never stored.
        await artifacts.put(
            source=b"x" * 3,
            artifact_id="other",
        )
        record = await graph.get_entity("evidence-1")
        assert record is not None
        await graph.update_entity(
            "evidence-1",
            dict(record.data, artifact_id="missing-artifact"),
        )
        with pytest.raises(ReportError, match="missing artifact"):
            await render_report_bundle(
                state_dir=state_dir,
                graph=graph,
                artifacts=artifacts,
                run_id=RUN_ID,
                status="completed",
                reason="done",
                turns=0,
                model_calls=0,
                tool_calls=0,
            )
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_render_fails_loudly_on_invalid_finding_payload(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    graph, artifacts = await _seed_run(state_dir, findings=[])
    try:
        await graph.create_entity(
            "finding-broken",
            ENTITY_FINDING,
            {"id": "finding-broken"},  # invalid: no cwe
        )
        with pytest.raises(ReportError, match="invalid payload"):
            await render_report_bundle(
                state_dir=state_dir,
                graph=graph,
                artifacts=artifacts,
                run_id=RUN_ID,
                status="completed",
                reason="done",
                turns=0,
                model_calls=0,
                tool_calls=0,
            )
    finally:
        await graph.close()


@pytest.mark.asyncio
async def test_render_fails_loudly_on_missing_event_log(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    graph, artifacts = await _seed_run(state_dir, findings=[_finding()])
    try:
        (state_dir / "actions.jsonl").unlink()
        with pytest.raises(ReportError, match="events.jsonl"):
            await render_report_bundle(
                state_dir=state_dir,
                graph=graph,
                artifacts=artifacts,
                run_id=RUN_ID,
                status="completed",
                reason="done",
                turns=0,
                model_calls=0,
                tool_calls=0,
            )
    finally:
        await graph.close()
