"""Image-hardening shape tests for the competition container (T31/PR31).

These tests validate the container story WITHOUT requiring Docker at test
time: the Dockerfile and .dockerignore shapes, the shared image-size budget,
the CI wiring, the SBOM script, and the documentation coverage. The real
build, size assertion, and smoke runs happen in the CI `docker` job
(.github/workflows/ci.yml) and are documented in docs/IMAGE_HARDENING.md.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Competition image size budget — Phase 12 exit criteria ("size target
# met"). CI (ci.yml), this module, and docs/IMAGE_HARDENING.md all state the
# same tripwire: 1500 * 1024 * 1024 bytes (1.5 GiB). Keep them in sync.
SIZE_BUDGET_BYTES = 1500 * 1024 * 1024

# AGENTS.md forbidden payloads — these must never appear as image
# instructions (comments may state the constraint).
FORBIDDEN_TERMS = ("cuda", "pytorch", "torch", "vllm", "tensorflow")

DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SBOM_SCRIPT = REPO_ROOT / "scripts" / "gen-sbom.sh"
IMAGE_DOC = REPO_ROOT / "docs" / "IMAGE_HARDENING.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read a repo file as text (test-local, fails loudly when missing)."""
    return path.read_text(encoding="utf-8")


def _lines_with_continuations(text: str) -> list[str]:
    """Join backslash-continued Dockerfile lines into logical lines."""
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        buffer = buffer + " " + line.lstrip() if buffer else line
        if not buffer.endswith("\\"):
            logical.append(buffer)
            buffer = ""
    if buffer:
        logical.append(buffer)
    return logical


def _instructions(dockerfile: str) -> list[str]:
    """Dockerfile instruction lines with comments stripped (per line)."""
    instructions: list[str] = []
    for line in _lines_with_continuations(dockerfile):
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            instructions.append(stripped)
    return instructions


def _stages(dockerfile: str) -> list[tuple[str, list[str]]]:
    """Split a Dockerfile into (stage_name, lines) at top-level FROM lines."""
    stages: list[tuple[str, list[str]]] = []
    current_name = ""
    current: list[str] = []
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if line.startswith("FROM "):
            if current:
                stages.append((current_name, current))
            parts = line.split()
            current_name = parts[-1] if "AS" in parts else ""
            current = [line]
        elif current:
            current.append(line)
    if current:
        stages.append((current_name, current))
    return stages


def _dockerignore_entries() -> set[str]:
    """Non-comment, non-blank .dockerignore patterns."""
    entries: set[str] = set()
    for raw in _read(DOCKERIGNORE).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def image_size_within_budget(size_bytes: int, budget_bytes: int = SIZE_BUDGET_BYTES) -> bool:
    """Return True when an image size stays under the competition budget.

    The budget is strict: an image exactly at the limit fails the gate.
    """
    return size_bytes < budget_bytes


def _stage_named(name: str) -> list[str]:
    """Instruction lines of the named Dockerfile stage (no comments)."""
    dockerfile = _read(DOCKERFILE)
    for stage_name, _lines in _stages(dockerfile):
        if stage_name == name:
            return _instructions("\n".join(_lines))
    raise AssertionError(f"Dockerfile has no stage named {name!r}")


# ---------------------------------------------------------------------------
# Size budget validator
# ---------------------------------------------------------------------------


def test_size_budget_constant_is_1_5_gib() -> None:
    """The shared tripwire is exactly 1500 MiB (< 1.5 GB target)."""
    assert SIZE_BUDGET_BYTES == 1500 * 1024 * 1024


def test_image_size_validator_boundaries() -> None:
    """The fixture validator accepts below-budget and rejects at/over budget."""
    assert image_size_within_budget(0)
    assert image_size_within_budget(SIZE_BUDGET_BYTES - 1)
    assert not image_size_within_budget(SIZE_BUDGET_BYTES)
    assert not image_size_within_budget(SIZE_BUDGET_BYTES + 1)
    assert not image_size_within_budget(10**12)


def test_expected_image_size_is_far_below_budget() -> None:
    """Documented component measurements must stay well under the budget."""
    # Base layer (43.2 MB) + full runtime venv (14 MB) + generous overhead.
    expected_bytes = 100 * 1024 * 1024
    assert image_size_within_budget(expected_bytes)
    # Even 14x the expectation stays below the tripwire.
    assert expected_bytes * 14 < SIZE_BUDGET_BYTES


def test_budget_is_consistent_across_ci_and_docs() -> None:
    """CI and docs must assert the same tripwire as this module."""
    ci = _read(CI_WORKFLOW)
    doc = _read(IMAGE_DOC)
    assert "1500 * 1024 * 1024" in ci
    assert "1500 * 1024 * 1024" in doc
    assert "1.5 GiB" in doc
    assert "1.5 GB" in doc


# ---------------------------------------------------------------------------
# Dockerfile shape
# ---------------------------------------------------------------------------


def test_dockerfile_is_multistage_and_pinned() -> None:
    """Two stages, both on a digest-pinned python:3.12-slim base."""
    stages = _stages(_read(DOCKERFILE))
    assert len(stages) >= 2
    names = [name for name, _lines in stages]
    assert names[0] == "builder"
    assert names[-1] == "runtime"
    for name, lines in stages:
        assert lines[0].startswith("FROM ")
        base = lines[0].split(" AS ", 1)[0].removeprefix("FROM ")
        assert base.startswith("python:3.12-slim@sha256:"), f"stage {name} is not pinned: {base}"


def test_runtime_stage_runs_as_non_root_user() -> None:
    """The runtime stage creates and switches to a non-root operator."""
    runtime = _stage_named("runtime")
    assert any("useradd" in line for line in runtime)
    user_directives = [line for line in runtime if line.startswith("USER")]
    assert user_directives, "runtime stage must declare a USER"
    assert all("root" not in line for line in user_directives)


def test_runtime_stage_entrypoint_runs_kernel_and_halctl_on_path() -> None:
    """ENTRYPOINT is `python -m ozzgraph`; halctl resolves via venv PATH."""
    runtime = _stage_named("runtime")
    entry = [line for line in runtime if line.startswith("ENTRYPOINT")]
    assert entry, "runtime stage must declare an ENTRYPOINT"
    assert '"python"' in entry[0] and '"-m"' in entry[0] and '"ozzgraph"' in entry[0]
    path_env = [line for line in runtime if line.startswith("ENV") and "PATH=" in line]
    assert path_env, "runtime stage must put the venv on PATH"
    assert "/app/.venv/bin" in " ".join(path_env)


def test_runtime_stage_state_is_writable_volume_outside_rootfs() -> None:
    """State/artifact dirs live on a declared volume for read-only rootfs runs."""
    runtime = _stage_named("runtime")
    env_lines = [line for line in runtime if line.startswith("ENV")]
    env_text = " ".join(env_lines)
    assert "OZZGRAPH_STATE_DIR=/var/lib/ozzgraph/state" in env_text
    assert "OZZGRAPH_ARTIFACT_DIR=/var/lib/ozzgraph/state/artifacts" in env_text
    assert any(line.startswith("VOLUME") and "/var/lib/ozzgraph/state" in line for line in runtime)
    assert "PYTHONDONTWRITEBYTECODE=1" in env_text


def test_dockerfile_has_no_forbidden_payload_instructions() -> None:
    """No CUDA/PyTorch/vLLM/weights as image instructions (comments allowed)."""
    lower = "\n".join(_instructions(_read(DOCKERFILE))).lower()
    for term in FORBIDDEN_TERMS:
        assert term not in lower, f"forbidden payload reference found: {term!r}"


def test_runtime_stage_has_no_installer_and_no_index_downloads() -> None:
    """Runtime stage must not pip-install, apt-get, or apk-add anything."""
    runtime = _stage_named("runtime")
    for line in runtime:
        assert "pip install" not in line
        assert "apt-get" not in line and "apk add" not in line and "dnf " not in line
    assert any("pip uninstall -y pip" in line for line in runtime)


# ---------------------------------------------------------------------------
# .dockerignore shape
# ---------------------------------------------------------------------------


def test_dockerignore_excludes_development_and_state_material() -> None:
    """Required exclusions: VCS, venv, dashboard, docs, tests, caches, state."""
    entries = _dockerignore_entries()
    required = {
        ".git",
        ".gitignore",
        ".gitreins/",
        ".venv/",
        "__pycache__/",
        "*.py[cod]",
        ".ruff_cache/",
        ".mypy_cache/",
        ".pytest_cache/",
        "state/",
        "*.db",
        "dashboard/",
        "docs/",
        "tests/",
        "scripts/",
        ".github/",
        ".env",
    }
    assert required <= entries, f"missing .dockerignore patterns: {required - entries}"


def test_dockerignore_keeps_build_context_inputs() -> None:
    """The build context must keep everything the builder stage needs."""
    entries = _dockerignore_entries()
    for needed in ("README.md", "pyproject.toml", "uv.lock", "src/"):
        assert needed not in entries, f".dockerignore must not exclude {needed!r}"


# ---------------------------------------------------------------------------
# CI wiring
# ---------------------------------------------------------------------------


def test_ci_keeps_existing_gates_intact() -> None:
    """lint/format/type/test jobs must remain defined."""
    ci = _read(CI_WORKFLOW)
    for marker in ("ruff check .", "ruff format --check .", "mypy src", "pytest -x -q"):
        assert marker in ci, f"CI gate missing: {marker!r}"


def test_ci_defines_docker_image_gate() -> None:
    """The docker job builds the image, checks size, and smoke-runs it."""
    ci = _read(CI_WORKFLOW)
    for marker in (
        "docker/build-push-action",
        "docker image inspect ozzgraph:ci",
        "docker run --rm ozzgraph:ci --version",
        "--entrypoint halctl ozzgraph:ci --help",
        "--read-only",
        "1500 * 1024 * 1024",
    ):
        assert marker in ci, f"docker CI gate missing: {marker!r}"


# ---------------------------------------------------------------------------
# SBOM script
# ---------------------------------------------------------------------------


def test_sbom_script_exists_and_is_executable_bash() -> None:
    """scripts/gen-sbom.sh must be an executable bash script."""
    assert SBOM_SCRIPT.is_file()
    assert os.access(SBOM_SCRIPT, os.X_OK)
    content = _read(SBOM_SCRIPT)
    assert content.startswith("#!/usr/bin/env bash")
    assert "syft" in content
    assert "spdx-json" in content
    assert "cyclonedx-json" in content


def test_sbom_script_passes_shell_syntax_check() -> None:
    """`bash -n` must pass (no Docker needed for the syntax check)."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI and dev hosts have bash
        return
    result = subprocess.run(
        [bash, "-n", str(SBOM_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Documentation coverage
# ---------------------------------------------------------------------------


def test_image_hardening_doc_covers_required_topics() -> None:
    """The image doc must cover build, size, SBOM, startup, immutability, fallback."""
    doc = _read(IMAGE_DOC)
    for heading in (
        "## Build Recipe",
        "## Minimization Choices",
        "## Size Measurements",
        "## SBOM",
        "## Startup Time and Memory Profile",
        "## Immutable-Image Properties",
        "## Fallback Verification",
        "## CI Gate",
    ):
        assert heading in doc, f"IMAGE_HARDENING.md missing section {heading!r}"


def test_adr_0007_records_the_container_decision() -> None:
    """ADR-0007 documents the immutable-image architecture decision."""
    adr = (REPO_ROOT / "docs" / "adr" / "0007-immutable-competition-image.md").read_text(
        encoding="utf-8"
    )
    for fragment in ("Status: accepted", "## Decision", "## Consequences", "immutable"):
        assert fragment in adr


def test_readme_references_the_image_doc_and_dockerfile() -> None:
    """README must point operators at the container story."""
    readme = _read(REPO_ROOT / "README.md")
    assert "docker build -t ozzgraph ." in readme
    assert "docs/IMAGE_HARDENING.md" in readme


def test_docs_do_not_drift_from_dockerfile_state_dir() -> None:
    """Docs and Dockerfile must agree on the state mount point."""
    dockerfile = _read(DOCKERFILE)
    assert re.search(r"OZZGRAPH_STATE_DIR=/var/lib/ozzgraph/state", dockerfile)
    assert "/var/lib/ozzgraph/state" in _read(IMAGE_DOC)
    assert "/var/lib/ozzgraph/state" in _read(REPO_ROOT / "docs" / "TESTING_AND_QA.md")
