# OzzGraph — Testing Manifest

Project: **ozzgraph** (v1.0.0) — Python 3.12 autonomous CTF agent harness
(terminal-native; halctl CLI entry point).
Infrastructure dir: `.coding-hermes/tests/` (scaffold created by TEST-INFRA-001).

## What Is Tested

- **1306 pytest unit/integration tests** under `tests/`, covering kernel
  components in isolation: halctl CLI, state graph (aiosqlite), executor
  turn loop, planner, evaluator, artifact store, model client, flags /
  submissions, hints, bootstrap, events, budgets, policy, replay, workers.
- **E2E-001 driver** (`.coding-hermes/tests/scripts/e2e_001_driver.py`): runs
  the REAL kernel end-to-end (f2b/b2f/negative/crypto/wiring/audit) against
  `tests/mcp_fake.py` + `ozzgraph.lab` "hidden-routes"; 66 PASS / 0 FAIL /
  1 UNTESTABLE on 2026-08-10. Evidence: `e2e-output/raw_results.json`.
- **CI docker gate**: non-root-user + startup-evidence assertions.
- **Release DoD rehearsal**: docs/RELEASE.md maps every DoD item to
  evidence (19/19 PASS at v1.0.0).

## What Is NOT Tested

Component-level tests are unit/integration only. End-to-end coverage comes
from the E2E-001 driver suite (see `test-state.toml` for the untested-path
and known-gap inventory): `visual` and `structure` dimensions have coverage
0 — no render/browser or dedicated schema-shape checks exist yet.

## Coverage by Dimension

Counters reflect the latest E2E-001 driver run (2026-08-10, 66 PASS / 0 FAIL
/ 1 UNTESTABLE; see `e2e-output/raw_results.json`).

| Dimension | Coverage | Status | Report dir |
|-----------|----------|--------|------------|
| f2b (write paths) | 10 | ✅ e2e driver | `f2b/` |
| b2f (read paths) | 5 | ✅ e2e driver | `b2f/` |
| negative (boundary) | 34 | ✅ e2e driver | `negative/` |
| visual (render) | 0 | ⚠️ untested | `b2f/render/` (see note) |
| crypto (secrets/leaks) | 9 | ✅ e2e driver | `crypto/` |
| wiring (cross-module) | 3 | ✅ e2e driver | `wiring/` |
| structure (schema/shape) | 0 | ⚠️ untested | `structure/` |
| audit (log/event trail) | 5 | ✅ e2e driver | `audit/` |

**Note on `visual/`:** ozzgraph is terminal-native (CLI, single-JSON-document
stdout contract, TTY tables) — there is **no standalone `visual/` directory**.
Visual/render verification targets terminal output rendering (scoreboard
alignment, unicode/emoji, error visibility, ANSI handling) and its reports
are filed under `b2f/render/` per the coding-hermes-testing v1.0 convention.

## Directory Layout

```
.coding-hermes/tests/
├── _index.md          # this manifest
├── test-state.toml    # coverage state + untested paths + known gaps
├── f2b/               # front-to-back (write path) reports   [scaffold]
├── b2f/               # back-to-front (read path) reports    [scaffold]
├── negative/          # negative/boundary reports            [scaffold]
├── crypto/            # secrets/leak/provenance reports      [scaffold]
├── wiring/            # cross-module wire reports            [scaffold]
├── structure/         # schema/shape reports                 [scaffold]
├── audit/             # log/event trail reports              [scaffold]
└── prompts/           # LLM testing prompt templates
    ├── f2b-write.md
    ├── b2f-read.md
    ├── negative.md
    ├── visual.md
    └── crypto.md
```

## How to Run a Testing Tick

1. Pick an untested path from `test-state.toml`.
2. Load the matching prompt template from `prompts/`.
3. Run the F2B write-path cycle, then the B2F read-path cycle.
4. File the report in the corresponding subdir; update `test-state.toml`
   (bump the dimension counter, remove/resolve the path).
5. Never claim coverage that was not actually exercised — every counter
   here must reflect a real filed report.
