# E2E-001 — F2B/B2F End-to-End Cycle (ozzgraph)

## Run #3 summary (2026-08-14)

- **Date:** 2026-08-14 00:09 UTC (2026-08-13 17:09 PDT) — fresh run against the
  current kernel. Previous runs: #1 2026-08-07 (65 PASS / 1 FAIL / 1
  UNTESTABLE), #2 2026-08-10 (66 PASS / 0 FAIL / 1 UNTESTABLE).
- **Driver:** `.coding-hermes/tests/scripts/e2e_001_driver.py` — runs the REAL
  ozzgraph kernel end-to-end against `tests/mcp_fake.py` FakeMcpServer (HalCTF
  platform side) + `ozzgraph.lab` "hidden-routes" target (challenge side). No
  production code touched; flag material redacted in all output.
- **Forensic:** `.coding-hermes/tests/scripts/e2e_001_forensic.py` — classifies
  every store/event carrying the test flag (event log per event_type, graph.db
  per entity, artifact index vs content, replay db).
- **Totals:** 66 PASS / 0 FAIL / 1 UNTESTABLE (67 checks) — identical to run
  #2; FLAGLEAK-001 remains fixed; no new gaps, no driver changes needed
  (kernel contract unchanged since run #2).
- **Raw data:** `e2e-output/raw_results.json`, `e2e-output/forensic_analysis.json`
  (regenerated 2026-08-14).

### Counts per dimension

| Dimension | PASS | FAIL | UNTESTABLE |
|-----------|------|------|------------|
| f2b       | 10   | 0    | 0          |
| b2f       | 5    | 0    | 0          |
| negative  | 34   | 0    | 1          |
| wiring    | 3    | 0    | 0          |
| audit     | 5    | 0    | 0          |
| crypto    | 9    | 0    | 0          |
| **TOTAL** | **66** | **0** | **1**    |

### Forensic sweep result

- `run_events_with_flag_not_replay_required`: **[]** ✓ — no run-only event
  (`flags.candidate_found`, `submission.attempted`, `submission.accepted`)
  carries the raw flag; all carry `flag_sha256` + `flag_length` digests.
- Raw-flag locations (all replay-required): `actions.jsonl` —
  `graph.entity_created` events only; the content-addressed artifact content
  file; `graph.db` (binary sqlite entity payloads). Artifact INDEX records
  carry no flag.
- `event_types_containing_raw_flag`: `["graph.entity_created"]` only
  (same as run #2).

## No new gaps this run (run #3)

The fresh 2026-08-14 run (driver + forensic) revealed **no new findings**:
0 FAIL across all six exercised dimensions; the only UNTESTABLE remains the
NUL-byte flag (by design — `execve` forbids NUL in argv, no CLI path to
test). Driver assertions needed no updates (kernel contract did not drift
since run #2). No new task added to `.coding-hermes/tasks.md`.

---

## Run #2 summary (2026-08-10)

- **Date:** 2026-08-10 — fresh run 09:28 UTC-7 against the v2 kernel (V01–V10 + HAL-001..HAL-011: environments/, halctf adapter, flag loop). Previous run: 2026-08-07 (pre-v2 kernel, 65 PASS / 1 FAIL / 1 UNTESTABLE).
- **Driver:** `.coding-hermes/tests/scripts/e2e_001_driver.py` — runs the REAL ozzgraph kernel end-to-end against `tests/mcp_fake.py` FakeMcpServer (HalCTF platform side) + `ozzgraph.lab` "hidden-routes" target (challenge side). The crypto `flag_leak_sweep_state_dir` check now asserts the POST-FLAGLEAK-001 contract (raw flag allowed ONLY in replay-required locations; run-only events must carry digests).
- **Forensic:** `.coding-hermes/tests/scripts/e2e_001_forensic.py` — post-run sweep of state dir, event log, and graph/replay DBs for flag material and provenance shape.
- **Totals:** 66 PASS / 0 FAIL / 1 UNTESTABLE (67 checks) — FLAGLEAK-001 verified fixed; no new gaps in this run.
- **Raw data:** `e2e-output/raw_results.json`, `e2e-output/forensic_analysis.json` (regenerated 2026-08-10).

### Counts per dimension

| Dimension | PASS | FAIL | UNTESTABLE |
|-----------|------|------|------------|
| f2b       | 10   | 0    | 0          |
| b2f       | 5    | 0    | 0          |
| negative  | 34   | 0    | 1          |
| wiring    | 3    | 0    | 0          |
| audit     | 5    | 0    | 0          |
| crypto    | 9    | 0    | 0          |
| **TOTAL** | **66** | **0** | **1**    |

## FLAGLEAK-001 — RESOLVED and verified fixed (2026-08-10)

**Priority:** High (was) · **Tags:** ++security, +crypto

**What it was:** Raw flag material persisted at rest in run-only event-log
events (`flags.candidate_found`, `submission.accepted`,
`submission.attempted`) — events NOT needed for replay — plus graph.db /
replay.db entity payloads.

**Fix:** Commit a667733 — run-only events now carry `flag_sha256` +
`flag_length` digests, never the raw flag. The raw flag is INTENTIONALLY
retained ONLY in replay-required locations: (1) the `graph.entity_created`
event in `actions.jsonl`, (2) the content-addressed artifact content file,
(3) `graph.db` entity payloads, (4) `replay.db`.

**Verification (fresh 2026-08-10 run):**
- Driver sweep check `flag_leak_sweep_state_dir`: **PASS** — raw-flag leaks
  outside replay-required set: none; run-only events
  `flags.candidate_found` / `submission.attempted` / `submission.accepted`
  carry `flag_sha256`+`flag_length` digests, no raw flag.
- Forensic `event_types_containing_raw_flag`: `["graph.entity_created"]` only
  (was 4 event types).
- Forensic `run_events_with_flag_not_replay_required`: `[]`.
- Raw-flag files in state dir: `actions.jsonl` (graph.entity_created events
  only), the artifact content file, `graph.db` (binary sqlite entity
  payloads) — all replay-required. Artifact INDEX records carry no flag.
- **Run #3 (2026-08-14) re-verified:** same results — sweep check PASS,
  forensic `run_events_with_flag_not_replay_required`: `[]`, raw flag only
  in `graph.entity_created` events + artifact content file + `graph.db`.

## No new gaps this run

The fresh 2026-08-10 run (driver + forensic) revealed **no new findings**:
0 FAIL across all six exercised dimensions; the only UNTESTABLE remains the
NUL-byte flag (below), which is by design. No new task added to
`.coding-hermes/tasks.md`.

## UNTESTABLE — negative/nul_byte_flag

A flag containing a NUL byte (`\x00`) cannot be expressed through the halctl CLI contract: `execve` forbids NUL in argv at the OS level, and Python's subprocess raises `ValueError` on NUL in args. There is no CLI path to test — the guard is OS-level, not application-level. Documented as UNTESTABLE by design.

## PASS summary — what was verified end-to-end

- **F2B write path (10):** challenge bootstrap → probe → free hint → executor turn (exactly ONE bounded action per turn, fingerprinted) → tool-plane shell execution → observation parse/persist → content-addressed artifact (sha256, dedupe, provenance source_action) → flag candidate (provenance-gated) → kernel submission accepted (+100 points) → router DONE.
- **B2F read path (5):** event-log replay hash byte-for-byte equal to live graph hash; `halctl status`, `halctl scoreboard`, `halctl challenge show` all read correctly; artifact index record shape validated.
- **Negative (34 PASS):** flag length boundaries (0/1/255/256/1000/65535), hint `--index` type abuse (0, negative, float-string, word, huge), unicode flags (emoji, RTL, NFC/NFD combining, zero-width), injection-adjacent (SQL, shell subst/backtick/pipe, path traversal, CRLF), CLI contract violations (unknown subcommand/flag, missing args), privilege gates (submit/paid-hint/exit without privilege, free hint with 0 allowed), no graph corruption after error paths.
- **Wiring (3):** halctl → hal_client → MCP JSON-RPC method roundtrip; params types preserved; privilege gate fires BEFORE the wire call.
- **Audit (5):** event-log completeness (21 events, none missing), termination last, bootstrap ordering before executor, action-before-observation ordering, evidence edge mirrored.
- **Crypto (9 PASS):** flag-leak sweep asserts post-FLAGLEAK-001 contract (raw flag only in replay-required set; run-only events carry digests), no flag in any halctl output doc, hal_failure events never carry the flag, provenance edges enforced, privileged env var not echoed, model API key not persisted anywhere.

## Artifacts

- `e2e-output/raw_results.json` — per-check PASS/FAIL/UNTESTABLE with details (regenerated 2026-08-14, run #3).
- `e2e-output/forensic_analysis.json` — flag-material locations, event-type classification, graph entity list, state-dir sweep (regenerated 2026-08-14, run #3).
