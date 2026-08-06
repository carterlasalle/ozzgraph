# OzzGraph

OzzGraph is a model-adaptive autonomous CTF agent harness for authorized, isolated security challenges.

It combines:

- Ozz-style security phases: reconnaissance, enumeration, exploitation, post-exploitation, pivoting, and flag hunting
- A Planner–Executor–Evaluator control loop
- Pi-style minimal runtime architecture and branchable session history
- Codex-style terminal-native interaction
- Claude Code-style scoped workers and lifecycle hooks
- OpenCode-style lazy skill loading
- Antares-style narrow vulnerability specialists
- Graph-engineered state, evidence provenance, and deterministic handoffs

The core principle is:

> The model supplies judgment when the next action is uncertain. The harness supplies memory, discipline, tools, safety boundaries, execution, evidence, and recovery.

## Documentation

- [Product Requirements](docs/PRD.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Technical Requirements](docs/TECHNICAL_REQUIREMENTS.md)
- [Data Strategy](docs/DATA_STRATEGY.md)
- [API and Integrations](docs/API_AND_INTEGRATIONS.md)
- [Testing and Quality Assurance](docs/TESTING_AND_QA.md)
- [Deployment and Infrastructure](docs/DEPLOYMENT_AND_INFRASTRUCTURE.md)
- [Security](docs/SECURITY.md)
- [Model Adapters](docs/MODEL_ADAPTERS.md)
- [Prompt Engineering](docs/PROMPT_ENGINEERING.md)
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md)
- [Release and Operations](docs/RELEASE_AND_OPERATIONS.md)
- [Agent Instructions](AGENTS.md)

## Planned Stack

### Competition runtime

- Python 3.12
- `uv` for dependency management and locking
- SQLite for state
- JSONL for append-only event logs
- Local filesystem artifact store
- OCI-compatible container
- No bundled model weights, CUDA, PyTorch, or vLLM

### Optional local dashboard

- TypeScript
- Yarn
- Separate from the competition image

## Repository Layout

```text
ozzgraph/
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── README.md
├── AGENTS.md
├── docs/
├── src/ozzgraph/
├── tests/
├── fixtures/
├── local-lab/
└── dashboard/
```

## Development Commands

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

Optional dashboard:

```bash
cd dashboard
yarn install --immutable
yarn dev
```

## Status

This repository currently contains the product and engineering specification. Implementation should follow the PR sequence in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).
