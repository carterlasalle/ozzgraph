"""Tests for the V10 full-regression benchmark suite (docs/BENCHMARKS.md).

Covers the benchmark harness (:mod:`ozzgraph.benchmarks`): the
full-regression matrix (every lab target + the deliberate dead-end
target), the OzzGraph full-harness run (COMPLETED with the real flag on
every target), the plain-ReAct baseline, the OzzGraph-beats-ReAct
comparison (fewer turns on the same scripted model; solves where the
baseline loops), the dead-end pivot proof (ProgressEvaluator pivots,
bounded iterations, decoy probes, real flag), deterministic scoring and
report rendering, and the CLI wiring.

All benchmarks are hermetic: the deterministic scripted model makes
zero network calls beyond the loopback lab, so the suite is
reproducible in CI (the report is byte-deterministic modulo the lab's
ephemeral port, which never enters the report).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ozzgraph.adapters import TerminalAdapter
from ozzgraph.benchmarks import (
    BENCHMARK_TARGETS,
    BenchmarkError,
    BenchmarkReport,
    HarnessKind,
    build_solve_script,
    render_markdown,
    run_all_benchmarks,
    run_benchmark,
    score_result,
)
from ozzgraph.benchmarks.models import BenchmarkResult, BenchmarkRun
from ozzgraph.benchmarks.react import run_react_benchmark
from ozzgraph.benchmarks.registry import decoy_paths_for
from ozzgraph.benchmarks.scripted import ScriptedModel
from ozzgraph.lab import LAB_REGISTRY, get_target
from ozzgraph.profiles import profile_for_model_id

#: Ports never enter the report; normalization makes comparisons robust.
_PORT_RE = re.compile(r"127\.0\.0\.1:\d+")


def _normalize(text: str) -> str:
    """Normalize ephemeral loopback ports for deterministic comparison."""
    return _PORT_RE.sub("127.0.0.1:PORT", text)


async def _run_pair(target_name: str, working_directory: Path, *, submit: bool = True):
    """One (ozzgraph, react) run pair via the shared-target harness."""
    return await run_benchmark(
        target_name,
        working_directory=working_directory,
        max_turns=12,
        include_react=True,
        submit=submit,
    )


# ---------------------------------------------------------------------------
# matrix + scripts
# ---------------------------------------------------------------------------


def test_benchmark_targets_are_registered_lab_targets() -> None:
    """Every benchmark target is a registered lab target (full matrix)."""
    registered = {target_class.name for target_class in LAB_REGISTRY}
    assert set(BENCHMARK_TARGETS) <= registered
    # The matrix covers every lab category plus the dead-end target.
    assert "dead-end" in BENCHMARK_TARGETS


def test_dead_end_has_registered_decoy_paths() -> None:
    """The dead-end target registers its rabbit-hole routes as decoys."""
    decoys = decoy_paths_for("dead-end")
    assert "/backup/flag.txt" in decoys
    assert "/admin" in decoys
    assert decoy_paths_for("hidden-routes") == ()


@pytest.mark.asyncio
async def test_solve_scripts_are_unique_and_bounded(tmp_path: Path) -> None:
    """Every script has unique commands (no duplicate fingerprints)."""
    for target_name in BENCHMARK_TARGETS:
        with get_target(target_name) as target:
            script = await build_solve_script(target_name, target, tmp_path)
        assert len(script) >= 3, target_name
        assert len(script) <= 5, target_name
        assert len(set(script)) == len(script), f"{target_name}: duplicate commands"
        assert all(command.strip() for command in script), target_name


# ---------------------------------------------------------------------------
# the full-regression runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("target_name", BENCHMARK_TARGETS)
async def test_ozzgraph_harness_solves_every_benchmark_target(
    target_name: str, tmp_path: Path
) -> None:
    """The full harness completes with the REAL flag on every target.

    The V10 full-regression contract: the runner terminates COMPLETED,
    the flag is evidenced in the run's durable state, and the run is
    solved — with at most the script length + completion-check turns
    (bounded; never a loop).
    """
    runs = await _run_pair(target_name, tmp_path)
    ozzgraph = runs[0]
    assert ozzgraph.harness is HarnessKind.OZZGRAPH
    assert ozzgraph.status == "completed", ozzgraph.failure
    assert ozzgraph.flag_found, target_name
    assert ozzgraph.solved, target_name
    assert ozzgraph.turns <= 12, target_name
    assert ozzgraph.tool_calls == ozzgraph.turns, target_name


@pytest.mark.asyncio
async def test_dead_end_benchmark_proves_pivot_and_real_flag(tmp_path: Path) -> None:
    """The dead-end run pivots away via ProgressEvaluator and still solves.

    The decoy probes (--fail on the 404/401 decoy routes) refute the
    hypotheses formed on the promising-looking paths; the security
    brain abandons them, the ProgressEvaluator records a PIVOT (every
    hypothesis resolved, objectives incomplete), and the run then finds
    the REAL flag at /flag — bounded iterations, no infinite loop, and
    the decoy was genuinely probed along the way.
    """
    runs = await _run_pair("dead-end", tmp_path)
    ozzgraph = runs[0]
    assert ozzgraph.status == "completed"
    assert ozzgraph.solved, "the real flag must be found after the dead ends"
    assert ozzgraph.decoy_probed, "the benchmark must actually probe the decoys"
    assert ozzgraph.pivots >= 1, "the ProgressEvaluator must record a pivot"
    assert ozzgraph.abandoned_hypotheses >= 1, "dead-end hypotheses must be abandoned"
    assert ozzgraph.turns <= 12, "bounded iterations — no infinite loop"


# ---------------------------------------------------------------------------
# plain-ReAct baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_baseline_solves_with_submit(tmp_path: Path) -> None:
    """The baseline solves when the model explicitly submits the flag."""
    runs = await _run_pair("hidden-routes", tmp_path)
    react = runs[1]
    assert react.harness is HarnessKind.REACT
    assert react.status == "solved"
    assert react.solved
    assert react.flag_found


@pytest.mark.asyncio
async def test_react_baseline_loops_without_submit(tmp_path: Path) -> None:
    """A model that never submits loops to the cap — the baseline's limit.

    The naive scripted model keeps re-probing the surface; without the
    harness's objective completion the baseline cannot recognize the
    flag it already saw, so it burns every turn to ``max_turns`` and is
    recorded unsolved (bounded — never an infinite loop).
    """
    runs = await _run_pair("hidden-routes", tmp_path, submit=False)
    react = runs[1]
    assert react.status == "max_turns"
    assert not react.solved
    assert react.flag_found  # the flag WAS observed — the loop just never ends
    assert react.turns == 12
    # The OzzGraph run of the same non-submitting model still completes:
    # the harness completes the objective on the flag evidence, not on
    # the model's own decision to stop.
    ozzgraph = runs[0]
    assert ozzgraph.status == "completed"
    assert ozzgraph.solved


# ---------------------------------------------------------------------------
# OzzGraph vs ReAct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("target_name", BENCHMARK_TARGETS)
async def test_ozzgraph_beats_react_on_every_target(target_name: str, tmp_path: Path) -> None:
    """On the same scripted model, OzzGraph scores strictly better.

    The suite's headline claim: the full harness completes the run the
    moment the flag is evidenced on a plan-bound turn (objectives
    complete via the evaluator), while the baseline must keep calling
    the model until it happens to emit a submit — so OzzGraph uses no
    more turns and fewer model calls on every benchmark target, and
    scores strictly higher. (LOCAL-PHASE-GAP: OzzGraph may spend one
    turn on the deterministic zero-LLM service-characterize action the
    bare baseline never performs, so turns may tie; the model-call and
    score comparisons stay strict.)
    """
    runs = await _run_pair(target_name, tmp_path)
    ozzgraph, react = runs
    assert ozzgraph.solved and react.solved
    assert ozzgraph.turns <= react.turns, target_name
    assert ozzgraph.model_calls < react.model_calls, target_name
    assert score_result(ozzgraph) > score_result(react)


@pytest.mark.asyncio
async def test_ozzgraph_finds_flag_where_react_loops(tmp_path: Path) -> None:
    """The dead-end target: OzzGraph solves where the baseline loops.

    With a naive model that never submits, the baseline burns its whole
    turn cap unsolved (it cannot recognize the dead ends), while the
    full harness pivots away from the decoys and completes with the
    real flag — the strongest form of the comparison.
    """
    runs = await _run_pair("dead-end", tmp_path, submit=False)
    ozzgraph, react = runs
    assert not react.solved
    assert react.status == "max_turns"
    assert ozzgraph.solved
    assert ozzgraph.status == "completed"


# ---------------------------------------------------------------------------
# determinism, scoring, report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_benchmark_report_is_deterministic(tmp_path: Path) -> None:
    """Two full-matrix passes produce identical normalized reports."""
    first = await run_all_benchmarks(
        working_directory=tmp_path / "a", max_turns=12, include_react=True
    )
    second = await run_all_benchmarks(
        working_directory=tmp_path / "b", max_turns=12, include_react=True
    )
    assert _normalize(render_markdown(first)) == _normalize(render_markdown(second))


def test_score_result_orders_solves_and_turns() -> None:
    """Solved dominates; then fewer turns; then fewer model calls."""
    base = BenchmarkResult(
        target_name="t",
        harness=HarnessKind.OZZGRAPH,
        status="completed",
        solved=True,
        flag_found=True,
        turns=3,
        model_calls=3,
        tool_calls=3,
        evidence_count=3,
    )
    assert score_result(base) > score_result(base.model_copy(update={"solved": False}))
    faster = base.model_copy(update={"turns": 2, "model_calls": 2})
    assert score_result(faster) > score_result(base)
    more_evidence = base.model_copy(update={"evidence_count": 40})
    assert score_result(more_evidence) > score_result(base)
    pivoted = base.model_copy(update={"pivots": 3})
    assert score_result(pivoted) > score_result(base)


def test_render_markdown_contains_table_and_verdict() -> None:
    """The report renders the matrix table and the comparison verdict."""
    report = BenchmarkReport(
        model_id="scripted",
        targets=("hidden-routes",),
        max_turns=12,
        runs=[
            BenchmarkRun(
                result=BenchmarkResult(
                    target_name="hidden-routes",
                    harness=HarnessKind.OZZGRAPH,
                    status="completed",
                    solved=True,
                    flag_found=True,
                    turns=3,
                    model_calls=3,
                    tool_calls=3,
                    evidence_count=3,
                ),
                score=1000012,
            ),
            BenchmarkRun(
                result=BenchmarkResult(
                    target_name="hidden-routes",
                    harness=HarnessKind.REACT,
                    status="solved",
                    solved=True,
                    flag_found=True,
                    turns=4,
                    model_calls=4,
                    tool_calls=3,
                ),
                score=1000001,
            ),
        ],
        ozzgraph_solved=1,
        react_solved=1,
        ozzgraph_wins=1,
        react_wins=0,
    )
    document = render_markdown(report)
    assert "| target | harness |" in document
    assert "| hidden-routes | ozzgraph | completed | yes | 3 |" in document
    assert "OzzGraph solves every benchmark target" in document
    assert "1,000,000" in document  # the scoring rules are documented


def test_scripted_model_emits_parseable_terminal_actions() -> None:
    """The scripted model's completions satisfy the terminal adapter."""

    async def _check() -> None:
        model = ScriptedModel(
            ("curl -sS --max-time 5 {url}/", "curl -sS --max-time 5 {url}/admin"),
            "OZ{lab-hidden-routes-0000000000}",
            submit=True,
        )
        adapter = TerminalAdapter(profile_for_model_id("scripted-benchmark"))
        first = await model("mission http://127.0.0.1:1/ context")
        assert adapter.parse(first).kind == "run"
        assert adapter.parse(first).payload == "curl -sS --max-time 5 http://127.0.0.1:1/"
        second = await model("mission http://127.0.0.1:1/ context")
        assert adapter.parse(second).payload == "curl -sS --max-time 5 http://127.0.0.1:1/admin"
        third = await model("mission http://127.0.0.1:1/ context")
        parsed = adapter.parse(third)
        assert parsed.kind == "submit"
        assert parsed.payload == "OZ{lab-hidden-routes-0000000000}"

    asyncio.run(_check())


# ---------------------------------------------------------------------------
# failure paths + CLI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_benchmark_target_fails_loudly(tmp_path: Path) -> None:
    """An unknown target name raises before any run starts."""
    with pytest.raises(BenchmarkError, match="unknown benchmark target"):
        await run_benchmark("nope", working_directory=tmp_path)


@pytest.mark.asyncio
async def test_react_baseline_respects_turn_cap(tmp_path: Path) -> None:
    """The baseline loop is bounded by max_turns — never infinite."""
    model = ScriptedModel(("curl -sS --max-time 5 {url}/",), "x", submit=False)
    with get_target("hidden-routes") as target:
        result = await run_react_benchmark(
            "hidden-routes",
            model,
            working_directory=tmp_path,
            max_turns=5,
            target=target,
        )
    assert result.turns == 5
    assert result.status == "max_turns"
    assert not result.solved


def test_benchmark_cli_help() -> None:
    """``ozzgraph benchmark --help`` exits 0 and documents the flags."""
    result = subprocess.run(
        [sys.executable, "-m", "ozzgraph", "benchmark", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert "--react" in result.stdout
    assert "--target" in result.stdout
    assert "--max-turns" in result.stdout


def test_benchmark_cli_runs_single_target(tmp_path: Path) -> None:
    """``ozzgraph benchmark --target X --react`` produces a report, exit 0."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ozzgraph",
            "benchmark",
            "--target",
            "hidden-routes",
            "--react",
            "--out",
            str(tmp_path / "report.md"),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "benchmark report written to" in result.stdout
    document = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "# OzzGraph benchmark report" in document
    assert "hidden-routes" in document
    assert "ozzgraph" in document and "react" in document
    assert "1,000,000" in document
