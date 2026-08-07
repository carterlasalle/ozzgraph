# OzzGraph — Testing Manifest

Project: **ozzgraph** (v1.0.0) — Python 3.12 autonomous CTF agent harness
(terminal-native; halctl CLI entry point).
Infrastructure dir: `.coding-hermes/tests/` (scaffold created by TEST-INFRA-001).

## What Is Tested

- **879 pytest unit/integration tests** under `tests/`, covering kernel
  components in isolation: halctl CLI, state graph (aiosqlite), executor
  turn loop, planner, evaluator, artifact store, model client, flags /
  submissions, hints, bootstrap, events, budgets, policy, replay.
- **CI docker gate**: non-root-user + startup-evidence assertions.
- **Release DoD rehearsal**: docs/RELEASE.md maps every DoD item to
  evidence (19/19 PASS at v1.0.0).

## What Is NOT Tested

**Zero structured E2E testing infrastructure existed before TEST-INFRA-001.**
All eight testing dimensions (f2b, b2f, negative, visual, crypto, wiring,
structure, audit) have coverage **0** — the 879 tests are component-level
unit/integration only; no write-path, read-path, wire, render, or leak
verification has ever been run end-to-end. See `test-state.toml` for the
full untested-path and known-gap inventory.

## Coverage by Dimension

| Dimension | Coverage | Status | Report dir |
|-----------|----------|--------|------------|
| f2b (write paths) | 0 | ⚠️ untested | `f2b/` |
| b2f (read paths) | 0 | ⚠️ untested | `b2f/` |
| negative (boundary) | 0 | ⚠️ untested | `negative/` |
| visual (render) | 0 | ⚠️ untested | `b2f/render/` (see note) |
| crypto (secrets/leaks) | 0 | ⚠️ untested | `crypto/` |
| wiring (cross-module) | 0 | ⚠️ untested | `wiring/` |
| structure (schema/shape) | 0 | ⚠️ untested | `structure/` |
| audit (log/event trail) | 0 | ⚠️ untested | `audit/` |

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
