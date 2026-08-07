"""Tests for the synthetic test lab (PR27): lifecycle, registry, isolation.

Covers the lab's determinism and isolation contract
(docs/SYNTHETIC_LAB.md): every registered target starts, serves a
loopback-only value, and cleans up on stop; the registry is stable and
ordered; flags are deterministic and derived from the target; and each
flag is discoverable ONLY through its category's intended challenge
steps — never in the easy initial surface (the isolation failure-path
fixtures). HTTP discovery runs through the bounded
:class:`~ozzgraph.shell.ShellRunner` with ``curl`` where the harness
would use it, per the PR27 brief.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from ozzgraph.lab import (
    LAB_REGISTRY,
    LabError,
    SyntheticTarget,
    get_target,
    lab_flag,
    list_targets,
)
from ozzgraph.shell import ShellRunner, ToolResult

#: The registry's stable catalogue order (docs/TESTING_AND_QA.md
#: "Synthetic Challenge Suite" + docs/SYNTHETIC_LAB.md).
EXPECTED_NAMES = (
    "http-recon",
    "hidden-routes",
    "auth-logic",
    "source-vuln",
    "file-forensics",
    "binary-strings",
    "credential-reuse",
    "network-pivot",
    "multi-stage",
)

EXPECTED_CATEGORIES = (
    "HTTP reconnaissance",
    "hidden routes",
    "authentication logic",
    "source vulnerability localization",
    "file forensics",
    "binary string extraction",
    "credential reuse",
    "simple network pivot",
    "multi-stage flag discovery",
)

#: OZ{...} envelope for lab flags (docs/SYNTHETIC_LAB.md: point
#: OZZGRAPH_FLAG_PATTERN at this when the harness targets the lab).
FLAG_RE = re.compile(r"^OZ\{lab-[a-z0-9-]+-[0-9a-f]{10}\}$")


async def _curl(
    target: str,
    working_directory: Path,
    timeout_seconds: float = 10.0,
) -> ToolResult:
    """One bounded ``curl`` through the real ShellRunner (no policy).

    ``target`` is the full URL (with path); flags ride inside the
    command exactly as the harness's curl invocations carry them
    (``--max-time`` etc. inline).
    """
    command = f"curl -sS --max-time 5 {target}"
    return await ShellRunner().run(
        command=command,
        timeout_seconds=timeout_seconds,
        stdout_limit=65536,
        stderr_limit=8192,
        working_directory=working_directory,
    )


def _url(target: SyntheticTarget) -> str:
    """target_value after start, with a sanity loopback assertion."""
    value = target.target_value
    assert value.startswith("http://127.0.0.1:")
    return value


# ---------------------------------------------------------------------------
# registry determinism
# ---------------------------------------------------------------------------


def test_registry_names_and_categories_are_stable() -> None:
    """One target per suite category, in the documented order."""
    assert [target.name for target in LAB_REGISTRY] == list(EXPECTED_NAMES)
    assert [target.category for target in LAB_REGISTRY] == list(EXPECTED_CATEGORIES)


def test_list_targets_matches_registry_and_hides_flags() -> None:
    """list_targets mirrors the registry and never exposes flags."""
    info = list_targets()
    assert [entry.name for entry in info] == list(EXPECTED_NAMES)
    assert [entry.category for entry in info] == list(EXPECTED_CATEGORIES)
    for entry in info:
        assert entry.description
        assert "OZ{" not in entry.description


def test_get_target_returns_fresh_isolated_instances() -> None:
    """Each call yields a new target; instances never share state."""
    first = get_target("hidden-routes")
    second = get_target("hidden-routes")
    assert first is not second
    assert first.flag == second.flag
    assert first.started is False and second.started is False


def test_get_target_unknown_name_fails_loudly() -> None:
    """An unknown target name raises LabError listing the catalogue."""
    with pytest.raises(LabError, match="unknown synthetic target 'nope'"):
        get_target("nope")
    with pytest.raises(LabError, match="available: http-recon"):
        get_target("nope")


def test_lab_flags_are_deterministic_and_derived() -> None:
    """Flags are stable across calls and match the OZ{...} envelope."""
    assert lab_flag("http-recon") == lab_flag("http-recon")
    assert lab_flag("http-recon") != lab_flag("hidden-routes")
    for name in EXPECTED_NAMES:
        assert FLAG_RE.fullmatch(lab_flag(name)), lab_flag(name)
        assert get_target(name).flag == lab_flag(name)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_target_value_requires_start(tmp_path: Path) -> None:
    """Reading target_value before start() fails loudly."""
    target = get_target("http-recon")
    with pytest.raises(LabError, match="not started"):
        _ = target.target_value


def test_double_start_fails_loudly() -> None:
    """Starting an already-started target raises LabError."""
    target = get_target("http-recon")
    target.start()
    try:
        with pytest.raises(LabError, match="already started"):
            target.start()
    finally:
        target.stop()


@pytest.mark.parametrize("target_class", LAB_REGISTRY, ids=[t.name for t in LAB_REGISTRY])
def test_lifecycle_start_reachable_stop_clean(target_class: type[SyntheticTarget]) -> None:
    """Every target starts, serves a loopback value, and stops cleanly."""
    target = target_class()
    assert target.started is False
    target.start()
    assert target.started is True

    url = _url(target)
    response = httpx.get(url + "/", timeout=5.0)
    assert response.status_code == 200  # every target serves a root surface

    target.stop()
    assert target.started is False
    with pytest.raises(LabError, match="not started"):
        _ = target.target_value
    # the port is released: connecting now fails
    with pytest.raises(httpx.ConnectError):
        httpx.get(url + "/", timeout=2.0)


def test_context_manager_bounds_lifecycle() -> None:
    """`with target:` starts and stops the target deterministically."""
    with get_target("hidden-routes") as target:
        assert target.started is True
        assert target.target_value.startswith("http://127.0.0.1:")
    assert target.started is False


def test_stop_is_idempotent() -> None:
    """stop() twice (or stop before start) is a safe no-op."""
    target = get_target("http-recon")
    target.stop()  # never started
    target.start()
    target.stop()
    target.stop()  # already stopped


def test_file_target_cleans_up_temp_directory(tmp_path: Path) -> None:
    """File targets remove their temp tree on stop."""
    target = get_target("file-forensics")
    target.start()
    directory = target.directory  # type: ignore[attr-defined]
    assert directory.is_dir()
    target.stop()
    assert not directory.exists()


# ---------------------------------------------------------------------------
# per-category flag discovery through the bounded shell runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_recon_flag_in_response_header(tmp_path: Path) -> None:
    """The recon flag rides the X-Ozz-Lab-Flag header, never the body."""
    with get_target("http-recon") as target:
        url = _url(target)
        # -i includes the response headers in stdout, like the harness's
        # recon probes (curl -I / -v / -D -).
        headers = await ShellRunner().run(
            command=f"curl -sS --max-time 5 -i {url}/",
            timeout_seconds=10.0,
            stdout_limit=65536,
            stderr_limit=8192,
            working_directory=tmp_path,
        )
        assert target.flag in headers.stdout
        assert "X-Ozz-Lab-Flag" in headers.stdout
        body = await _curl(url + "/", tmp_path)
        assert target.flag not in body.stdout


@pytest.mark.asyncio
async def test_hidden_routes_flag_behind_admin(tmp_path: Path) -> None:
    """robots.txt advertises /admin; only /admin holds the flag."""
    with get_target("hidden-routes") as target:
        url = _url(target)
        robots = await _curl(url + "/robots.txt", tmp_path)
        assert "Disallow: /admin" in robots.stdout
        root = await _curl(url + "/", tmp_path)
        assert target.flag not in root.stdout  # isolation: never in /
        admin = await _curl(url + "/admin", tmp_path)
        assert target.flag in admin.stdout


@pytest.mark.asyncio
async def test_auth_logic_flag_after_valid_credential(tmp_path: Path) -> None:
    """Without credentials /admin is 401; with them, the flag is served."""
    with get_target("auth-logic") as target:
        url = _url(target)
        denied = await _curl(url + "/admin", tmp_path)
        assert target.flag not in denied.stdout
        assert "401" in denied.stdout or "unauthorized" in denied.stdout
        granted = await ShellRunner().run(
            command=f"curl -sS --max-time 5 -u admin:labpass {url}/admin",
            timeout_seconds=10.0,
            stdout_limit=65536,
            stderr_limit=8192,
            working_directory=tmp_path,
        )
        assert target.flag in granted.stdout
        # wrong credential stays locked
        wrong = await ShellRunner().run(
            command=f"curl -sS --max-time 5 -u admin:wrong {url}/admin",
            timeout_seconds=10.0,
            stdout_limit=65536,
            stderr_limit=8192,
            working_directory=tmp_path,
        )
        assert target.flag not in wrong.stdout


@pytest.mark.asyncio
async def test_source_vuln_flag_next_to_vulnerable_line(tmp_path: Path) -> None:
    """The flag lives in the comment adjacent to the vulnerable call."""
    with get_target("source-vuln") as target:
        url = _url(target)
        root = await _curl(url + "/", tmp_path)
        assert target.flag not in root.stdout  # isolation: not on the index
        source = await _curl(url + "/src/app.py", tmp_path)
        assert target.flag in source.stdout
        assert "os.system" in source.stdout


@pytest.mark.asyncio
async def test_file_forensics_flag_inside_non_obvious_file(tmp_path: Path) -> None:
    """The listing shows names only; the flag is inside .backup/creds.old."""
    with get_target("file-forensics") as target:
        url = _url(target)
        listing = await _curl(url + "/", tmp_path)
        assert target.flag not in listing.stdout  # isolation: listing is names only
        assert ".backup" in listing.stdout
        readme = await _curl(url + "/README.txt", tmp_path)
        assert target.flag not in readme.stdout
        creds = await _curl(url + "/.backup/creds.old", tmp_path)
        assert target.flag in creds.stdout


@pytest.mark.asyncio
async def test_binary_strings_flag_via_strings_scan(tmp_path: Path) -> None:
    """The flag is ASCII inside binary junk; only a strings scan finds it."""
    with get_target("binary-strings") as target:
        url = _url(target)
        listing = await _curl(url + "/", tmp_path)
        assert target.flag not in listing.stdout  # isolation: never in the listing
        scanned = await ShellRunner().run(
            command=f"curl -sS --max-time 5 {url}/data.bin -o data.bin && strings data.bin",
            timeout_seconds=10.0,
            stdout_limit=65536,
            stderr_limit=8192,
            working_directory=tmp_path,
        )
        assert target.flag in scanned.stdout


@pytest.mark.asyncio
async def test_credential_reuse_flag_after_reuse(tmp_path: Path) -> None:
    """The leaked credential in /backup/creds.txt unlocks /admin."""
    with get_target("credential-reuse") as target:
        url = _url(target)
        leak = await _curl(url + "/backup/creds.txt", tmp_path)
        assert "username=admin" in leak.stdout
        assert "password=labpass" in leak.stdout
        denied = await _curl(url + "/admin", tmp_path)
        assert target.flag not in denied.stdout
        granted = await ShellRunner().run(
            command=f"curl -sS --max-time 5 -u admin:labpass {url}/admin",
            timeout_seconds=10.0,
            stdout_limit=65536,
            stderr_limit=8192,
            working_directory=tmp_path,
        )
        assert target.flag in granted.stdout


@pytest.mark.asyncio
async def test_network_pivot_flag_on_second_hop(tmp_path: Path) -> None:
    """/pivot discloses the internal server; the flag lives there only."""
    with get_target("network-pivot") as target:
        url = _url(target)
        entry = await _curl(url + "/pivot", tmp_path)
        assert target.flag not in entry.stdout  # isolation: entry hop has no flag
        match = re.search(r"http://127\.0\.0\.1:\d+/flag", entry.stdout)
        assert match is not None
        internal = await _curl(match.group(0), tmp_path)
        assert target.flag in internal.stdout


@pytest.mark.asyncio
async def test_multi_stage_flag_after_two_chained_steps(tmp_path: Path) -> None:
    """stage1 reveals the stage2 path; only stage2 holds the flag."""
    with get_target("multi-stage") as target:
        url = _url(target)
        stage1 = await _curl(url + "/stage1", tmp_path)
        assert target.flag not in stage1.stdout  # isolation: stage1 has no flag
        match = re.search(r"next: (/stage2/[0-9a-f]{16})", stage1.stdout)
        assert match is not None
        stage2 = await _curl(url + match.group(1), tmp_path)
        assert target.flag in stage2.stdout
        wrong = await _curl(url + "/stage2/" + "0" * 16, tmp_path)
        assert target.flag not in wrong.stdout
        assert "not found" in wrong.stdout


def test_multi_stage_token_is_deterministic_across_instances() -> None:
    """Two multi-stage targets reveal the same stage-2 token."""
    first = get_target("multi-stage")
    second = get_target("multi-stage")
    first.start()
    second.start()
    try:
        first_path = re.search(
            r"next: (/stage2/[0-9a-f]{16})",
            httpx.get(first.target_value + "/stage1", timeout=5.0).text,
        )
        second_path = re.search(
            r"next: (/stage2/[0-9a-f]{16})",
            httpx.get(second.target_value + "/stage1", timeout=5.0).text,
        )
        assert first_path is not None and second_path is not None
        assert first_path.group(1) == second_path.group(1)
    finally:
        first.stop()
        second.stop()


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_never_in_the_wrong_place(tmp_path: Path) -> None:
    """The isolation guarantee: GET / never leaks any target's flag.

    Each target's flag is only reachable through its intended steps;
    the root surface (or the pivot entry, or stage 1) never contains
    it. This is the failure-path fixture proving the lab isolates
    flags from the initial response.
    """
    for target_class in LAB_REGISTRY:
        with target_class() as target:
            url = _url(target)
            probes = [
                url,  # root surface for HTTP targets
                url + "/robots.txt",
            ]
            if target.name == "network-pivot":
                probes = [url + "/pivot"]
            if target.name == "multi-stage":
                probes = [url + "/stage1"]
            for probe in probes:
                result = await _curl(probe, tmp_path)
                assert target.flag not in result.stdout, f"{target.name} leaked its flag at {probe}"


def test_registry_instances_are_independent() -> None:
    """Starting one target never affects another instance's state."""
    first = get_target("http-recon")
    second = get_target("http-recon")
    first.start()
    try:
        assert first.started is True
        assert second.started is False
        with pytest.raises(LabError, match="not started"):
            _ = second.target_value
    finally:
        first.stop()


def test_async_context_manager_lifecycle() -> None:
    """async with bounds the lifecycle exactly like `with`."""

    async def _run() -> tuple[str, bool]:
        async with get_target("hidden-routes") as target:
            return target.target_value, target.started

    url, started = asyncio.run(_run())
    assert started is True
    assert url.startswith("http://127.0.0.1:")
