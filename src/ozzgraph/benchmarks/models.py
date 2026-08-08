"""Benchmark records and deterministic scoring (V10 full-regression).

The V10 benchmark suite (docs/BENCHMARKS.md) records one
:class:`BenchmarkResult` per (target, harness) run and scores it with
:func:`score_result` — a pure, deterministic function of the recorded
counts, so the same run always produces the same score and the
report's comparison table is reproducible byte for byte (modulo the
lab's ephemeral loopback port, a property of the environment, not of
the harness).

Score design (documented in docs/BENCHMARKS.md, "Scoring"):

- Solving the target dominates everything: solved adds 1,000,000.
- Within a solved run, fewer turns win: each turn under the cap adds
  100 points (the cap is the run's ``max_turns``).
- Then fewer model calls: each model call under the cap adds 10.
- Then more evidence: min(evidence_count, 50) — a solved run with a
  richer evidence chain ranks higher.
- Then dead-end handling: each pivot adds 1 (bounded at 10) — pivoting
  away from decoys is a positive signal, never a penalty.

The score's purpose is the OzzGraph-vs-ReAct comparison: both harnesses
run the SAME scripted model on the SAME target, so the score difference
isolates harness behavior (graph/brain/evaluator vs a bare loop).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


#: The two harnesses the V10 suite compares (docs/BENCHMARKS.md).
#: ``ozzgraph`` is the full graph/brain/evaluator harness
#: (:class:`~ozzgraph.runner.AutonomousRunner`); ``react`` is the
#: deterministic plain-ReAct baseline (a bare propose -> execute loop
#: with no graph, no security brain, and no progress evaluation).
class HarnessKind(str, Enum):
    OZZGRAPH = "ozzgraph"
    REACT = "react"


class BenchmarkResult(BaseModel):
    """One deterministic benchmark run: a harness against one target.

    Attributes:
        target_name: The lab target name (``ozzgraph.lab`` registry).
        harness: Which harness ran (:class:`HarnessKind`).
        status: The terminal outcome — the runner's status value
            (``completed`` / ``budget_exhausted`` / ``failed``) for the
            OzzGraph harness, or ``solved`` / ``max_turns`` / ``exit``
            for the ReAct baseline.
        solved: True when the run ended with the target's REAL flag
            evidenced (OzzGraph: COMPLETED with the flag in the
            evidence chain; ReAct: a submit whose payload was the flag).
        flag_found: True when the target's real flag ever appeared in a
            tool output during the run (even when never submitted).
        turns: Loop iterations (OzzGraph: the runner's turn count;
            ReAct: model calls consumed).
        model_calls: Model completions consumed.
        tool_calls: Executed (policy-approved, non-duplicate) actions.
        evidence_count: ``evidence`` graph entities (OzzGraph only).
        pivots: ``brain.progress_evaluated`` events with a ``pivot``
            verdict (OzzGraph only) — the dead-end-resolution signal.
        abandoned_hypotheses: ``brain.hypothesis_abandoned`` events
            (OzzGraph only) — refuted hypotheses the brain dropped.
        decoy_probed: True when the run executed an action touching one
            of the target's registered decoy paths.
        failure: Loud failure detail, or None for a clean run.
        duration_s: Wall-clock seconds (diagnostics only; EXCLUDED from
            the deterministic report so reports stay reproducible).
    """

    model_config = ConfigDict(extra="forbid")

    target_name: str = Field(min_length=1)
    harness: HarnessKind
    status: str = Field(min_length=1)
    solved: bool
    flag_found: bool
    turns: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    evidence_count: int = Field(ge=0, default=0)
    pivots: int = Field(ge=0, default=0)
    abandoned_hypotheses: int = Field(ge=0, default=0)
    decoy_probed: bool = False
    failure: str | None = None
    duration_s: float = Field(ge=0.0, default=0.0)


#: Score weight for a solved run (dominates every other term).
_SOLVED_SCORE = 1_000_000
#: Points per turn saved (each turn under the cap).
_TURN_POINTS = 100
#: Points per model call saved (each call under the cap).
_MODEL_CALL_POINTS = 10
#: Cap on the evidence bonus (evidence beyond this adds nothing).
_EVIDENCE_BONUS_CAP = 50
#: Points per pivot verdict, capped.
_PIVOT_POINTS = 1
_PIVOT_BONUS_CAP = 10


def score_result(result: BenchmarkResult, *, max_turns: int = 12) -> int:
    """The deterministic score of one benchmark run.

    Args:
        result: The recorded run.
        max_turns: The run's turn cap (the denominator of the
            saved-turns term; runs are compared under the same cap).

    Returns:
        An integer score: solved dominates, then fewer turns, then
        fewer model calls, then evidence, then pivots.
    """
    score = _SOLVED_SCORE if result.solved else 0
    score += max(0, max_turns - result.turns) * _TURN_POINTS
    score += max(0, max_turns - result.model_calls) * _MODEL_CALL_POINTS
    score += min(result.evidence_count, _EVIDENCE_BONUS_CAP)
    score += min(result.pivots, _PIVOT_BONUS_CAP) * _PIVOT_POINTS
    return score


class BenchmarkRun(BaseModel):
    """One recorded run plus its deterministic score.

    Attributes:
        result: The recorded run.
        score: :func:`score_result` of ``result`` under the report's
            ``max_turns``.
    """

    model_config = ConfigDict(extra="forbid")

    result: BenchmarkResult
    score: int = Field(ge=0)


class BenchmarkReport(BaseModel):
    """The deterministic full-regression report for one benchmark pass.

    Attributes:
        model_id: The model under evaluation (``scripted`` for the
            hermetic deterministic model, or a real model id).
        targets: The evaluated targets, in benchmark order.
        max_turns: The per-run turn cap.
        runs: One scored :class:`BenchmarkRun` per (target, harness),
            in benchmark order (OzzGraph first, then ReAct when run).
        ozzgraph_solved / react_solved: Solved-run counts per harness.
        ozzgraph_wins: Targets where OzzGraph's score strictly beats
            ReAct's (the suite's headline claim; equal scores are not
            wins).
        react_wins: Targets where ReAct's score strictly beats
            OzzGraph's (a documented regression signal — must be 0 for
            the shipped scripted suite).
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1)
    targets: tuple[str, ...]
    max_turns: int = Field(ge=1)
    runs: list[BenchmarkRun]
    ozzgraph_solved: int = Field(ge=0)
    react_solved: int = Field(ge=0)
    ozzgraph_wins: int = Field(ge=0)
    react_wins: int = Field(ge=0)

    def run_for(self, target_name: str, harness: HarnessKind) -> BenchmarkResult | None:
        """The recorded run for ``target_name`` x ``harness``, or None."""
        for run in self.runs:
            if run.result.target_name == target_name and run.result.harness is harness:
                return run.result
        return None
