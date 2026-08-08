# OzzGraph — Task Board

> Foreman: deepseek-v4-flash @ openrouter | DuckBrain: ozzgraph

Autonomous CTF agent harness. Spec-complete (see `docs/` and `AGENTS.md`).
Implementation follows the 32-PR sequence in `docs/IMPLEMENTATION_PLAN.md`. One
PR = one board task. Decompose each PR with `coding-hermes-model-router` before
spawning a worker. Follow AGENTS.md invariants + PR scope rules; bridge every
commit to a `gitreins task complete` so the Tier 2 judge evaluates real code.

## Active

> E2E-001 tick ran 2026-08-07: 65P/1F/1U, finding FLAGLEAK-001 fixed (a667733, judge PASS) — see Completed.

**Tick 2026-08-07: DOCS-000 done — documentation gate PASS (judge 1913c392).**
CONTRIBUTING.md (149 lines: toolchain, gates, PR workflow) + SECURITY.md (187
lines: vuln reporting, agent-isolated security model, flag hashing FLAGLEAK-001,
container hardening) created; README.md refreshed to launchpad bar (badges, ASCII
diagram, nav tables). Repo metadata set (description + 6 topics via gh repo
edit). Content landed in 06c5ab1 (concurrent repo-owner commit absorbed staged
docs; worker marker commit d3d74cf) — guards PASS, judge PASS 1913c392, Tier-1
lint/tests/secrets green. Worktree clean. Docs gate satisfied → idle classification.

## v2 Active — General Autonomous Security-Research Harness (per docs/CHANGES_v2.md)

> v2 milestone: pivot from "HalCTF agent" to "general vuln-research harness with
> HalCTF as one adapter." Vertical-first. Read docs/CHANGES_v2.md before
> starting. Phases V01-V10; work strictly in order (FIFO), each a judged commit.

> ✅ V01 DONE (2026-08-08): committed da1aaaf (26 files, 2968 insertions:
> environments/ package, ADR-0008, kernel rewrites, AutonomousRunner, tests).
> Judge PASS d1416f4 (5/5 criteria, 911 tests).
>
> ✅ V02 DONE (2026-08-08): committed db18787 (6 files, 923 insertions:
> `ozzgraph run` CLI + console script, findings.py Finding/FindingStore,
> runner evidence→hypothesis→Finding on CONFIRMED verdict, supervisor
> Evaluator wiring, tests/test_e2e_run.py process-level E2E — real subprocess
> against lab hidden-routes target + stub OpenAI endpoint; exit 0,
> TERMINATION: completed, graph chain + findings.json, exit-1/exit-3 mapping).
> Full gate green (ruff/format/mypy strict, 915 tests). Judge PASS 6c6a117a
> (3/3 criteria). Next: V03 (tool-runtime). Tick cap 3600s.
>
> ✅ V03 DONE (2026-08-08): committed c3c44f9 (10 files, 1583 insertions:
> src/ozzgraph/toolplane.py ToolCatalog/ToolInventory/CapabilityRegistry/
> ToolProvider, startup inventory in AutonomousRunner, capabilities-driven
> context (AVAILABLE CAPABILITIES block), Skill.required_capabilities +
> list_available, docker/Dockerfile.kali `:max` Kali image, docs +
> test_toolplane.py 26 tests + wiring/image tests). Work was completed by the
> prior tick's worker but left uncommitted (timeout) — this tick verified
> gates green (ruff/format/mypy strict, 941 tests) and committed, no redo.
> Judge PASS 3f4640ae (5/5 criteria). Next: V04 (semantic-observations).
>
> ✅ V04 DONE (2026-08-08): committed 8e49dd0 (observations.py 26KB→~140KB
> expansion: 17 tool-specific typed parsers — curl/nmap/ffuf/feroxbuster/
> nuclei/netexec/smbmap/ldapsearch/semgrep(JSON+SARIF)/codeql(SARIF)/trivy/
> gitleaks/file/readelf/checksec/exiftool(JSON+text)/binwalk — consuming
> JSON/XML/SARIF/JSONL/LDIF via registry keys (source,kind), parser_for_command
> dispatch gated by tool flags, runner `_persist_execution` wired through
> observation_for_result: raw output persisted to ArtifactStore FIRST, then
> semantic parse → typed Observation referencing artifact id → observation
> entity + evidence edge; malformed output still stored raw with
> malformed=True observation (fail loudly). docs/OBSERVATIONS.md added,
> toolplane.py capability entries for parseable tools, 56 new tests.
> Gates green (ruff/format/mypy strict, 997 tests). Judge PASS 01aa9dd
> (3/3 criteria, tier1 lint/tests/secrets PASS). Next: V05 (model-harness-matrix).

> ✅ V05 DONE (2026-08-08): committed 70d6f3f (9 files, 1040 insertions:
> profiles.py registry is now data, not code — per-model TOML profiles
> under profile_data/ (claude/deepseek/gpt/llama/fallback) loaded
> deterministically; ModelProfile gains model_ids + benchmarks
> (TraceMetrics); ProfileStore (discover exact-id→family-prefix→fallback,
> discover_from_service via GET /v1/models + capability probe — function
> call never assumed, update_benchmarks/persist_report byte-deterministic);
> tests/test_profile_store.py 18 tests. Work was completed by the prior
> tick's worker but left uncommitted (timeout) — this tick verified gates
> green (ruff/format/mypy strict, 1015 tests) and committed, no redo.
> Judge PASS b8a2cfd1 (5/5 criteria, tier1 lint/tests/secrets PASS).
> Also hardened .gitreins/config.yaml: test_command now `uv run --group
> dev pytest` + test_timeout 300 (pipeline's 120s default killed the
> 115s suite — 2 spurious FAIL verdicts traced to SIGKILL at 120s and
> dev-group-less resync). Next: V06 (security-brain).
>
> ✅ V06 DONE (2026-08-08): committed c948e93 (4 files, 2216 insertions:
> src/ozzgraph/security_brain.py 1129 lines — OpportunityGenerator
> (graph-predicate opportunities: characterize service / test hypothesis /
> expand scope, ranked, fail-loud), StrategicPlanner (LLM, invoked ONLY
> when >1 viable path; strategy prompt + ranking summary), TaskBuilder
> (bounded Task from opportunity/plan, command parity with runner),
> HypothesisManager (create→evidence→promote/abandon lifecycle,
> idempotent, event-mirrored), ProgressEvaluator (continue/pivot/finish
> from graph predicates); runner.py wired: _one_turn branches on typed
> BrainDecision — DeterministicActionDecision executes with ZERO model
> calls, StrategicDecision calls StrategicPlanner exactly once,
> FallbackDecision preserves the old model-propose path for 0-opportunity
> states; BRAIN_PROGRESS_EVALUATED event; docs/CHANGES_v2.md updated.
> 14 new tests (zero-LLM assertion, multi-path promote, hypothesis
> lifecycle, progress verdicts). Gates green (ruff/format/mypy strict,
> 1029 tests). Judge PASS ea396688 (4/4 criteria, tier1 lint/tests/
> secrets PASS). Next: V07 (specialists).
>
> ✅ V07 DONE (2026-08-08): committed c3872ce→e953e00 (4 commits, 2801
> insertions: workers.py SpecialistMicroAgent + MicroAgentTask — bounded
> deterministic hypothesis→experiment→observation→conclusion loop,
> MAX_MICRO_ITERATIONS=3, ZERO model calls, only context is hypothesis
> objective + prior observations (never full graph); scheduler.py
> parallelizes independent hypotheses (hypothesis id IS the conflict key;
> ready_order drives batch under max_workers) while global strategy stays
> serialized via serialized_task + reserved MUTATION_CONFLICT_KEY;
> reducer.py merges structured verdicts — Verdict + evidence_ids + impact
> (CWE/assets/confidence) live in fact payload + fingerprint;
> specialists.py SpecialistFleet (narrow task build → bounded parallel
> schedule → reducer → promote confirmed/abandon refuted → evidence-backed
> findings + findings.json); runner.py _run_specialist_batch_turn
> dispatches a fleet batch instead of an LLM call when brain returns a
> pure independent-hypothesis StrategicDecision and a fleet is wired
> (specialists=); docs/CHANGES_v2.md + ADR-0009. Work was completed by
> the prior tick's worker and committed but left unpushed/unjudged — this
> tick verified gates green (ruff/format/mypy strict, 1070 tests), pushed,
> judged PASS 8b6c8e3 (4/4 criteria, tier1 lint/tests/secrets PASS).
> Next: V08 (local-assessment).
>
> ✅ V08 DONE (2026-08-08): committed 1673b97→14eb864→198ba36 (16 files,
> 2261 insertions: reporting.py render_report_bundle — report.md +
> report.json + report.sarif from graph findings in state_dir alongside
> evidence/ + graph.sqlite (online-backup snapshot) + events.jsonl, runner
> emits bundle at COMPLETED with loud report_failed; LocalEnvironment
> url/network/repository/docker-compose/hybrid modes via OZZGRAPH_TARGET
> classification (classify_local_target, LOCAL_MODE_NAMES, scope_mode
> hybrid, LocalEnvironment now the DEFAULT via supervisor._make_environment);
> config.py OZZGRAPH_SCOPE_FILE/_load_scope_entries (JSON/YAML/TOML,
> sorted+deduped → target_allowlist) + OZZGRAPH_CREDENTIALS_FILE/
> _load_credential_records (Credential refs sorted by name) with loud
> ConfigError on unreadable/malformed/wrong-shape; ADR-0010, docs/
> CHANGES_v2.md + USAGE env vars; tests test_reporting.py 458 lines +
> test_config.py 219 + test_environments.py 149 + test_runner.py 57 + e2e
> additions). Work completed + pushed by prior worker, unjudged — this tick
> verified gates green (ruff/format/mypy strict, 1114 tests) and confirmed
> judge PASS 6025990f (4/4 criteria, tier1 lint/tests/secrets PASS).
> Next: V09 (halctf-adapter).

| ID | Task | Pri | Cpx | Deps | Tags | Model | Reasoning | Fallback |
|----|------|-----|-----|------|------|-------|-----------|----------|
| V09 | halctf-adapter: HAL_* / OPENAI_BASE_URL / MCP_ENDPOINT discovery, official tool set (list_ctfs/challenges/status/submit_flag/request_hint/scoreboard), smoke flag, scoring, hint costs, graceful completion; move hint-policy/submission/scoreboard/flag-candidate-extractor OUT of generic kernel into ozzgraph.environments.halctf | High | 4±1 | V01,V02 | +++python, ++integration, ++ctf | DS-V4-Flash | Medium | Kimi-K3 |
| V10 | full-regression: real benchmark suite across model matrix (web/api/source/network/ad/pwn/reverse/forensics/stego/cloud/halctf) incl. deliberate dead ends + tool-contract test (every skill's required capability has a working installed provider); prove OzzGraph+model beats plain ReAct | High | 5±1 | V03-V09 | +++python, ++testing, ++benchmark | DS-V4-Pro | High | DS-V4-Flash |

## Completed

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| V08 | local-assessment: URL/network/repository/Docker-Compose/hybrid modes via OZZGRAPH_TARGET classification, scope + credentials files (target_allowlist + Credential list, loud ConfigError), report bundle (report.md/json/sarif + evidence/ + graph.sqlite + events.jsonl) at COMPLETED, LocalEnvironment as DEFAULT, ADR-0010 (judge PASS 6025990f, all 4 criteria) | High | 5±1 | 198ba36 | DS-V4-Flash |
| V06 | security-brain: OpportunityGenerator + StrategicPlanner (LLM only when >1 viable path) + TaskBuilder + HypothesisManager + ProgressEvaluator, deterministic zero-LLM single-action path wired into runner (judge PASS ea396688, all 4 criteria) | Critical | 5±1 | c948e93 | DS-V4-Flash |
| V07 | specialists: SpecialistMicroAgent bounded hypothesis→experiment→observation→conclusion loop (MAX_MICRO_ITERATIONS=3, zero model calls, no full-graph context), Scheduler parallel hypotheses (hypothesis-id conflict keys) + serialized global strategy (MUTATION_CONFLICT_KEY), Reducer structured verdict merge (verdict+evidence_ids+impact CWE/assets/confidence), SpecialistFleet batch + runner dispatch, ADR-0009 (judge PASS 8b6c8e3, all 4 criteria) | High | 4±1 | e953e00 | DS-V4-Flash |
| V05 | model-harness-matrix: empirical per-model profiles — TOML-backed data-driven registry (profile_data/), ProfileStore discover/discover_from_service (GET /v1/models + capability probe), byte-deterministic TraceMetrics benchmark persistence (judge PASS b8a2cfd1, all 5 criteria) | High | 4±1 | 70d6f3f | DS-V4-Flash |
| V04 | semantic-observations: typed parsers/projectors for 17 high-value tools (JSON/XML/SARIF/JSONL), raw-first ArtifactStore persistence, runner observation wiring (judge PASS 01aa9dd, all 3 criteria) | Critical | 5±1 | 8e49dd0 | DS-V4-Flash |
| V03 | tool-runtime: ToolCatalog/ToolInventory/CapabilityRegistry/ToolProvider, startup tool inventory, capabilities-not-binaries, `:max` Kali image (judge PASS 3f4640ae, all 5 criteria) | Critical | 5±1 | c3c44f9 | DS-V4-Flash |
| V02 | autonomous-vertical-slice: `ozzgraph run <target>` end-to-end as a real process (CLI + console script, Finding model/store, evidence→hypothesis→Finding, evaluator wiring, process-level E2E test) (judge PASS 6c6a117a, all 3 criteria) | Critical | 5±1 | db18787 | DS-V4-Flash |
| V01 | generic-runtime: EnvironmentAdapter protocol, Scope/Target/Objective, Local/HalCTF environments, kernel rewrites, real AutonomousRunner (judge PASS d1416f4, all 5 criteria) | Critical | 5±1 | da1aaaf | DS-V4-Flash |
| FLAGLEAK-001 | Redact/hash flag material in run-only event-log events (flags.candidate_found/submission.attempted/submission.accepted/submission.rejected now carry flag_sha256+flag_length digests; graph.entity_created keeps raw flag — replay-required) (judge PASS, all 4 criteria) | High | 3±1 | a667733 | DS-V4-Flash |
| DOCS-000 | Documentation gate pass — CONTRIBUTING.md + SECURITY.md + README refresh + repo metadata (judge PASS 1913c392, all 3 criteria) | High | 3±1 | 06c5ab1 | DS-V4-Flash |
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
