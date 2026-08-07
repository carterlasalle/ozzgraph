# OzzGraph — Task Board

> Foreman: deepseek-v4-flash @ openrouter | DuckBrain: ozzgraph

Autonomous CTF agent harness. Spec-complete (see `docs/` and `AGENTS.md`).
Implementation follows the 32-PR sequence in `docs/IMPLEMENTATION_PLAN.md`. One
PR = one board task. Decompose each PR with `coding-hermes-model-router` before
spawning a worker. Follow AGENTS.md invariants + PR scope rules; bridge every
commit to a `gitreins task complete` so the Tier 2 judge evaluates real code.

## Active

> E2E-001 tick ran 2026-08-07: 65P/1F/1U, finding FLAGLEAK-001 fixed (a667733, judge PASS) — see Completed.

**Idle tick #1 (2026-08-07) — NEVER-DONE 14-point audit: ALL PASS, zero gaps.**
Spec alignment OK (all 6 halctl subcommands + challenge show in code; docs match),
doc coverage OK (DOC-001 judge PASS), 880 tests pass, deps OK (only pydantic-core
2.48.0 BLOCKED by pydantic pin — known; ast-serialize/librt transitive), no
TODOs/stubs (NotImplementedError is caught), gitleaks allowlist narrowed, no
benchmarks (N/A for CLI harness), halctl clean-state CLI OK, CI green (latest 2
runs success; earlier E2E-board-commit failure resolved by 5560311), DuckBrain
synced (2 tick entries), gitignore complete, wiring OK ([project.scripts] halctl),
E2E-001 ran today, GitReins judge config correctly sized (60 iter/1M in).
Cooldown set 43200s. Counter: 1/7.

## Completed

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| FLAGLEAK-001 | Redact/hash flag material in run-only event-log events (flags.candidate_found/submission.attempted/submission.accepted/submission.rejected now carry flag_sha256+flag_length digests; graph.entity_created keeps raw flag — replay-required) (judge PASS, all 4 criteria) | High | 3±1 | a667733 | DS-V4-Flash |
| DOC-001 | Full documentation pass — polished README + docs/USAGE.md + docs/CUSTOMIZATION.md (judge PASS 8cee566d) | High | 3±1 | 2a0daa3 | DS-V4-Flash |
| DEPS-001 | ruff 0.16.1→0.16.2 (direct dev dep patch bump; pydantic-core 2.48.0 BLOCKED by pydantic==2.46.4 pin) | Low | 1±0 | a70f3f7 | DS-V4-Flash |

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| TEST-INFRA-001 | .coding-hermes/tests scaffold per coding-hermes-testing v1.0 (_index.md, test-state.toml, 5 prompts, 7 subdirs) | High | 3±1 | d58da21 | DS-V4-Flash |
| T32 | PR32: v1.0 release candidate (full DoD, rehearsal, image quality gates) | Critical | 5±1 | e10ca1d | DS-V4-Flash |
| T31 | PR31: image hardening (minimize, SBOM, immutable, <1.5GB) | High | 5±1 | ac7d3cb | DS-V4-Flash |
| T30 | PR30: optional Yarn dashboard (OUTSIDE image) | Medium | 5±1 | 0dc7098 | DS-V4-Flash |
| T29 | PR29: chaos + adversarial tests (loops, timeouts, malformed, injection) | High | 5±1 | cb3f46f | DS-V4-Flash |
| T28 | PR28: golden traces + model–harness matrix | High | 5±1 | 403e40a | DS-V4-Flash |
| T27 | PR27: synthetic test lab (isolated targets) | High | 5±1 | 6476a7f | DS-V4-Flash |
| T26 | PR26: reducer + conflict handling (validated findings merge) | Critical | 5±1 | e22b37b | DS-V4-Flash |
| T25 | PR25: specialist workers (scope-limited) | High | 5±1 | b4fd7da | DS-V4-Flash |
| T24 | PR24: task DAG + scheduler (conflict keys, bounded parallel) | High | 5±1 | 393a2ba | DS-V4-Flash |
| T23 | PR23: hint policy (free auto, paid gated, supervisor-only) | High | 4±1 | 72b0276 | DS-V4-Flash |
| T22 | PR22: flag provenance + supervisor-only submission | Critical | 4±1 | 40a62eb | DS-V4-Flash |
| T21 | PR21: evaluator (deterministic + model fallback) + replanning | High | 5±1 | 7666d01 | DS-V4-Flash |
| T20 | PR20: executor loop (one bounded action/turn, strict output contract) | Critical | 5±1 | 36f46a4 | DS-V4-Flash |
| T19 | PR19: planner + schemas (hypotheses, bounded plan, abandon conditions) | High | 5±1 | 7a7d16e | DS-V4-Flash |
| T18 | PR18: graph-driven phase router (predicates, not counts) | Critical | 5±1 | a9cf1cb | DS-V4-Flash |
| T17 | PR17: skill registry + lazy loading + initial skill packs | High | 4±1 | 2cc240a | DS-V4-Flash |
| T16 | PR16: context compiler (bounded subgraph view, layers) | Critical | 5±1 | 26b9c8e | DS-V4-Flash |
| T15 | PR15: JSON adapter + repair strategy | High | 4±1 | 3a07f79 | DS-V4-Flash |
| T14 | PR14: terminal-native + three-line adapters | High | 4±1 | 706cc7c | DS-V4-Flash |
| T13 | PR13: model profile + adapter interfaces | High | 4±1 | 282625b | DS-V4-Flash |
| T12 | PR12: deterministic bootstrap (target parse, smoke flag, probe, free hint) | High | 4±1 | 681f79f | DS-V4-Flash |
| T11 | PR11: observation parsers (normalized results) | High | 4±1 | d0f03df | DS-V4-Flash |
| T10 | PR10: scope policy + duplicate detection (fingerprints, allowlists) | Critical | 5±1 | 6589721 | DS-V4-Flash |
| T09 | PR9: bounded shell runner (process-group timeout, truncation, ToolResult) | Critical | 5±1 | 5745d2d | DS-V4-Flash |
| T08 | PR8: artifact store + JSONL event log + replay (graph hash) | Critical | 5±1 | 59a3cff | DS-V4-Flash |
| T07 | PR7: SQLite state graph (entities, edges, schema_version, migrations) | Critical | 5±1 | 32d9185 | DS-V4-Flash |
| T06 | PR6: MCP client + `halctl` adapter (challenge/status/submit/hint/exit) | High | 5±1 | 2af48d3 | DS-V4-Flash |
| T05 | PR5: OpenAI-compatible model client (list/complete, retry, timeout, token usage) | High | 4±1 | 60f4be6 | DS-V4-Flash |
| T04 | PR4: structured event logging | High | 3±1 | 0948763 | DS-V4-Flash |
| T00 | Bootstrap init — repo, spec docs, AGENTS.md, uv scaffold | Trivial | 1±0 | — | DS-V4-Flash |
| T03 | PR3: heartbeat, budgets, lifecycle (SIGTERM/SIGINT) | High | 4±1 | T02 | DS-V4-Flash | 41b16a4 |
| T01 | PR1: init uv project + CI skeleton (lint, format, type, test gates) | Critical | 2±1 | e4deda1 | DS-V4-Flash |
| T02 | PR2: runtime configuration + supervisor skeleton | Critical | 4±1 | 02711ad | DS-V4-Flash |

## [ ] E2E-001 — E2E Testing Tick (self-improving loop)

Spawn Luna (browser/screenshots) or Step 3.7 Flash (CLI/API). Deploy/build,
run the F2B→B2F cycle using .coding-hermes/tests/ scaffold (test-state.toml
dims: f2b/b2f/negative/visual/crypto/wiring/structure/audit — all currently 0).
halctl CLI journeys, artifact store, executor loop. → update test-state.toml →
inject findings into board. Every 5-10 ticks.

## [ ] NEVER-DONE — Run coding-hermes-never-done 12-point audit

Load coding-hermes-never-done skill. Run ALL 12 checks: spec alignment,
doc coverage, test gaps, package upgrades, pitfall hunt, performance audit,
endpoint verification, CI/CD health, DuckBrain sync, code quality,
middle-out wiring, usability smoke test. Create a task for EVERY gap found.
This task is never complete — the audit always finds something.
