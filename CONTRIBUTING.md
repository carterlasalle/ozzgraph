# Contributing to OzzGraph

OzzGraph is a model-adaptive autonomous CTF agent harness for authorized,
isolated environments. This file covers the developer workflow: environment
setup, the quality gates, and how to land changes.

[AGENTS.md](AGENTS.md) is the authoritative governance document for all
coding work in this repository. If anything here conflicts with AGENTS.md,
AGENTS.md wins. Read it before your first change.

## Toolchain

| Tool | Version | Purpose |
| --- | --- | --- |
| Python | 3.12+ | Runtime language (`requires-python = ">=3.12"`) |
| [uv](https://docs.astral.sh/uv/) | any recent | Dependency management, venv, task runner |
| pytest | dev group | Test suite — 880 tests collected |
| ruff | dev group | Lint + format (line length 100, py312) |
| mypy | dev group | Strict type checking |
| pyright | dev group | Optional additional type checking (see `pyrightconfig.json`) |

Dependencies are declared in [pyproject.toml](pyproject.toml) and locked in
`uv.lock`. The runtime dependency set is deliberately small (pydantic, httpx,
aiosqlite); everything else lives in the `dev` dependency group and never
enters the competition image.

## Environment setup

```bash
# From the repository root. Creates .venv and installs the project plus the
# dev group (pytest, ruff, mypy, pyright).
uv sync

# If uv.lock changed on a pull / you want the exact locked set:
uv sync --frozen
```

Never add a dependency without updating the lockfile (`uv add` /
`uv lock`), and keep runtime dependencies minimal — the container image
policy (docs/IMAGE_HARDENING.md) forbids public-internet runtime
dependencies and bulk payloads (no CUDA, PyTorch, vLLM, model weights).

## Running tests

```bash
uv run pytest                # full suite (880 tests: unit, integration,
                             # chaos, replay/golden, lab E2E, image shape)
uv run pytest -x -q          # fast smoke — stop at first failure, quiet
uv run pytest tests/test_policy.py -q   # a single module while iterating
```

Kernel changes normally require unit, integration, replay/golden-trace, and
failure-path tests; prompt/adapter changes require fixture-based tests; tool
or parser changes require success/failure/truncation/adversarial fixtures.
See AGENTS.md § Testing Expectations for the full matrix.

## Quality gates

Run all four before committing:

```bash
uv run ruff check .           # lint
uv run ruff format --check .  # format (also checks Python blocks in docs/)
uv run mypy src               # strict typing (pyproject: strict = true)
uv run pytest -x -q           # tests
```

To auto-format instead of just checking: `uv run ruff format .` — but only
touch files your change actually needs (no drive-by reformatting).

## GitReins Tier-1 guards

The GitReins pre-commit hook (`.git/hooks/pre-commit`) runs
`gitreins guard` on every commit with these Tier-1 guards enabled
(`.gitreins/config.yaml`):

| Guard | What it runs |
| --- | --- |
| secrets | gitleaks scan (`.gitleaks.toml`) — catches `sk-` API keys etc. in any staged content; docs are **not** whitelisted |
| lint | `uv run ruff check .` |
| tests | `uv run pytest -x --tb=short` (full suite, fail fast) |

A commit that fails any guard is rejected. Fix the issue, re-stage, and
commit again. The Tier-2 LLM judge and the task lifecycle are owned by the
foreman process — contributors just commit.

## Branch and PR workflow

- Work on a short-lived branch off `main`: `git switch -c <topic>`.
- One PR per architectural layer or tightly related slice (AGENTS.md § Pull
  Request Scope). Do not combine, e.g.: graph schema changes with dashboard
  work, model adapters with unrelated security skills, lifecycle changes
  with broad refactors, or prompt rewrites with container dependency
  changes. Follow the sequence in docs/IMPLEMENTATION_PLAN.md.
- Commit with a conventional message and one commit per task, matching the
  repo history: `type: description. Addresses <task-id>.` (types: `feat`,
  `fix`, `docs`, `test`, `refactor`, `chore`). Docs-only changes use `docs:`.
- Do not push directly to `main` for feature work; open a PR so CI
  (`.github/workflows/ci.yml`: lint, format, type, test, docker jobs) runs
  against it.

## Definition of Done

From AGENTS.md § Definition of Done for a PR — a PR is complete only when:

- code is implemented
- documentation is updated
- tests cover success and failure
- `uv run ruff check .` passes
- `uv run mypy src` passes
- `uv run pytest` passes
- no architectural invariant is weakened
- errors fail loudly
- replay compatibility is preserved or migrated
- dependency and image-size impact is explained

## Work procedure

Before editing (AGENTS.md § Work Procedure):

1. Read the relevant document under `docs/`.
2. Identify the owning module.
3. State which invariants the change touches.
4. Add or update tests first when practical.
5. Implement the smallest complete slice.
6. Run focused tests.
7. Run the full quality gate.
8. Update documentation and ADRs for architectural decisions.

## Architecture Decision Records

Create an ADR under `docs/adr/` (use the template at
`docs/adr/0000-template.md`) when a change:

- introduces a new persistent storage technology
- changes a core model interaction protocol
- changes privileged-operation ownership
- changes graph event semantics
- changes process-isolation policy
- changes the runtime language or package manager
- adds a new concurrency model
- weakens an existing invariant

## Documentation

Docs live in `docs/` (PRD, architecture, ADRs, usage, customization,
testing/QA, golden traces, synthetic lab, image hardening, release) plus the
root-level README.md, AGENTS.md, and this file. Every code change must keep
the relevant docs accurate — stale docs fail the Definition of Done.
