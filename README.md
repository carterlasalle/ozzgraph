# OzzGraph

OzzGraph is a model-adaptive autonomous CTF agent harness for authorized,
isolated security challenges. It wraps a small deterministic kernel around a
planner–executor–evaluator loop so even modest models behave like disciplined
security operators.

The core principle:

> The model supplies judgment when the next action is uncertain. The harness
> supplies memory, discipline, tools, safety boundaries, execution, evidence,
> and recovery.

**Status: spec-complete — all 32 PRs of the implementation plan are merged and
this is the v1.0 release candidate (`1.0.0`).** Every Definition of Done item
passed the PR32 rehearsal (19/19, see [docs/RELEASE.md](docs/RELEASE.md)).

## What the harness does

- **Ozz-style security phases** — BOOTSTRAP, RECON, ENUMERATION,
  EXPLOITATION, POST_EXPLOITATION, PIVOT, FLAG_HUNT, VERIFY_AND_SUBMIT,
  REPLAN, DONE — driven by **graph-state predicates, never action counts**.
- **Authoritative state outside model context** — a SQLite state graph
  (`graph.db`), an append-only JSONL event log (`actions.jsonl`), and a
  content-addressed artifact store. Replaying the event log reconstructs the
  identical graph hash.
- **One bounded action per executor turn** — every action carries a timeout,
  an output limit, and a normalized fingerprint; duplicates and failed
  fingerprints are never retried (loop prevention).
- **Lazy skill registry** — compact per-phase summaries are advertised to the
  model; full skill cards load only when a skill is selected.
- **Model profiles + three adapter protocols** — terminal-native free text,
  strict three-line, and structured JSON, each with its own prompt compiler,
  parser, and deterministic repair strategy.
- **Planner–Executor–Evaluator** — bounded ranked plans only when the graph
  branches; deterministic evaluation with replanning, abandonment, and loop
  recovery.
- **Safety boundaries** — command-length limits, target allowlists, platform
  and public-internet blocking, per-phase command families, and
  supervisor-only flag submission and paid hints (models never call raw MCP;
  `halctl` is the only adapter surface).
- **Bounded parallel workers** — a task DAG scheduler with conflict keys,
  scope-limited specialist workers, and a reducer that merges validated
  findings into authoritative facts.
- **Deterministic bootstrap** — parses targets, retrieves challenge status,
  submits a smoke flag, requests the free hint, and probes reachability before
  the main loop.
- **Optional local dashboard** — a strict-TypeScript, zero-runtime-dependency
  read-only viewer for runs, graph, events, artifacts, metrics, and replay
  (outside the competition image).

## Quick start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies
uv sync

# 2. Configure (all configuration is environment-driven; see docs/USAGE.md)
export HAL_USER_ID=team-42
export OZZGRAPH_CHALLENGE_ID=challenge-01
export OZZGRAPH_TARGET=http://127.0.0.1:8000
export OZZGRAPH_TARGET_ALLOWLIST=127.0.0.1
export OZZGRAPH_MODEL_BASE_URL=http://127.0.0.1:8000/v1   # OpenAI-compatible
export OZZGRAPH_MODEL_API_KEY=...                          # optional

# 3. Run the harness
uv run python -m ozzgraph
```

The run prints the identity line (`USER ID: team-42`), runs deterministic
bootstrap reconnaissance, then executes the bounded loop until a budget is
exhausted, a signal arrives, or the run completes — always ending with a
human-readable `TERMINATION: <reason>` line (exit codes: `0` completed, `1`
failed, `130` interrupted, `3` budget exhausted). State and artifacts are
written to `state/` by default (`state/actions.jsonl`, `state/graph.db`,
`state/artifacts/`).

### Talking to the challenge platform (`halctl`)

`halctl` is the local terminal-native adapter for the HalCTF MCP integration.
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

- **Usage guide** — [docs/USAGE.md](docs/USAGE.md): install, configuration,
  running a capture, artifact store + event log + replay, executor semantics,
  budgets/heartbeat/lifecycle, scheduling.
- **Customization** — [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md): model
  profiles + adapters, skill registry, scope policy, hint policy, specialist
  workers, reducer, phase routing.
- [Product Requirements](docs/PRD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Technical Requirements](docs/TECHNICAL_REQUIREMENTS.md)
- [Data Strategy](docs/DATA_STRATEGY.md)
- [API and Integrations](docs/API_AND_INTEGRATIONS.md)
- [Testing and Quality Assurance](docs/TESTING_AND_QA.md)
- [Golden Traces](docs/GOLDEN_TRACES.md)
- [Synthetic Lab](docs/SYNTHETIC_LAB.md)
- [Image Hardening](docs/IMAGE_HARDENING.md)
- [Release and Operations](docs/RELEASE.md)
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md)
- [Architecture Decision Records](docs/adr/)
- [Agent Instructions](AGENTS.md)

## Repository Layout

```text
ozzgraph/
├── pyproject.toml          # package metadata, scripts, tool config
├── uv.lock
├── Dockerfile              # immutable competition image (see docs/IMAGE_HARDENING.md)
├── README.md
├── AGENTS.md               # coding-agent governance for this repo
├── .github/workflows/ci.yml
├── docs/                   # PRD, architecture, API, ADRs, usage, customization, ...
├── scripts/                # e.g. SBOM generation
├── src/ozzgraph/           # the harness kernel (halctl entry point too)
├── tests/                  # pytest suite (unit, integration, chaos, replay, ...)
└── dashboard/              # optional read-only TS dashboard (outside the image)
```

## Development Commands

```bash
uv sync
uv run ruff check .          # lint
uv run ruff format --check . # format (also checks Python blocks inside docs/)
uv run mypy src              # strict typing
uv run pytest                # full test suite
```

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
