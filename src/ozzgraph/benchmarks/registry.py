"""Benchmark target registry and per-target solve scripts (V10).

``BENCHMARK_TARGETS`` is the full-regression matrix: every lab category
from docs/TESTING_AND_QA.md "Synthetic Challenge Suite" (one benchmark
per lab target) plus the V10 deliberate-dead-end target
(:class:`~ozzgraph.lab.targets.DeadEndTarget`), which has no solve path
through its decoys — the real flag lives only at ``/flag``.

:func:`build_solve_script` derives the deterministic probe script for a
target from its LIVE instance: the commands a competent agent would run
to solve it, in order, with ``{url}`` placeholders standing for the
loopback URL. For the chained targets (``network-pivot``,
``multi-stage``) the script DISCOVERS the next hop by probing the
entry endpoint with the same bounded shell the harness would use —
the script is a pure function of the live target, so runs stay
deterministic while the model never receives information the challenge
does not expose.

The script's final probe must be the flag-bearing one: the OzzGraph
harness completes the run (objectives complete via the evaluator's
COMPLETE verdict) the moment the flag is evidenced on a plan-bound
turn, so the scripted model never needs to emit an explicit submit
against the full harness (privileged kinds are supervisor-owned and
never executed by the runner, AGENTS.md rule #5).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from ozzgraph.lab import SyntheticTarget
from ozzgraph.shell import ShellRunner

#: The full-regression matrix, in benchmark order: every lab target
#: plus the deliberate dead-end target (V10, docs/BENCHMARKS.md).
BENCHMARK_TARGETS: tuple[str, ...] = (
    "http-recon",
    "hidden-routes",
    "auth-logic",
    "source-vuln",
    "file-forensics",
    "binary-strings",
    "credential-reuse",
    "network-pivot",
    "multi-stage",
    "dead-end",
)

#: Per-target decoy paths: substrings of the routes that look promising
#: but lead nowhere (V10 dead-end proof). A run whose executed actions
#: touched one of these records ``decoy_probed``; the dead-end
#: benchmark asserts the run still completed with the REAL flag.
DECOY_PATHS: dict[str, tuple[str, ...]] = {
    "dead-end": ("/backup/flag.txt", "/admin"),
}

#: Bounded probe defaults for script discovery (the same shape the
#: harness's own probes use).
_SCRIPT_TIMEOUT_S = 10.0
_SCRIPT_STDOUT_LIMIT = 65536
_SCRIPT_STDERR_LIMIT = 8192


class BenchmarkError(RuntimeError):
    """Base error for the benchmark layer (AGENTS.md rule #9)."""


async def build_solve_script(
    target_name: str,
    target: SyntheticTarget,
    working_directory: Path,
) -> tuple[str, ...]:
    """The deterministic solve script for a live target instance.

    Args:
        target_name: A name from :data:`BENCHMARK_TARGETS`.
        target: The started target instance (its ``target_value`` is
            the loopback URL the script probes).
        working_directory: Scratch directory for the discovery probes
            of chained targets.

    Raises:
        BenchmarkError: If ``target_name`` is not a benchmark target,
            or a chained-target discovery probe fails (fail loudly,
            AGENTS.md rule #9).

    Returns:
        The script commands in order; ``{url}`` stands for the target
        URL (the scripted model substitutes the live URL).
    """
    url = target.target_value
    if target_name == "http-recon":
        # The flag rides the X-Ozz-Lab-Flag header: a plain probe, a
        # header-inclusive probe (first observation), then the
        # plan-bound header probe that completes the run.
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 -i {url}/",
            f"curl -sS --max-time 5 -D - -o /dev/null {url}/",
        )
    if target_name == "hidden-routes":
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/robots.txt",
            f"curl -sS --max-time 5 {url}/admin",
        )
    if target_name == "auth-logic":
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/admin",
            f"curl -sS --max-time 5 -u admin:labpass {url}/admin",
        )
    if target_name == "source-vuln":
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/src/app.py",
            f"curl -sS --max-time 5 '{url}/src/app.py?v=2'",
        )
    if target_name == "file-forensics":
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/.backup/",
            f"curl -sS --max-time 5 {url}/.backup/creds.old",
        )
    if target_name == "binary-strings":
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/data.bin",
            f"curl -sS --max-time 5 {url}/data.bin?raw=1",
        )
    if target_name == "credential-reuse":
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/backup/creds.txt",
            f"curl -sS --max-time 5 -u admin:labpass {url}/admin",
        )
    if target_name == "network-pivot":
        # The /pivot response embeds the internal server's BASE address
        # ("internal admin at http://127.0.0.1:PORT/flag"); the script
        # then appends the flag path itself.
        internal = await _discover(
            target_name,
            f"curl -sS --max-time 5 {url}/pivot",
            r"http://127\.0\.0\.1:\d+",
            working_directory,
        )
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/pivot",
            f"curl -sS --max-time 5 {internal}/flag",
        )
    if target_name == "multi-stage":
        stage2 = await _discover(
            target_name,
            f"curl -sS --max-time 5 {url}/stage1",
            r"/stage2/[0-9a-f]{16}",
            working_directory,
        )
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/stage1",
            f"curl -sS --max-time 5 {url}{stage2}",
        )
    if target_name == "dead-end":
        # The V10 rabbit hole: the first two probes form hypotheses, the
        # two decoy probes FAIL deterministically (--fail: 404/401 ->
        # exit 22), which refutes and abandons those hypotheses — the
        # ProgressEvaluator then pivots (every hypothesis resolved,
        # objectives incomplete), the router routes to Phase.PIVOT
        # (all_hypotheses_resolved_objectives_open), the pivot_hunt
        # skill covers the phase, and the final probe fetches the real
        # flag on a plan-bound turn, completing the run.
        return (
            f"curl -sS --max-time 5 {url}/",
            f"curl -sS --max-time 5 {url}/robots.txt",
            f"curl -sS --fail --max-time 5 {url}/backup/flag.txt",
            f"curl -sS --fail --max-time 5 {url}/admin",
            f"curl -sS --max-time 5 {url}/flag",
        )
    available = ", ".join(BENCHMARK_TARGETS)
    raise BenchmarkError(f"unknown benchmark target {target_name!r}; available: {available}")


async def _discover(
    target_name: str,
    command: str,
    pattern: str,
    working_directory: Path,
) -> str:
    """One bounded discovery probe: run ``command``, extract ``pattern``.

    Raises:
        BenchmarkError: If the probe fails or the expected next-hop
            pattern is absent (fail loudly — a script that cannot find
            its own next step would silently mislead the benchmark).
    """
    result = await ShellRunner().run(
        command=command,
        timeout_seconds=_SCRIPT_TIMEOUT_S,
        stdout_limit=_SCRIPT_STDOUT_LIMIT,
        stderr_limit=_SCRIPT_STDERR_LIMIT,
        working_directory=working_directory,
    )
    if result.exit_code != 0 or result.timeout_state:
        raise BenchmarkError(
            f"discovery probe for {target_name!r} failed: {command!r} "
            f"exit={result.exit_code} timeout={result.timeout_state}"
        )
    match = re.search(pattern, result.stdout)
    if match is None:
        raise BenchmarkError(
            f"discovery probe for {target_name!r} found no {pattern!r} in the output of {command!r}"
        )
    return match.group(0)


def decoy_paths_for(target_name: str) -> tuple[str, ...]:
    """The registered decoy paths for ``target_name`` (empty when none)."""
    return DECOY_PATHS.get(target_name, ())


def validate_targets(targets: Sequence[str]) -> None:
    """Validate a benchmark target selection.

    Raises:
        BenchmarkError: If any name is not a registered benchmark target
            (fail loudly, AGENTS.md rule #9).
    """
    for target_name in targets:
        if target_name not in BENCHMARK_TARGETS:
            raise BenchmarkError(
                f"unknown benchmark target {target_name!r}; "
                f"available: {', '.join(BENCHMARK_TARGETS)}"
            )
