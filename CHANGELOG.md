# Changelog

All notable changes to OzzGraph are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-08-08

### Major: OzzGraph is now a general autonomous security-research harness

The v2 milestone (V01–V10) reframed OzzGraph from a HalCTF-specific agent into
a **vulnerability-research system with HalCTF as one adapter**. The kernel,
state graph, replay, provenance, and artifact store are unchanged; everything
above them was rebuilt around a vertical-slice autonomous runner and an
environment-adapter abstraction. See [docs/CHANGES_v2.md](docs/CHANGES_v2.md).

#### Added — autonomous runner & vertical slice (V02)

- `ozzgraph run <target>` — a real end-to-end process: CLI entry point +
  console script, `Finding`/`FindingStore`, and an evidence →
  hypothesis → `Finding` pipeline that executes the full
  discover → model → tool → observe → graph → validate → report loop.
- Process-level E2E test (`tests/test_e2e_run.py`) that runs the real
  subprocess against a lab target and a stub OpenAI endpoint — no test code
  manually drives the components.
- `TERMINATION: <reason>` exit mapping (`0` completed / `1` failed / `130`
  interrupted / `3` budget exhausted).

#### Added — semantic observations (V04)

- 17 tool-specific typed parsers (curl, nmap, ffuf/feroxbuster, nuclei,
  netexec, smbmap, ldapsearch, semgrep JSON+SARIF, CodeQL SARIF, trivy,
  gitleaks, file/readelf/checksec, exiftool JSON+text, binwalk) consuming
  JSON/XML/SARIF/JSONL/LDIF via a registry keyed by `(source, kind)`.
- **Raw-first persistence**: command output is stored in the `ArtifactStore`
  *before* parsing, then projected to a typed `Observation` that references
  the artifact id. Malformed output is still stored raw with `malformed=True`
  (fail loudly).
- Docs: [docs/OBSERVATIONS.md](docs/OBSERVATIONS.md).

#### Added — capability-driven tool plane (V03)

- `ToolCatalog` / `ToolInventory` / `CapabilityRegistry` / `ToolProvider`.
- Startup deterministically inventories installed tools; skills declare
  **capabilities**, and the model is shown only the capabilities actually
  available (never a nonexistent tool).
- `:max` Kali image (`docker/Dockerfile.kali`) for an essentially-unlimited
  security toolset.

#### Added — empirical model profiles (V05)

- Per-model TOML profiles under `profile_data/` (claude/deepseek/gpt/llama/
  fallback) loaded deterministically — profiles are now data, not code.
- `ModelProfile` gains `model_ids` + `benchmarks` (`TraceMetrics`).
- `ProfileStore` with exact-id → family-prefix → fallback discovery and
  `discover_from_service` via `GET /v1/models` + a capability probe.
- Byte-deterministic benchmark persistence.

#### Added — security brain (V06)

- Opportunity-driven planning: `OpportunityGenerator` + `StrategicPlanner`
  (LLM invoked only when there is more than one viable path), `TaskBuilder`,
  `HypothesisManager`, `ProgressEvaluator`.
- Deterministic zero-LLM single-action path wired into the runner.

#### Added — specialist micro-agents (V07)

- `SpecialistMicroAgent`: bounded hypothesis → experiment → observation →
  conclusion loop with a structured `Verdict`, zero model calls, no full-graph
  context.
- `SpecialistFleet` parallel hypotheses through the `Scheduler` (hypothesis-id
  conflict keys), serialized global mutation via `MUTATION_CONFLICT_KEY`, and
  a `Reducer` that merges structured verdicts (verdict + evidence ids + impact/
  CWE/assets/confidence).
- ADR-0009.

#### Added — local assessment (V08)

- Target modes: URL, network/CIDR, repository, Docker-Compose, and hybrid
  source + runtime via `OZZGRAPH_TARGET` classification; scope + credentials
  files (target allowlist + `Credential` list, loud `ConfigError`).
- Report bundle at `COMPLETED`: `report.md` / `report.json` / `report.sarif` +
  `evidence/` + `graph.sqlite` + `events.jsonl`.
- `LocalEnvironment` is now the default environment.
- ADR-0010.

#### Added — HalCTF adapter (V09)

- `HAL_*` / `OPENAI_BASE_URL` / `MCP_ENDPOINT` discovery and the official tool
  set (`list_ctfs`, `list_challenges`, `get_challenge`, `get_challenge_status`,
  `submit_flag`, `request_hint`, `get_scoreboard`, `get_score_breakdown`).
- Smoke flag, competition scoring, hint costs, attempt limits, graceful
  completion.
- Hint policy, submission coordinator, scoreboard, and flag-candidate
  extraction moved **out of the generic kernel** into
  `ozzgraph.environments.halctf` (ADR-0011).

#### Added — full-regression benchmark suite (V10)

- `benchmarks/` package: registry + OzzGraph harness vs plain ReAct + a
  scripted model + scoring + deterministic report.
- Dead-end lab target with pivot proof (hypothesis abandonment + PIVOT,
  bounded turns).
- Tool-contract test: every required capability resolves to an installed
  provider.
- Benchmark CLI (`--target`/`--react`/`--max-turns`/`--out` +
  `OZZGRAPH_BENCHMARK_*` env).
- Docs: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

#### Added — documentation pass (DOCS-000)

- README refreshed for the v2 milestone; docs index + repo metadata updated.

### CI / tooling

- Publish competition image to **GHCR** (always) + **Docker Hub** (when the
  `DOCKERHUB_PUBLISH` repo variable is set) on `main` / version tags / manual
  dispatch.
- Bumped GitHub Actions to current majors (checkout v7, build-push v7, login
  v4, setup-buildx v4) — clears the Node 20 deprecation.

### Fixed

- `fix(e2e)`: E2E driver imports the V09-moved flag modules from their
  canonical homes (`ozzgraph.entities` / `ozzgraph.environments.halctf`).

## [1.0.0] — 2026-08-07

### Added

- Initial OzzGraph release: deterministic model-adaptive CTF harness kernel
  with SQLite graph state, append-only JSONL events, content-addressed artifact
  store, replay, provenance, bounded shell execution, lazy skills, model
  adapters, planner–executor–evaluator loop, policy/tool-plane safety
  boundaries, parallel worker scheduling, and an optional read-only TypeScript
  dashboard. All 32 PRs of the implementation plan merged (19/19 DoD rehearsal).
