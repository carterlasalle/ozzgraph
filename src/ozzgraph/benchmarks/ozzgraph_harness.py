"""The OzzGraph full-harness benchmark driver (V10).

:func:`run_ozzgraph_benchmark` runs ONE lab target through the REAL
kernel — :class:`~ozzgraph.runner.AutonomousRunner` with the security
brain, the graph, the evaluator, the scope policy, and the tool plane —
exactly as the supervisor wires it (docs/CHANGES_v2.md milestone 2's
process-level slice, driven in-process), with the scripted model
service injected so the run is hermetic and deterministic.

The run's observable outcome is scored from authoritative state only:
the runner's structured termination event (turns, status), the
executor's attempted-action events (executed commands), the security
brain's progress/hypothesis events (pivots, abandonments), the graph
(evidence count), and the artifact store (the real flag's presence).
Nothing is inferred from model prose (AGENTS.md rule #3).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.benchmarks.models import BenchmarkResult, HarnessKind
from ozzgraph.benchmarks.registry import decoy_paths_for
from ozzgraph.benchmarks.scripted import ScriptedModel, ScriptedModelService
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments import LocalEnvironment
from ozzgraph.evaluator import Evaluator
from ozzgraph.events import EventLog
from ozzgraph.lab import SyntheticTarget, get_target
from ozzgraph.matrix import LAB_FLAG_PATTERN
from ozzgraph.model_client import ModelClient, ModelService
from ozzgraph.policy import ScopePolicy
from ozzgraph.runner import RUNNER_TERMINATED, AutonomousRunner
from ozzgraph.security_brain import BRAIN_HYPOTHESIS_ABANDONED, BRAIN_PROGRESS_EVALUATED
from ozzgraph.state_graph import StateGraph

#: Budget headroom above the per-run turn cap: the runner consumes one
#: model call + one tool call per executed action; the headroom covers
#: model proposals the executor declines (privileged kinds, duplicates)
#: without letting a broken run spin forever.
_BUDGET_HEADROOM = 4

#: Wall-clock cap per benchmark run (actual runs finish in ~1-2s; the
#: cap only bounds a broken run — never an infinite loop).
_MAX_RUNTIME_S = 120

#: Event types carrying the executed command per approved action.
_EXECUTOR_ACTION_ATTEMPTED = "executor.action_attempted"

#: Event types for progress pivots and hypothesis abandonments.
_PROGRESS_EVALUATED = BRAIN_PROGRESS_EVALUATED
_HYPOTHESIS_ABANDONED = BRAIN_HYPOTHESIS_ABANDONED

#: The ``action`` entity type in the state graph.
_ENTITY_EVIDENCE = "evidence"


@dataclass(frozen=True)
class _Metrics:
    """The deterministic metrics one run exposes (typed, not a bare dict)."""

    flag_found: bool
    turns: int
    model_calls: int
    tool_calls: int
    evidence_count: int
    pivots: int
    abandoned_hypotheses: int
    decoy_probed: bool
    failure: str | None


async def run_ozzgraph_benchmark(
    target_name: str,
    model: ScriptedModel | ModelService,
    *,
    working_directory: Path,
    max_turns: int = 12,
    run_id: str = "benchmark",
    target: SyntheticTarget | None = None,
) -> BenchmarkResult:
    """Run one target through the full OzzGraph harness, hermetically.

    Args:
        target_name: A benchmark target name (docs/BENCHMARKS.md matrix).
        model: The model under evaluation — a deterministic
            :class:`ScriptedModel` (wrapped in a scripted service) or a
            real :class:`~ozzgraph.model_client.ModelService` endpoint
            (docs/BENCHMARKS.md, "Real-model runs").
        working_directory: Scratch root; the run's state (graph, events,
            artifacts) lands in a per-target subdirectory here.
        max_turns: The per-run turn cap (bounds the budgets; the runner
            terminates BUDGET_EXHAUSTED if the script does not solve).
        run_id: Run identifier recorded on every event.
        target: Optional SHARED live target instance (started by the
            caller, e.g. :func:`ozzgraph.benchmarks.run_benchmark`);
            when None a fresh instance is started and stopped here.

    Returns:
        The deterministic :class:`BenchmarkResult` (turns, model calls,
        executed tools, evidence, pivots, abandonments, decoy probes,
        flag finding, status, and the solve verdict).
    """
    start_ns = time.monotonic_ns()
    owned = target is None
    instance = target if target is not None else get_target(target_name)
    if owned:
        instance.start()
    try:
        url = instance.target_value
        flag = instance.flag
        state_dir = working_directory / f"state-{target_name}"
        config = _config(state_dir, max_turns=max_turns)
        environment = LocalEnvironment(config, environ={**os.environ, "OZZGRAPH_TARGET": url})
        service = _as_service(model)
        status = await _run_runner(config, environment, service, state_dir, run_id, max_turns)
        metrics = await _collect_metrics(
            state_dir, target_name=target_name, flag=flag, status=status
        )
    finally:
        if owned:
            instance.stop()
    return BenchmarkResult(
        target_name=target_name,
        harness=HarnessKind.OZZGRAPH,
        status=status,
        solved=metrics.flag_found and status == "completed",
        flag_found=metrics.flag_found,
        turns=metrics.turns,
        model_calls=_model_calls(service, metrics),
        tool_calls=metrics.tool_calls,
        evidence_count=metrics.evidence_count,
        pivots=metrics.pivots,
        abandoned_hypotheses=metrics.abandoned_hypotheses,
        decoy_probed=metrics.decoy_probed,
        failure=metrics.failure,
        duration_s=(time.monotonic_ns() - start_ns) / 1_000_000_000,
    )


def _as_service(model: ScriptedModel | ModelService) -> ScriptedModelService | ModelService:
    """The service-form client for the runner (wrap the callable form)."""
    if isinstance(model, ScriptedModel):
        return ScriptedModelService(model)
    return model


def _model_calls(service: ScriptedModelService | ModelService, metrics: _Metrics) -> int:
    """Model completions: exact for a scripted service, budget-derived otherwise.

    The scripted service counts every ``complete`` it served; a real
    :class:`~ozzgraph.model_client.ModelService` has no counter, so its
    completions are read from the runner's structured termination event
    (the budget's model-call accounting).
    """
    if isinstance(service, ScriptedModelService):
        return len(service.requests)
    return metrics.model_calls


def _config(state_dir: Path, *, max_turns: int) -> OzzGraphConfig:
    """The validated runtime configuration for one benchmark run.

    The allowlist is exactly the loopback address (the lab binds
    127.0.0.1 only), the flag pattern is the lab envelope, and the
    budgets are bounded by the turn cap — the same shape a real run's
    configuration takes, minus the operator identity specifics.
    """
    budget = max_turns + _BUDGET_HEADROOM
    return OzzGraphConfig(
        hal_user_id="benchmark",
        state_dir=state_dir,
        artifact_dir=state_dir / "artifacts",
        heartbeat_interval_s=300,
        max_runtime_s=_MAX_RUNTIME_S,
        max_tokens=0,
        max_model_calls=budget,
        max_tool_calls=budget,
        max_workers=2,
        max_hints=1,
        target_allowlist=("127.0.0.1",),
        flag_pattern=LAB_FLAG_PATTERN,
        max_submissions=1,
    )


async def _run_runner(
    config: OzzGraphConfig,
    environment: LocalEnvironment,
    service: ModelClient,
    state_dir: Path,
    run_id: str,
    max_turns: int,
) -> str:
    """Wire the supervisor's runner composition and run it once."""
    state_dir.mkdir(parents=True, exist_ok=True)
    event_log = EventLog.for_run(state_dir)
    artifacts = ArtifactStore.for_run(state_dir)
    budget = max_turns + _BUDGET_HEADROOM
    budgets = Budgets(
        max_tokens=0,
        max_model_calls=budget,
        max_tool_calls=budget,
        max_workers=2,
        max_hints=1,
        max_runtime_s=float(_MAX_RUNTIME_S),
    )
    policy = ScopePolicy(target_allowlist=("127.0.0.1",))
    evaluator = Evaluator(run_id=run_id, event_log=event_log)
    async with StateGraph(state_dir / "graph.db") as graph:
        runner = AutonomousRunner(
            config=config,
            graph=graph,
            event_log=event_log,
            artifacts=artifacts,
            budgets=budgets,
            environment=environment,
            model_service=service,
            evaluator=evaluator,
            run_id=run_id,
            policy=policy,
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
    return status.value


async def _collect_metrics(
    state_dir: Path,
    *,
    target_name: str,
    flag: str,
    status: str,
) -> _Metrics:
    """Derive the deterministic metrics from authoritative state."""
    events = _read_events(state_dir)
    turns = 0
    model_calls = 0
    for event in events:
        if event.get("event_type") == RUNNER_TERMINATED:
            payload = event.get("payload")
            if isinstance(payload, dict):
                raw_turns = payload.get("turns")
                if isinstance(raw_turns, int):
                    turns = raw_turns
                raw_model_calls = payload.get("model_calls")
                if isinstance(raw_model_calls, int):
                    model_calls = raw_model_calls
    tool_calls = sum(1 for event in events if event.get("event_type") == _EXECUTOR_ACTION_ATTEMPTED)
    pivots = sum(
        1
        for event in events
        if event.get("event_type") == _PROGRESS_EVALUATED
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("verdict") == "pivot"
    )
    abandoned = sum(1 for event in events if event.get("event_type") == _HYPOTHESIS_ABANDONED)
    commands: list[str] = []
    for event in events:
        if event.get("event_type") != _EXECUTOR_ACTION_ATTEMPTED:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        command = payload.get("command")
        if isinstance(command, str):
            commands.append(command)
    decoy_probed = any(
        any(decoy in command for decoy in decoy_paths_for(target_name)) for command in commands
    )
    evidence_count = await _count_evidence(state_dir)
    flag_found = _flag_in_state(state_dir, flag)
    failure = None if status == "completed" else f"status={status}"
    return _Metrics(
        flag_found=flag_found,
        turns=turns,
        model_calls=model_calls,
        tool_calls=tool_calls,
        evidence_count=evidence_count,
        pivots=pivots,
        abandoned_hypotheses=abandoned,
        decoy_probed=decoy_probed,
        failure=failure,
    )


def _read_events(state_dir: Path) -> list[dict[str, Any]]:
    """Every run-log event from ``state_dir/actions.jsonl``."""
    path = state_dir / "actions.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def _count_evidence(state_dir: Path) -> int:
    """The ``evidence`` entity count in the run's state graph."""

    async def _count() -> int:
        async with StateGraph(state_dir / "graph.db") as graph:
            return len(await graph.list_entities(_ENTITY_EVIDENCE))

    return await _count()


def _flag_in_state(state_dir: Path, flag: str) -> bool:
    """True when the real flag appears anywhere in the run's durable state.

    Scans every file under ``state_dir`` (the artifact store's raw
    outputs, the event log, the graph database) as raw bytes — the flag
    is sensitive data that lands in the artifact of the flag-bearing
    action, so a byte-level scan is the most robust deterministic
    signal and never depends on parser summaries.
    """
    target = flag.encode("utf-8")
    for path in state_dir.rglob("*"):
        if path.is_file():
            try:
                if target in path.read_bytes():
                    return True
            except OSError:  # pragma: no cover - a vanished file is not a flag
                continue
    return False
