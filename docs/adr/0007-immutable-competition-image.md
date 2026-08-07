# ADR-0007: Immutable, Minimal Competition Image

Status: accepted

Date: 2026-08-07

## Context

Phase 12 of docs/IMPLEMENTATION_PLAN.md ("Competition Hardening") requires
image minimization, a dependency audit, an SBOM, startup optimization, memory
profiling, fallback verification, and an immutable image, with an explicit
size target (< 1.5 GB) and no public-internet dependency. AGENTS.md forbids
bundling model weights and adding CUDA, PyTorch, or vLLM to the competition
image, and mandates that the optional Yarn dashboard never enter the image
(ADR-0006). The repository had no Dockerfile: the container story is created
from scratch.

The kernel is env-configured (`src/ozzgraph/config.py` reads `HAL_USER_ID`
and `OZZGRAPH_*` variables) and owns its state on disk (SQLite graph,
append-only JSONL events, artifact store), so a container adds process
isolation and packaging around an already self-contained runtime — it must
not change kernel behavior, replay semantics, or the graph hash.

## Decision

We will ship a single multi-stage `Dockerfile` at the repository root:

- **Builder stage**: `python:3.12-slim` (digest-pinned manifest list,
  pinned 2026-08-05) with uv pinned to `0.12.1` (the uv.lock maintenance
  version); `uv sync --frozen --no-dev --no-editable` produces a fully
  self-contained venv (project installed as a real wheel, dev dependency
  group excluded).
- **Runtime stage**: a fresh `python:3.12-slim` carrying only the copied
  venv. A non-root operator (`ozzgraph`, uid 10001) runs the kernel;
  `ENTRYPOINT ["python", "-m", "ozzgraph"]` and `halctl` on PATH.
- **Immutable by construction**: no build tools, no pip (removed), no dev
  deps, no dashboard/Node, no CUDA/weights; configuration exclusively via
  environment; `PYTHONDONTWRITEBYTECODE=1`; state on the declared volume
  `/var/lib/ozzgraph/state` so the rootfs can run `--read-only --tmpfs /tmp`.
- **Gates**: a CI job builds the image, asserts size < 1.5 GiB
  (1500 × 1024 × 1024 bytes), and runs three smoke tests (ENTRYPOINT
  `--version`, `halctl --help`, and a 2-second supervised run under a
  read-only rootfs that must terminate with exit code 3 = BUDGET_EXHAUSTED).
- **SBOM**: `scripts/gen-sbom.sh` emits SPDX 2.3 + CycloneDX 1.5 via syft,
  with a documented pip-audit fallback for Python-dependency-only audits.
- **No kernel changes**: the image packages the kernel as-is; replay and
  graph-hash compatibility are preserved by construction.

## Consequences

Easier:

- Competition deploys are a single, reproducible, auditable artifact; the
  size tripwire (1.5 GiB) is enforced on every PR.
- Supply-chain auditing has a repeatable SBOM path; runtime surface is
  minimal (3 runtime deps, no installer, no shell wrapper, no exposed ports).
- Read-only rootfs + volume-mounted state makes tampering and accidental
  writes structurally harder, and the non-root user bounds container
  privileges.

Harder:

- The digest-pinned base must be bumped deliberately for security updates;
  the tag date is documented in the Dockerfile comment.
- Without pip in the runtime stage, operators cannot install anything into
  the image at runtime — any addition requires a rebuild (this is intended).
- The authoring environment for this ADR had no Docker daemon, so the final
  image size was not measured locally; the CI docker job is the authoritative
  measurement, and component-level measurements are documented in
  docs/IMAGE_HARDENING.md.
- The CI docker job needs a Docker-capable runner; if one is unavailable the
  job is made conditional (repository variable) but remains defined.
