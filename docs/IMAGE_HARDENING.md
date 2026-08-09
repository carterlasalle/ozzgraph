# Image Hardening (T31)

The OzzGraph competition container is a small, immutable, self-contained
runtime for the Python kernel (`python -m ozzgraph` supervisor entry, `halctl`
adapter on PATH). This document is the authoritative reference for building,
measuring, auditing, and verifying the image. See
[ADR-0007](adr/0007-immutable-competition-image.md) for the architectural
decision and [TESTING_AND_QA.md](TESTING_AND_QA.md) for the CI gates.

## Goals and Constraints

- **Size target**: built image under 1.5 GB (Phase 12 exit criteria). The
  budget is enforced in CI with `1500 * 1024 * 1024` bytes (1.5 GiB).
- **No forbidden payloads**: no CUDA, PyTorch, vLLM, or model weights
  (AGENTS.md Forbidden Shortcuts).
- **No public-internet dependency at runtime**: the image contains only the
  base OS, Python 3.12, and the three runtime deps from `pyproject.toml`
  (pydantic, httpx, aiosqlite) plus their locked transitive deps.
- **Dashboard out**: `dashboard/` (Yarn/TypeScript) is excluded from the
  build context and never enters the image (ADR-0006).
- **Immutable runtime**: non-root user, read-only rootfs support, state on a
  declared volume, configuration exclusively via environment.
- **Kernel untouched**: PR31 makes no kernel code changes; replay and hash
  compatibility are preserved by construction.

## Build Recipe

Requires a Docker daemon with BuildKit (Docker >= 23, or buildx).

```bash
# From the repository root:
docker build -t ozzgraph:latest .

# Multi-arch build (optional):
docker buildx build --platform linux/amd64,linux/arm64 -t ozzgraph:latest .

# Quick sanity checks:
docker run --rm ozzgraph:latest --version
docker run --rm --entrypoint halctl ozzgraph:latest --help
```

The Dockerfile is multi-stage:

1. `builder` — `python:3.12-slim`, uv pinned to `0.12.1` (the version used to
   maintain `uv.lock`), runs `uv sync --frozen --no-dev --no-editable`. The
   venv is fully self-contained (project installed as a real wheel, dev
   dependency group excluded, no editable `.pth` into the source tree).
2. `runtime` — fresh `python:3.12-slim`; copies only the venv, creates the
   non-root operator (`ozzgraph`, uid 10001), sets env, declares the state
   volume, and drops pip.

## Max Image (Kali, `:max`)

V03 (tool-runtime) adds a second, opt-in image for full-toolkit research:
`docker/Dockerfile.kali` builds `ozzgraph:max` from `kalilinux/kali-rolling`
with the `kali-linux-everything` metapackage (the entire Kali toolset) plus
the same uv-installed app the default image ships. The V03 tool inventory
(`src/ozzgraph/toolplane.py`) then finds the real toolset at startup —
nmap, ffuf, nuclei, netexec, semgrep, trivy, gitleaks, binwalk, exiftool,
searchsploit, and everything else `kali-linux-everything` installs.

```bash
# Build (from the repository root; same context as the default image):
docker build -f docker/Dockerfile.kali -t ozzgraph:max .

# Sanity checks (same contract as the default image):
docker run --rm ozzgraph:max --version
docker run --rm --entrypoint nmap ozzgraph:max --version
docker run --rm --entrypoint python ozzgraph:max -c \
  "from ozzgraph.toolplane import ToolInventory; i = ToolInventory().run(); print(len(i.capabilities.available()), 'capabilities')"

# A local assessment run with the full toolset:
docker run --rm -v /var/lib/ozzgraph/state \
  -e OZZGRAPH_TARGET=http://127.0.0.1:3000 \
  -e OZZGRAPH_MODEL_BASE_URL=http://host.docker.internal:8000/v1 \
  ozzgraph:max
```

Deliberate differences vs the default image (ADR-0007 hardening is NOT
regressed — this is a separate, opt-in tool image):

| Aspect | Default image | `:max` (Kali) |
| --- | --- | --- |
| Base | `python:3.12-slim` (digest-pinned) | `kalilinux/kali-rolling` (rolling — latest toolset by design) |
| Toolset | none beyond the Python runtime | `kali-linux-everything` (tens of GB installed) |
| User | non-root `ozzgraph` (uid 10001) | root (Kali tooling needs raw sockets/packet capture; add `--cap-add=NET_RAW --cap-add=NET_ADMIN` or `--privileged` for scans) |
| Rootfs | immutable, `--read-only` safe | mutable (a tool image, not the competition runtime) |
| Package installer | pip removed | kept (apt/uv stay for tool updates) |
| Size budget | < 1.5 GiB (CI tripwire) | NOT budget-bound (kali-linux-everything is intentionally huge) |

Both images share the same kernel entrypoint (`python -m ozzgraph`), the
same venv install (`uv sync --frozen --no-dev --no-editable`), the same
state volume, and the same env-driven configuration — a run configured for
the default image behaves identically on `:max` except for the tools the
inventory finds.

## Minimization Choices

| Choice | Effect |
| --- | --- |
| `python:3.12-slim` base (not full/bookworm, not GPU images) | ~43 MB base layer; no CUDA/ML stacks by construction |
| Multi-stage build | build tools (uv, hatchling, wheel cache) never reach the final image |
| `uv sync --no-dev --no-editable` | only the 3 runtime deps + locked transitive deps; no dev group (pytest, ruff, mypy, pyright) |
| `--no-editable` wheel install | final image does not need the source tree |
| `pip uninstall -y pip` in runtime | no runtime package installer; ~7 MB smaller |
| `.dockerignore` | excludes `.git`, `.venv`, `dashboard/`, `docs/`, `tests/`, `fixtures/`, `state/`, caches, secrets |
| `PYTHONDONTWRITEBYTECODE=1` | no `.pyc` writes — required for a read-only rootfs |
| `PYTHONUNBUFFERED=1` | heartbeat/event output streams immediately |
| Digest-pinned base image | reproducible builds (manifest list pinned 2026-08-05) |
| No EXPOSE, no HEALTHCHECK | outbound-only harness; liveness is the kernel heartbeat |

Forbidden shortcuts (AGENTS.md) are structurally impossible: no `pip install`
from any index in the runtime stage, no Node, no dashboard, no weights.

## Size Measurements

The authoring environment for PR31 has **no Docker daemon**, so the final
image could not be built and weighed locally; the CI `docker` job is the
authoritative build-and-measure gate. Every component below was measured
directly (2026-08-07):

| Component | Size | How measured |
| --- | --- | --- |
| `python:3.12-slim` amd64 base layer | 43.2 MB (uncompressed) | Docker Hub Registry API for tag `3.12-slim` (manifest digest `sha256:229a2c5b…`, pushed 2026-08-05) |
| Runtime dependency venv | 9.8 MB total / 9.7 MB site-packages | `uv export --frozen --no-dev` + fresh venv + `uv pip install` (uv 0.12.1, Python 3.12) |
| Full project venv (deps + `ozzgraph` wheel + console scripts) | 14 MB | `uv sync --frozen --no-dev --no-editable` into a scratch dir |
| `src/ozzgraph` source | 0.8 MB | `du` (excl. `__pycache__`) |
| Base pip (removed in runtime) | ~7 MB | `du` of base site-packages |

**Expected final uncompressed size: ~50–60 MB** (43.2 MB base − ~7 MB pip +
14 MB venv + layer overhead). This is two orders of magnitude below the
1.5 GB budget; the CI assertion is a tripwire, not a tight fit.

Measure the real image in CI or locally with:

```bash
docker build -t ozzgraph:latest .
docker image inspect ozzgraph:latest --format '{{.Size}}'   # bytes
# or human-readable:
docker image inspect ozzgraph:latest --format '{{.Size}}' | awk '{printf "%.1f MiB\n", $1/1024/1024}'
```

## SBOM

`scripts/gen-sbom.sh` produces an SPDX 2.3 JSON and a CycloneDX 1.5 JSON for
a built image, using [syft](https://github.com/anchore/syft):

```bash
# Install syft once:
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin

# Generate (image + output dir are optional, defaults below):
scripts/gen-sbom.sh ozzgraph:latest sbom/
# -> sbom/ozzgraph.spdx.json   sbom/ozzgraph.cdx.json

# Optionally attach the SBOM to the image as an OCI artifact (requires cosign):
cosign attach sbom --sbom sbom/ozzgraph.spdx.json --type spdx ozzgraph:latest
```

The script prints a package-count sanity summary from the SPDX document.
Run it from a host with the image loaded; it needs no network access to the
image itself.

**Fallback without syft** (Python-dependency audit only, no OS packages):

```bash
uv export --frozen --no-dev | uv run pip-audit -r /dev/stdin
```

## Startup Time and Memory Profile

Measured on the authoring host (2026-08-07, Python 3.12, uv 0.12.1, same
dependency set and same `--no-editable` venv layout as the image):

| Scenario | Wall time | Peak RSS | Notes |
| --- | --- | --- | --- |
| `python -m ozzgraph --version` (direct venv python) | 0.55 s | 38.9 MB | full kernel import cost |
| `uv run python -m ozzgraph --version` | 0.58 s | 39.0 MB | includes uv runner overhead |
| 3-second supervised run (heartbeat 1 s) | 3.81 s | 43.6 MB | exits 3 = BUDGET_EXHAUSTED; state dir 12 KB |

The container adds roughly 0.2–0.5 s of base-image process spawn on top of
the kernel import (~0.55 s), so cold start is expected under ~1 s. Peak RSS
for the running kernel is ~44 MB, dominated by Python + pydantic import.

Reproduce in CI or locally (container values):

```bash
# Startup (import cost):
/usr/bin/time -v docker run --rm ozzgraph:latest --version

# Short supervised run with memory profile (host-side):
/usr/bin/time -v docker run --rm --read-only --tmpfs /tmp \
  -e HAL_USER_ID=probe -e OZZGRAPH_MAX_RUNTIME_S=10 \
  -e OZZGRAPH_HEARTBEAT_INTERVAL_S=2 ozzgraph:latest

# Live sampling with docker stats (run in a second terminal):
docker stats --no-stream <container-id>
```

## Immutable-Image Properties

- **Non-root**: runtime user `ozzgraph` (uid 10001); the kernel is the only
  process.
- **Read-only rootfs**: the image runs correctly with `--read-only --tmpfs /tmp`
  (the smoke test in CI proves it). State and artifacts land in
  `/var/lib/ozzgraph/state` (declared `VOLUME`), so they are writable while
  the rootfs stays read-only. Persist them with
  `-v /host/path:/var/lib/ozzgraph/state` when a run must survive container
  exit.
- **Env-driven configuration**: every knob (`HAL_USER_ID`, `OZZGRAPH_*` from
  `src/ozzgraph/config.py`) is an environment variable; the image carries no
  baked-in identity or challenge config.
- **No runtime package installer** (pip removed), no shell wrapper around the
  entrypoint, no exposed ports.
- **Deterministic Python**: `PYTHONDONTWRITEBYTECODE=1` + `PYTHONUNBUFFERED=1`.

## Fallback Verification

If a build or smoke step cannot run (no Docker daemon), verify the container
story without Docker:

1. `bash -n scripts/gen-sbom.sh` — SBOM script syntax.
2. `uv sync --frozen --no-dev --no-editable` in a scratch dir — the exact
   builder-stage command; then check `.venv/bin/python -m ozzgraph --version`
   and `.venv/bin/halctl --help` run from the copied venv alone.
3. `uv run pytest tests/test_image_hardening.py` — shape/budget tests
   (no Docker required).
4. The CI `docker` job performs the authoritative build, size assertion, and
   smoke runs on every PR.

## CI Gate

`.github/workflows/ci.yml` job `docker` (Docker image — build + size + smoke):

1. Builds the image (`docker/build-push-action`, buildx, GHA cache).
2. Asserts `docker image inspect` size `< 1500 * 1024 * 1024` bytes.
3. Smoke: `docker run --rm IMAGE --version` (ENTRYPOINT).
4. Smoke: `docker run --rm --entrypoint halctl IMAGE --help` (halctl on PATH).
5. Smoke: `docker run --rm --entrypoint id IMAGE` — the runtime user must be
   `uid=10001(ozzgraph)` / `gid=10001(ozzgraph)`, never root (PR32).
6. Smoke: short supervised run under `--read-only --tmpfs /tmp`, asserting the
   BUDGET_EXHAUSTED exit code 3, the `USER ID:` identity line in the log, and
   a `TERMINATION: budget_exhausted` final line (startup + identity +
   termination-summary evidence, PR32). This is the LOCAL-mode mapping (no
   HalCTF runtime variable); a HalCTF-mode run exits 0 for every structured
   termination (docs/adr/0012).

If the workflow ever runs on a runner without Docker, gate the job behind a
repository variable (`if: ${{ vars.RUN_DOCKER_JOB == 'true' }}`) — the job
definition must remain so the gate stays visible.

## Related Documents

- [ADR-0007](adr/0007-immutable-competition-image.md) — architectural decision
- [TESTING_AND_QA.md](TESTING_AND_QA.md) — quality gates and test suite
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — Phase 12 exit criteria
- [AGENTS.md](../AGENTS.md) — invariants and forbidden shortcuts
