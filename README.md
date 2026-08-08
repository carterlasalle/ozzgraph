<div align="center">

# OzzGraph

**A model-adaptive autonomous security-research harness for authorized, isolated environments.**

[![CI](https://github.com/carterlasalle/ozzgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/carterlasalle/ozzgraph/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](https://docs.python.org/3.12/)
[![uv](https://img.shields.io/badge/uv-managed-blue)](https://docs.astral.sh/uv/)

[Quick start](#quick-start) · [Documentation](#documentation) · [Repository layout](#repository-layout) · [Development](#development-commands) · [Container image](#container-image) · [Safety model](#safety-model)

</div>

OzzGraph wraps a small deterministic kernel around a planner–executor–evaluator
loop so even modest models behave like disciplined security operators.

The core principle:

> The model supplies judgment when the next action is uncertain. The harness
> supplies memory, discipline, tools, safety boundaries, execution, evidence,
> and recovery.

```mermaid
flowchart TB
    subgraph Kernel[Supervisor Kernel]
        S[State & Work Graph<br/>SQLite + JSONL events + artifact store]
        P[Phase Router<br/>graph predicates, never action counts]
        PS[Planner / Scheduler<br/>bounded plans, task DAG, conflict keys]
        CC[Context Compiler] --> M[Model Adapter]
        M --> R[model: bounded judgment only]
        PO[Policy & Tool Plane<br/>allowlists, fingerprints, timeouts, limits]
        OA[Observation & Artifact Pipeline<br/>raw output stays outside context]
        ER[Evaluator & Reducer<br/>provenance-validated facts]
    end
    S --> P
    P --> PS
    P --> CC
    P --> PO
    P --> OA
    OA --> ER
    ER -->|loop| P
```

**Release status: v2.0.0 shipped.** OzzGraph is a **general autonomous
security-research harness** — scope → assets → observations → evidence →
hypotheses → validated findings → report — with HalCTF as **one optional
environment adapter** among several (local assessment, Docker Compose, git
repository, network scope, synthetic lab). v1.0.0 (the 32-PR implementation
plan, 19/19 DoD rehearsal) and the v2 milestone (V01–V10) are both released;
see the [v2.0.0 release](https://github.com/carterlasalle/ozzgraph/releases/tag/v2.0.0),
[CHANGELOG.md](CHANGELOG.md), [docs/CHANGES_v2.md](docs/CHANGES_v2.md) for the
V01–V10 milestone notes, and [docs/RELEASE.md](docs/RELEASE.md) for release ops.

## What the harness does

| Area | What OzzGraph provides |
| --- | --- |
| Security phases | Ozz-style BOOTSTRAP → RECON → ENUMERATION → EXPLOITATION → POST_EXPLOITATION → PIVOT → FLAG_HUNT → VERIFY_AND_SUBMIT → REPLAN → DONE, driven by **graph-state predicates, never action counts** |
| Authoritative state | SQLite state graph (`graph.db`), append-only JSONL event log (`actions.jsonl`), content-addressed artifact store — replaying the log reconstructs the identical graph hash |
| Bounded actions | One action per executor turn with a timeout, output limit, and normalized fingerprint; duplicates/failed fingerprints never retried |
| Environment adapters (v2) | Pluggable `EnvironmentAdapter` interface: `HalCTFEnvironment` (legacy CTF flow) and `LocalEnvironment` classifying targets as url / network / host / repository / docker-compose / hybrid — local is the default with no `HAL_*` config |
| Semantic observations (v2) | Typed parsers/projectors for nmap, ffuf, nuclei, netexec, semgrep, CodeQL, trivy, gitleaks, and more, with **raw-first** artifact persistence; see [docs/OBSERVATIONS.md](docs/OBSERVATIONS.md) |
| Findings model (v2) | CWE-classified findings (assets, preconditions, evidence ids, reproduction, impact CIA, confidence) rendered as `report.md` / `report.json` / `report.sarif` + an `evidence/` bundle |
| Security brain (v2) | OpportunityGenerator → StrategicPlanner → HypothesisManager → ProgressEvaluator; a single obvious action executes deterministically with **zero LLM calls** |
| Specialist fleets (v2) | Bounded deterministic micro-agents (hypothesis → experiment → observation → verdict), parallelized per-hypothesis and merged by the reducer — no model calls |
| Benchmark matrix (v2) | Full-regression suite across the synthetic lab matrix vs a plain-ReAct baseline, hermetic scripted-model runs by default with a real-model option; see [docs/BENCHMARKS.md](docs/BENCHMARKS.md) |
| Lazy skills | Compact per-phase summaries advertised to the model; full skill cards load only when selected |
| Model adapters | Three protocols — terminal-native free text, strict three-line, structured JSON — each with its own prompt compiler, parser, and deterministic repair |
| Planner–Executor–Evaluator | Bounded ranked plans only when the graph branches; deterministic evaluation with replanning, abandonment, loop recovery |
| Safety boundaries | Command-length limits, target allowlists, platform/internet blocking, per-phase command families, supervisor-only flag submission + paid hints (`halctl` only adapter) |
| Parallel workers | Task DAG scheduler with conflict keys, scope-limited specialists, reducer that merges validated findings into facts |
| Deterministic bootstrap | Parses targets, retrieves status, submits smoke flag, requests free hint, probes reachability before the main loop |
| Optional dashboard | Strict-TypeScript, zero-runtime-dependency read-only viewer (runs, graph, events, artifacts, metrics, replay) outside the image |

## Safety model

OzzGraph is a security harness: it intentionally makes dangerous or
unverifiable paths hard or impossible. The model is **untrusted**; the harness
owns all safety boundaries.

- **State lives outside model context.** SQLite graph, append-only JSONL
  events, and a content-addressed artifact store are authoritative. A model
  claim is a hypothesis, never confirmed state; facts require deterministic
  evidence.
- **One bounded action per turn.** Every action has a timeout, an output
  limit, and a normalized fingerprint. Duplicates and failed fingerprints are
  never retried (loop prevention). No multi-command plans disguised as one
  action.
- **No raw MCP.** Models never call MCP directly; `halctl` is the HalCTF
  adapter surface, and only the supervisor may submit flags, buy paid hints,
  or exit the run (`OZZGRAPH_HAL_PRIVILEGED`). Local environments run tools
  through the policy-gated tool plane instead.
- **Command and target allowlists.** Command-length limits, target allowlists,
  and platform / public-internet destination blocking are enforced before
  execution. Per-phase command families gate what a worker may run.
- **Deterministic, provenance-validated facts.** Every `Fact` references
  evidence; every evidence references an observation or artifact; replaying
  the event log reconstructs the identical graph hash.
- **Authorized, isolated environments only.** OzzGraph is designed for
  sanctioned security assessments, never production or public-internet
  targets.

What OzzGraph does **not** guarantee: it does not sandbox a hostile model's
*output* from influencing a run beyond the above controls, and it is not a
general-purpose automation framework. See [SECURITY.md](SECURITY.md) for
vulnerability reporting and the full threat model.

## Quick start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies
uv sync

# 2. v2 default experience — one authorized target, local assessment mode
uv run python -m ozzgraph run http://127.0.0.1:8000

# 3. Classic env-configured run (HAL_* selects the HalCTF environment)
export HAL_USER_ID=team-42
export OZZGRAPH_CHALLENGE_ID=challenge-01
export OZZGRAPH_TARGET=http://127.0.0.1:8000
export OZZGRAPH_TARGET_ALLOWLIST=127.0.0.1
export OZZGRAPH_MODEL_BASE_URL=http://127.0.0.1:8000/v1   # OpenAI-compatible
export OZZGRAPH_MODEL_API_KEY=...                          # optional
uv run python -m ozzgraph
```

A run prints the identity line (`USER ID: team-42`), runs deterministic
bootstrap reconnaissance, then executes the bounded loop until a budget is
exhausted, a signal arrives, or the run completes — always ending with a
human-readable `TERMINATION: <reason>` line (exit codes: `0` completed, `1`
failed, `130` interrupted, `3` budget exhausted). State and artifacts are
written to `state/` by default (`state/actions.jsonl`, `state/graph.db`,
`state/artifacts/`); a completed v2 run also renders the report bundle
(`report.md` / `report.json` / `report.sarif` / `evidence/`).

### Talking to the challenge platform (`halctl`)

`halctl` is the local terminal-native adapter for the HalCTF environment.
Each subcommand prints exactly one JSON document:

```bash
uv run halctl challenge show --challenge-id "$OZZGRAPH_CHALLENGE_ID"
uv run halctl status --challenge-id "$OZZGRAPH_CHALLENGE_ID"
uv run halctl submit --flag 'flag{...}' --challenge-id "$OZZGRAPH_CHALLENGE_ID"
uv run halctl hint --index 0 --challenge-id "$OZZGRAPH_CHALLENGE_ID"
uv run halctl scoreboard
uv run halctl exit --reason completed
```

Flag submission, paid hints (`--index` > 0), and `exit` are
**supervisor-only**: they fail unless `OZZGRAPH_HAL_PRIVILEGED` is set, which
only the supervisor does. See [docs/USAGE.md](docs/USAGE.md) for the full
walkthrough.

## Documentation

| Document | Purpose |
| --- | --- |
| [docs/CHANGES_v2.md](docs/CHANGES_v2.md) | v2 milestone (V01–V10): general security-research reframing + per-milestone implementation notes |
| [docs/USAGE.md](docs/USAGE.md) | install, configuration, running a capture, replay, lifecycle |
| [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) | model profiles + adapters, skills, policies, workers |
| [docs/PRD.md](docs/PRD.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/TECHNICAL_REQUIREMENTS.md](docs/TECHNICAL_REQUIREMENTS.md) | product, architecture, technical requirements |
| [docs/DATA_STRATEGY.md](docs/DATA_STRATEGY.md) · [docs/API_AND_INTEGRATIONS.md](docs/API_AND_INTEGRATIONS.md) | data strategy, API and integrations |
| [docs/TESTING_AND_QA.md](docs/TESTING_AND_QA.md) · [docs/GOLDEN_TRACES.md](docs/GOLDEN_TRACES.md) | testing/QA gates, golden traces |
| [docs/OBSERVATIONS.md](docs/OBSERVATIONS.md) · [docs/BENCHMARKS.md](docs/BENCHMARKS.md) | semantic observation parsers, V10 full-regression benchmark suite |
| [docs/SYNTHETIC_LAB.md](docs/SYNTHETIC_LAB.md) · [docs/IMAGE_HARDENING.md](docs/IMAGE_HARDENING.md) | synthetic lab, container hardening |
| [docs/RELEASE.md](docs/RELEASE.md) · [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | release ops, implementation plan |
| [docs/adr/](docs/adr/) | architecture decision records |
| [AGENTS.md](AGENTS.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | agent governance, contribution workflow, security policy |

## Repository Layout

```text
ozzgraph/
├── pyproject.toml          # package metadata, scripts, tool config
├── uv.lock
├── Dockerfile              # immutable competition image (see docs/IMAGE_HARDENING.md)
├── README.md
├── AGENTS.md               # coding-agent governance for this repo
├── CONTRIBUTING.md         # build/test/lint/PR workflow
├── SECURITY.md             # vulnerability reporting + security model
├── .github/workflows/ci.yml
├── docs/                   # PRD, architecture, API, ADRs, usage, customization, ...
├── scripts/                # e.g. SBOM generation
├── src/ozzgraph/           # the harness kernel (halctl entry point too)
│   ├── environments/       # EnvironmentAdapter: halctf/ + local/ (url · network · host · repository · docker-compose · hybrid)
│   ├── lab/                # synthetic vulnerable targets (benchmark + E2E matrix)
│   ├── benchmarks/         # V10 full-regression suite + plain-ReAct baseline
│   ├── profile_data/       # per-model empirical profile TOMLs
│   ├── findings.py         # findings model (CWE, assets, evidence, impact CIA, confidence)
│   ├── observations.py     # semantic tool parsers/projectors (raw-first)
│   ├── security_brain.py   # opportunity generator → strategic planner → hypothesis manager → progress evaluator
│   ├── specialists.py      # SpecialistFleet: bounded deterministic micro-agents
│   ├── profile_store.py    # empirical model profiles (loads profile_data/)
│   └── matrix.py           # model-matrix harness (format compliance, tool selection, solve rates)
├── tests/                  # pytest suite (unit, integration, chaos, replay, ...)
└── dashboard/              # optional read-only TS dashboard (outside the image)
```

## Development Commands

| Command | Purpose |
| --- | --- |
| `uv sync` | install dependencies into `.venv` |
| `uv run ruff check .` | lint |
| `uv run ruff format --check .` | format check (also checks Python blocks inside `docs/`) |
| `uv run mypy src` | strict typing |
| `uv run pytest` | full test suite (1181 tests) |

The optional dashboard (setup and endpoints: [API_AND_INTEGRATIONS.md](docs/API_AND_INTEGRATIONS.md)
§ Optional Dashboard API) lives in `dashboard/`:

```bash
cd dashboard
yarn install
yarn lint && yarn typecheck && yarn test && yarn build
yarn start --runs-dir ../state --host 127.0.0.1 --port 8787
```

## Container Image

```bash
docker build -t ozzgraph .
docker run --rm ozzgraph --version
docker run --rm --read-only --tmpfs /tmp \
  -e HAL_USER_ID=team-42 \
  -e OZZGRAPH_MAX_RUNTIME_S=7200 \
  ozzgraph
```

The image is a multi-stage, non-root (uid 10001), read-only-rootfs runtime
with `halctl` on PATH; state and artifacts live under
`/var/lib/ozzgraph/state` (mount `-v /host/state:/var/lib/ozzgraph/state` to
persist). Build recipe, size budget (<1.5 GB), SBOM generation, and startup
measurements: [docs/IMAGE_HARDENING.md](docs/IMAGE_HARDENING.md).
