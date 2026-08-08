"""V10 full-regression benchmark suite for OzzGraph.

The suite (docs/BENCHMARKS.md, docs/CHANGES_v2.md milestone 10) runs a
target through BOTH harnesses — the full
:class:`~ozzgraph.runner.AutonomousRunner` (graph + security brain +
evaluator + tool plane) and the plain-ReAct baseline
(:mod:`ozzgraph.benchmarks.react`) — under a deterministic scripted
model (:mod:`ozzgraph.benchmarks.scripted`), scores every run
deterministically (:mod:`ozzgraph.benchmarks.models`), and renders a
reproducible markdown report (:mod:`ozzgraph.benchmarks.report`).

Public surface:

- :data:`~ozzgraph.benchmarks.registry.BENCHMARK_TARGETS` — the
  full-regression matrix (every lab category + the deliberate dead-end
  target).
- :func:`run_benchmark` — one target through one or both harnesses.
- :func:`run_all_benchmarks` — the full matrix, one
  :class:`~ozzgraph.benchmarks.models.BenchmarkReport`.
- :func:`render_markdown` — the deterministic report document.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ozzgraph.benchmarks.models import (
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkRun,
    HarnessKind,
    score_result,
)
from ozzgraph.benchmarks.ozzgraph_harness import run_ozzgraph_benchmark
from ozzgraph.benchmarks.react import run_react_benchmark
from ozzgraph.benchmarks.registry import (
    BENCHMARK_TARGETS,
    BenchmarkError,
    build_solve_script,
    decoy_paths_for,
    validate_targets,
)
from ozzgraph.benchmarks.report import render_markdown
from ozzgraph.benchmarks.scripted import ScriptedModel, ScriptedModelService
from ozzgraph.lab import get_target

__all__ = [
    "BENCHMARK_TARGETS",
    "BenchmarkError",
    "BenchmarkReport",
    "BenchmarkResult",
    "BenchmarkRun",
    "HarnessKind",
    "ScriptedModel",
    "ScriptedModelService",
    "assemble_report",
    "build_solve_script",
    "decoy_paths_for",
    "render_markdown",
    "run_all_benchmarks",
    "run_benchmark",
    "run_ozzgraph_benchmark",
    "run_react_benchmark",
    "score_result",
    "validate_targets",
]

#: Default cap on turns per run (docs/BENCHMARKS.md; the shipped solve
#: scripts are at most 5 actions, so the cap only bounds broken runs).
DEFAULT_MAX_TURNS = 12


async def run_benchmark(
    target_name: str,
    *,
    working_directory: Path,
    max_turns: int = DEFAULT_MAX_TURNS,
    include_react: bool = True,
    submit: bool = True,
) -> list[BenchmarkResult]:
    """Run one benchmark target through the harnesses under one model.

    Starts ONE live target instance, builds the deterministic solve
    script from its live surface (docs/BENCHMARKS.md, "Solve scripts"),
    and runs the full OzzGraph harness — and, when ``include_react``,
    the plain-ReAct baseline — against the SAME instance with fresh
    :class:`ScriptedModel` copies of the script (the ephemeral port is
    shared, so both runs probe the same live target).

    Args:
        target_name: A benchmark target name.
        working_directory: Scratch root for run state and tool actions.
        max_turns: Per-run turn cap.
        include_react: When True also run the plain-ReAct baseline
            (the comparison run).
        submit: When True the scripted model submits the flag after its
            probe script (the competent-agent ending); when False it
            never submits — the naive-loop scenario that proves
            OzzGraph finds the flag where ReAct loops.

    Returns:
        The recorded runs, OzzGraph first, then ReAct (when run).

    Raises:
        BenchmarkError: If ``target_name`` is not a benchmark target or
            its solve script cannot be derived.
    """
    validate_targets((target_name,))
    target = get_target(target_name)
    target.start()
    try:
        script = await build_solve_script(target_name, target, working_directory)
        runs: list[BenchmarkResult] = []
        runs.append(
            await run_ozzgraph_benchmark(
                target_name,
                ScriptedModel(script, target.flag, submit=submit),
                working_directory=working_directory,
                max_turns=max_turns,
                target=target,
            )
        )
        if include_react:
            runs.append(
                await run_react_benchmark(
                    target_name,
                    ScriptedModel(script, target.flag, submit=submit),
                    working_directory=working_directory,
                    max_turns=max_turns,
                    target=target,
                )
            )
        return runs
    finally:
        target.stop()


async def run_all_benchmarks(
    *,
    working_directory: Path,
    targets: Sequence[str] = BENCHMARK_TARGETS,
    max_turns: int = DEFAULT_MAX_TURNS,
    include_react: bool = True,
    submit: bool = True,
    model_id: str = "scripted",
) -> BenchmarkReport:
    """Run the full-regression matrix and build the deterministic report.

    Args:
        working_directory: Scratch root for all run state.
        targets: The target subset; defaults to
            :data:`~ozzgraph.benchmarks.registry.BENCHMARK_TARGETS`.
        max_turns: Per-run turn cap.
        include_react: When True run the plain-ReAct baseline per
            target (the comparison).
        submit: The scripted model's submit behavior (see
            :func:`run_benchmark`).
        model_id: The model identifier reported in the document.

    Returns:
        The deterministic :class:`BenchmarkReport` (scored runs plus
        the comparison summary).

    Raises:
        BenchmarkError: If any target name is unknown or a solve script
            cannot be derived.
    """
    validate_targets(targets)
    ordered = tuple(target_name for target_name in BENCHMARK_TARGETS if target_name in targets)
    results: list[BenchmarkResult] = []
    for target_name in ordered:
        results.extend(
            await run_benchmark(
                target_name,
                working_directory=working_directory,
                max_turns=max_turns,
                include_react=include_react,
                submit=submit,
            )
        )
    return assemble_report(results, targets=ordered, max_turns=max_turns, model_id=model_id)


def assemble_report(
    runs: Sequence[BenchmarkResult],
    *,
    targets: Sequence[str],
    max_turns: int,
    model_id: str,
) -> BenchmarkReport:
    """Build the deterministic report from recorded runs.

    Scores every run under ``max_turns`` and derives the comparison
    summary (solved counts per harness and per-target winner). Shared
    by :func:`run_all_benchmarks` (scripted mode) and the CLI's
    real-model path so both produce identical report semantics.
    """
    scored = [
        BenchmarkRun(result=result, score=score_result(result, max_turns=max_turns))
        for result in runs
    ]
    ozzgraph_solved = sum(
        1 for run in scored if run.result.harness is HarnessKind.OZZGRAPH and run.result.solved
    )
    react_solved = sum(
        1 for run in scored if run.result.harness is HarnessKind.REACT and run.result.solved
    )
    ozzgraph_wins = 0
    react_wins = 0
    for target_name in targets:
        ozzgraph_run = _run_for(scored, target_name, HarnessKind.OZZGRAPH)
        react_run = _run_for(scored, target_name, HarnessKind.REACT)
        if ozzgraph_run is None or react_run is None:
            continue
        if ozzgraph_run.score > react_run.score:
            ozzgraph_wins += 1
        elif react_run.score > ozzgraph_run.score:
            react_wins += 1
    return BenchmarkReport(
        model_id=model_id,
        targets=tuple(targets),
        max_turns=max_turns,
        runs=scored,
        ozzgraph_solved=ozzgraph_solved,
        react_solved=react_solved,
        ozzgraph_wins=ozzgraph_wins,
        react_wins=react_wins,
    )


def _run_for(
    runs: Sequence[BenchmarkRun], target_name: str, harness: HarnessKind
) -> BenchmarkRun | None:
    """The scored run for one (target, harness), or None."""
    for run in runs:
        if run.result.target_name == target_name and run.result.harness is harness:
            return run
    return None
