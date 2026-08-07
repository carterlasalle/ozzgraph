# syntax=docker/dockerfile:1

# OzzGraph competition image (T31 — image hardening).
#
# Design (docs/IMAGE_HARDENING.md, ADR-0007):
#   * multi-stage: a uv builder produces a self-contained venv; the final
#     stage carries only the runtime (no build tools, no dev dependency group)
#   * python:3.12-slim base, digest-pinned for reproducibility
#   * non-root runtime user (uid 10001)
#   * immutable runtime: configuration is env-driven; the rootfs is safe to
#     run with `--read-only` (state lives on the declared VOLUME, /tmp is tmpfs)
#   * ENTRYPOINT runs the kernel (`python -m ozzgraph`); `halctl` is on PATH
#   * no CUDA / PyTorch / vLLM / model weights (AGENTS.md forbidden)
#   * no dashboard, no Node, no public-internet dependency at runtime

# ---------------------------------------------------------------- builder
# Digest pinned 2026-08-05 (manifest list, multi-arch); amd64 layer is
# 43.2 MB uncompressed (see docs/IMAGE_HARDENING.md for the measurement).
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS builder

# uv pinned to the version used to maintain uv.lock (0.12.1).
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

ENV UV_LINK_MODE=copy

WORKDIR /app

# Metadata + lockfile first (cached layer); the source comes after.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install runtime dependencies and the project itself as a real wheel.
# --no-editable: the venv is fully self-contained, so the final stage does
# not need the source tree. --no-dev keeps the dev dependency group out.
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

# Non-root operator; the kernel is the only long-lived process.
RUN useradd --create-home --uid 10001 --no-log-init ozzgraph

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# `halctl` console script on PATH; deterministic Python behavior; state lives
# outside the rootfs so the image can run with a read-only rootfs.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OZZGRAPH_STATE_DIR=/var/lib/ozzgraph/state \
    OZZGRAPH_ARTIFACT_DIR=/var/lib/ozzgraph/state/artifacts

# Hardening/minimization: drop the base image's pip (no runtime package
# installer, ~7 MB smaller), drop venv activate scripts, and hand the
# writable runtime directories to the operator user.
RUN pip uninstall -y pip && rm -f /app/.venv/bin/activate* \
    && mkdir -p /var/lib/ozzgraph/state \
    && chown -R ozzgraph:ozzgraph /var/lib/ozzgraph /app

# Writable state/artifact mount point (anonymous volume unless overridden).
VOLUME ["/var/lib/ozzgraph/state"]

USER ozzgraph

# Kernel entry: `docker run --rm IMAGE --version` prints the version; a full
# run is configured entirely via environment (HAL_USER_ID, OZZGRAPH_*).
# `halctl` is available on PATH (e.g. `docker run --entrypoint halctl IMAGE ...`).
ENTRYPOINT ["python", "-m", "ozzgraph"]
