# E2E-001 — F2B/B2F End-to-End Cycle (ozzgraph)

## Run summary

- **Date:** 2026-08-07 — initial run 14:09 UTC-7; reproducibility re-run 14:30 UTC-7 (identical results).
- **Driver:** `.coding-hermes/tests/scripts/e2e_001_driver.py` — runs the REAL ozzgraph kernel end-to-end against `tests/mcp_fake.py` FakeMcpServer (HalCTF platform side) + `ozzgraph.lab` "hidden-routes" target (challenge side).
- **Forensic:** `.coding-hermes/tests/scripts/e2e_001_forensic.py` — post-run sweep of state dir, event log, and graph/replay DBs for flag material and provenance shape.
- **Totals:** 65 PASS / 1 FAIL / 1 UNTESTABLE (67 checks) — reproducible across both runs.
- **Raw data:** `e2e-output/raw_results.json`, `e2e-output/forensic_analysis.json`.

### Counts per dimension

| Dimension | PASS | FAIL | UNTESTABLE |
|-----------|------|------|------------|
| f2b       | 10   | 0    | 0          |
| b2f       | 5    | 0    | 0          |
| negative  | 34   | 0    | 1          |
| wiring    | 3    | 0    | 0          |
| audit     | 5    | 0    | 0          |
| crypto    | 8    | 1    | 0          |
| **TOTAL** | **65** | **1** | **1**    |

## Finding FLAGLEAK-001 (crypto — the ONE real finding)

**Priority:** High · **Complexity:** 3 (2–4) · **Tags:** ++security, +crypto

**Summary:** Raw flag material persists at rest in run-only event-log events and in graph/replay database entity payloads.

**Evidence (from `e2e-output/forensic_analysis.json`):**

- Files in the state dir containing the raw flag (4): `actions.jsonl`, `artifacts/<sha256-content-addressed-file>` (the artifact content file), `graph.db`, `replay.db`.
- Event-log event types carrying the raw flag (4): `flags.candidate_found`, `graph.entity_created`, `submission.accepted`, `submission.attempted`. The other 9 event types (bootstrap*, executor.action_attempted, graph.edge_created, termination) do NOT carry it.
- Graph entities containing the flag (3): `obs-1` (observation), `flag-<sha256>` (flag_candidate), `submission-1` (submission).
- Artifact INDEX records do NOT contain the flag (only the artifact content file does). No flag in any halctl stdout/stderr doc (status/scoreboard/challenge show/error/privilege error).

**Root cause / required change:** Only `graph.entity_created` is replay-required (the event-log replay hash depends on it). The run-only events `flags.candidate_found`, `submission.accepted`, `submission.attempted` are NOT needed for replay — their payloads should REDACT or HASH flag material (e.g. store `[FLAG:<n chars>]` or a digest) before persistence, so the raw flag never lands in `actions.jsonl` / `graph.db` / `replay.db` at rest.

## UNTESTABLE — negative/nul_byte_flag

A flag containing a NUL byte (`\x00`) cannot be expressed through the halctl CLI contract: `execve` forbids NUL in argv at the OS level, and Python's subprocess raises `ValueError` on NUL in args. There is no CLI path to test — the guard is OS-level, not application-level. Documented as UNTESTABLE by design.

## PASS summary — what was verified end-to-end

- **F2B write path (10):** challenge bootstrap → probe → free hint → executor turn (exactly ONE bounded action per turn, fingerprinted) → tool-plane shell execution → observation parse/persist → content-addressed artifact (sha256, dedupe, provenance source_action) → flag candidate (provenance-gated) → kernel submission accepted (+100 points) → router DONE.
- **B2F read path (5):** event-log replay hash byte-for-byte equal to live graph hash; `halctl status`, `halctl scoreboard`, `halctl challenge show` all read correctly; artifact index record shape validated.
- **Negative (34 PASS):** flag length boundaries (0/1/255/256/1000/65535), hint `--index` type abuse (0, negative, float-string, word, huge), unicode flags (emoji, RTL, NFC/NFD combining, zero-width), injection-adjacent (SQL, shell subst/backtick/pipe, path traversal, CRLF), CLI contract violations (unknown subcommand/flag, missing args), privilege gates (submit/paid-hint/exit without privilege, free hint with 0 allowed), no graph corruption after error paths.
- **Wiring (3):** halctl → hal_client → MCP JSON-RPC method roundtrip; params types preserved; privilege gate fires BEFORE the wire call.
- **Audit (5):** event-log completeness (21 events, none missing), termination last, bootstrap ordering before executor, action-before-observation ordering, evidence edge mirrored.
- **Crypto (8 PASS of 9):** no flag in any halctl output doc, hal_failure events never carry the flag, provenance edges enforced, privileged env var not echoed, model API key not persisted anywhere; flag-leak sweep found only the 4 locations listed in FLAGLEAK-001.

## Artifacts

- `e2e-output/raw_results.json` — per-check PASS/FAIL/UNTESTABLE with details.
- `e2e-output/forensic_analysis.json` — flag-material locations, event-type classification, graph entity list, state-dir sweep.
