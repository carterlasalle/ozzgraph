# OzzGraph — Task Board

> Foreman: deepseek-v4-flash @ openrouter | DuckBrain: ozzgraph

Autonomous CTF agent harness. Spec-complete (see `docs/` and `AGENTS.md`).
Implementation follows the 32-PR sequence in `docs/IMPLEMENTATION_PLAN.md`. One
PR = one board task. Decompose each PR with `coding-hermes-model-router` before
spawning a worker. Follow AGENTS.md invariants + PR scope rules; bridge every
commit to a `gitreins task complete` so the Tier 2 judge evaluates real code.

## Active

| ID | Task | Pri | Cpx | Deps | Tags | Model | Reasoning | Fallback |
|----|------|-----|-----|------|------|-------|-----------|----------|
| T05 | PR5: OpenAI-compatible model client (list/complete, retry, timeout, token usage) | High | 4±1 | T02 | +++python, +++httpx, ++async, -vision | GLM-5.2 | High | DS-V4-Pro |
| T06 | PR6: MCP client + `halctl` adapter (challenge/status/submit/hint/exit) | High | 5±1 | T02 | +++python, ++cli, ++integration, -vision | GLM-5.2 | High | DS-V4-Pro |
| T07 | PR7: SQLite state graph (entities, edges, schema_version, migrations) | Critical | 5±1 | T02 | +++python, +++aiosqlite, +++sqlite, -vision | GLM-5.2 | High | DS-V4-Pro |
| T08 | PR8: artifact store + JSONL event log + replay (graph hash) | Critical | 5±1 | T07 | +++python, +++sqlite, ++replay, -vision | GLM-5.2 | High | DS-V4-Pro |
| T09 | PR9: bounded shell runner (process-group timeout, truncation, ToolResult) | Critical | 5±1 | T04 | +++python, ++subprocess, ++security, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T10 | PR10: scope policy + duplicate detection (fingerprints, allowlists) | Critical | 5±1 | T09 | +++python, ++security, -vision | DS-V4-Flash | High | GLM-5.2 |
| T11 | PR11: observation parsers (normalized results) | High | 4±1 | T09 | +++python, ++parsing, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T12 | PR12: deterministic bootstrap (target parse, smoke flag, probe, free hint) | High | 4±1 | T07,T09 | +++python, ++integration, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T13 | PR13: model profile + adapter interfaces | High | 4±1 | T05 | +++python, ++pydantic, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T14 | PR14: terminal-native + three-line adapters | High | 4±1 | T13 | +++python, ++parsing, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T15 | PR15: JSON adapter + repair strategy | High | 4±1 | T13 | +++python, ++parsing, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T16 | PR16: context compiler (bounded subgraph view, layers) | Critical | 5±1 | T07,T13 | +++python, ++graph, -vision | GLM-5.2 | High | DS-V4-Pro |
| T17 | PR17: skill registry + lazy loading + initial skill packs | High | 4±1 | T16 | +++python, ++yaml, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T18 | PR18: graph-driven phase router (predicates, not counts) | Critical | 5±1 | T16 | +++python, ++graph, -vision | GLM-5.2 | High | DS-V4-Pro |
| T19 | PR19: planner + schemas (hypotheses, bounded plan, abandon conditions) | High | 5±1 | T18 | +++python, ++pydantic, ++graph, -vision | GLM-5.2 | High | DS-V4-Pro |
| T20 | PR20: executor loop (one bounded action/turn, strict output contract) | Critical | 5±1 | T14,T19 | +++python, ++async, -vision | GLM-5.2 | High | DS-V4-Pro |
| T21 | PR21: evaluator (deterministic + model fallback) + replanning | High | 5±1 | T19,T20 | +++python, ++graph, -vision | GLM-5.2 | High | DS-V4-Pro |
| T22 | PR22: flag provenance + supervisor-only submission | Critical | 4±1 | T20 | +++python, ++security, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T23 | PR23: hint policy (free auto, paid gated, supervisor-only) | High | 4±1 | T20 | +++python, ++policy, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T24 | PR24: task DAG + scheduler (conflict keys, bounded parallel) | High | 5±1 | T18 | +++python, ++graph, ++async, -vision | GLM-5.2 | High | DS-V4-Pro |
| T25 | PR25: specialist workers (scope-limited) | High | 5±1 | T24 | +++python, ++async, -vision | GLM-5.2 | High | DS-V4-Pro |
| T26 | PR26: reducer + conflict handling (validated findings merge) | Critical | 5±1 | T25 | +++python, ++graph, -vision | GLM-5.2 | High | DS-V4-Pro |
| T27 | PR27: synthetic test lab (isolated targets) | High | 5±1 | T09 | +++python, ++testing, ++containers, -vision | GLM-5.2 | High | DS-V4-Pro |
| T28 | PR28: golden traces + model–harness matrix | High | 5±1 | T27 | +++python, ++testing, ++replay, -vision | GLM-5.2 | High | DS-V4-Pro |
| T29 | PR29: chaos + adversarial tests (loops, timeouts, malformed, injection) | High | 5±1 | T27 | +++python, ++testing, ++security, -vision | GLM-5.2 | High | DS-V4-Pro |
| T30 | PR30: optional Yarn dashboard (OUTSIDE image) | Medium | 5±1 | T28 | +++typescript, ++yarn, -python, -vision | GLM-5.2 | High | DS-V4-Pro |
| T31 | PR31: image hardening (minimize, SBOM, immutable, <1.5GB) | High | 5±1 | T28 | +++docker, ++devops, -vision | DS-V4-Flash | Medium | GLM-5.2 |
| T32 | PR32: v1.0 release candidate (full DoD, rehearsal, image quality gates) | Critical | 5±1 | T31 | +++devops, ++release, -vision | GLM-5.2 | High | DS-V4-Pro |

## Completed

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| T04 | PR4: structured event logging | High | 3±1 | 0948763 | DS-V4-Flash |
| T00 | Bootstrap init — repo, spec docs, AGENTS.md, uv scaffold | Trivial | 1±0 | — | DS-V4-Flash |
| T03 | PR3: heartbeat, budgets, lifecycle (SIGTERM/SIGINT) | High | 4±1 | T02 | DS-V4-Flash | 41b16a4 |
| T01 | PR1: init uv project + CI skeleton (lint, format, type, test gates) | Critical | 2±1 | e4deda1 | DS-V4-Flash |
| T02 | PR2: runtime configuration + supervisor skeleton | Critical | 4±1 | 02711ad | DS-V4-Flash |

## [ ] NEVER-DONE — Run coding-hermes-never-done 12-point audit

Load coding-hermes-never-done skill. Run ALL 12 checks: spec alignment,
doc coverage, test gaps, package upgrades, pitfall hunt, performance audit,
endpoint verification, CI/CD health, DuckBrain sync, code quality,
middle-out wiring, usability smoke test. Create a task for EVERY gap found.
This task is never complete — the audit always finds something.
