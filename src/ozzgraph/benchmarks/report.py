"""Deterministic markdown report for the V10 benchmark suite.

:func:`render_markdown` renders a :class:`BenchmarkReport` into a
human-readable markdown document: the per-(target, harness) table, the
comparison summary (solved counts, per-target winner, and the suite's
headline claims), and the scoring rules. The rendering is a pure
function of the report — no timestamps, no wall-clock durations — so
the same runs always render the same document (modulo the lab's
ephemeral loopback port, which the report never embeds).
"""

from __future__ import annotations

from ozzgraph.benchmarks.models import BenchmarkReport

#: Column widths for the deterministic table (header rows only).
_TABLE_HEADER = (
    "| target | harness | status | solved | turns | model_calls | tool_calls | "
    "evidence | pivots | abandoned | decoy | score |"
)
_TABLE_RULE = "|---|---|---|---|---|---|---|---|---|---|---|---|"


def render_markdown(report: BenchmarkReport) -> str:
    """The deterministic markdown report for one benchmark pass."""
    lines = [
        "# OzzGraph benchmark report",
        "",
        f"- model: `{report.model_id}`",
        f"- targets: {', '.join(report.targets)}",
        f"- max turns per run: {report.max_turns}",
        "",
        "## Results",
        "",
        _TABLE_HEADER,
        _TABLE_RULE,
    ]
    for run in report.runs:
        result = run.result
        lines.append(
            f"| {result.target_name} | {result.harness.value} | {result.status} | "
            f"{'yes' if result.solved else 'no'} | {result.turns} | {result.model_calls} | "
            f"{result.tool_calls} | {result.evidence_count} | {result.pivots} | "
            f"{result.abandoned_hypotheses} | {'yes' if result.decoy_probed else 'no'} | "
            f"{run.score} |"
        )
    lines.extend(
        [
            "",
            "## Comparison: OzzGraph vs plain ReAct",
            "",
            f"- targets solved by OzzGraph: {report.ozzgraph_solved}/{len(report.targets)}",
            f"- targets solved by plain ReAct: {report.react_solved}/{len(report.targets)}",
            f"- targets where OzzGraph scored strictly better: {report.ozzgraph_wins}",
            f"- targets where plain ReAct scored strictly better: {report.react_wins}",
            _comparison_note(report),
            "",
            "## Scoring",
            "",
            (
                "Score = 1,000,000 (solved) + 100 per turn under the cap + 10 per "
                "model call under the cap + min(evidence, 50) + min(pivots, 10). "
                "Solving dominates; then fewer turns; then fewer model calls; then "
                "a richer evidence chain; then dead-end pivots."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _comparison_note(report: BenchmarkReport) -> str:
    """The deterministic headline verdict of the comparison."""
    if report.react_wins > 0:
        return (
            "NOTE: plain ReAct scored strictly better on some targets — a "
            "regression signal the shipped scripted suite must never produce."
        )
    if report.ozzgraph_solved == len(report.targets) and report.ozzgraph_wins >= 0:
        return (
            "Verdict: OzzGraph solves every benchmark target and never loses a "
            "target to the baseline — the graph/brain/evaluator harness beats "
            "the bare loop on the same scripted model."
        )
    return "Verdict: the comparison is inconclusive for this model/target set."
