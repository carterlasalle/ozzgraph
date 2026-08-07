#!/usr/bin/env python3
"""E2E-001 forensic analysis: exactly which stores/events carry the test flag.

Re-runs the F2B journey (reusing the driver's real-code-path cycle) against
a kept state dir, then classifies every file/event containing the raw flag:

  1. event log (actions.jsonl) — per event_type
  2. state graph (graph.db) — per entity type
  3. artifact store — index fields vs content files
  4. replay db

This is the evidence base for .coding-hermes/tests/crypto/flag_leak.md and
.coding-hermes/tests/audit/flag_leak.md. Flag material is never printed;
only locations and counts are reported.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / ".coding-hermes" / "tests" / "scripts"))
sys.path.insert(0, str(REPO))

import e2e_001_driver as driver
import mcp_fake

from ozzgraph.lab import get_target


def classify(path: Path, flag: str) -> list[str]:
    data = path.read_bytes()
    if flag.encode() not in data:
        return []
    if path.name == "actions.jsonl":
        hits = []
        for line in data.decode().splitlines():
            ev = json.loads(line)
            if flag in json.dumps(ev):
                hits.append(ev["event_type"])
        return [f"actions.jsonl events: {sorted(set(hits))}"]
    if path.name in ("graph.db", "replay.db"):
        return [f"{path.name}: binary sqlite (entity payloads — see entity list below)"]
    return [f"{path.name}"]


async def main() -> None:
    out: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="e2e-001-forensic-") as tmp:
        state_dir = Path(tmp) / "state"
        with get_target("hidden-routes") as target:
            os.environ["OZZGRAPH_TARGET"] = target.target_value
            flag = target.flag
            server = mcp_fake.FakeMcpServer(driver.build_handler(flag))
            server.start_threaded()
            os.environ[driver.MCP_BASE_URL_ENV] = server.base_url
            os.environ[driver.MCP_TIMEOUT_ENV] = "5"
            os.environ[driver.MCP_MAX_RETRIES_ENV] = "0"
            os.environ[driver.CHALLENGE_ID_ENV] = "web-01"
            os.environ[driver.HAL_PRIVILEGED_ENV] = "1"
            try:
                _, _ = await driver.f2b_cycle(server, flag, state_dir)
            finally:
                server.stop_threaded()

        # 1. event log: per-event_type flag presence
        with (state_dir / "actions.jsonl").open() as handle:
            events = [json.loads(line) for line in handle]
        flagged_events = {ev["event_type"]: flag in json.dumps(ev) for ev in events}
        out["event_log"] = {
            "total_events": len(events),
            "event_types_containing_raw_flag": sorted(
                t for t, hit in flagged_events.items() if hit
            ),
            "event_types_without_flag": sorted(t for t, hit in flagged_events.items() if not hit),
        }

        # which of those flagged events are graph.* (replay-required) vs run events?
        graph_flagged = [
            t
            for t in out["event_log"]["event_types_containing_raw_flag"]  # type: ignore[union-attr]
            if t.startswith("graph.")
        ]
        run_flagged = [
            t
            for t in out["event_log"]["event_types_containing_raw_flag"]  # type: ignore[union-attr]
            if not t.startswith("graph.")
        ]
        out["event_log"]["graph_events_with_flag_replay_required"] = graph_flagged
        out["event_log"]["run_events_with_flag_not_replay_required"] = run_flagged

        # 2. state graph: which entity types hold the flag (via event payloads)
        entity_created = [ev for ev in events if ev["event_type"] == "graph.entity_created"]
        flagged_entities = [
            {"entity_id": ev["payload"]["entity_id"], "entity_type": ev["payload"]["entity_type"]}
            for ev in entity_created
            if flag in json.dumps(ev)
        ]
        out["graph_entities_containing_flag"] = flagged_entities

        # 3. artifact store: index fields vs content
        store = driver.ArtifactStore.for_run(state_dir)
        records = await store.list()
        index_flagged = [
            r.artifact_id for r in records if flag in json.dumps(r.model_dump(mode="json"))
        ]
        content_flagged = []
        for r in records:
            p = store.path_for(r.artifact_id)
            if p.exists() and flag.encode() in p.read_bytes():
                content_flagged.append(r.artifact_id)
        out["artifacts"] = {
            "index_records_with_flag": index_flagged,
            "content_files_with_flag": content_flagged,
            "index_record_field_names": sorted(records[0].model_dump().keys()) if records else [],
        }

        # 4. all files sweep
        all_hits = []
        for p in sorted(state_dir.rglob("*")):
            if p.is_file():
                all_hits += [cls for cls in classify(p, flag)]
        out["state_dir_file_sweep"] = all_hits

    results = REPO / "e2e-output" / "forensic_analysis.json"
    results.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
