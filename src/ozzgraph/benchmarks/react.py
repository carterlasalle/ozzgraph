"""The plain-ReAct baseline loop (V10 comparison harness).

:func:`run_react_benchmark` is the deliberate baseline the V10 suite
compares the full OzzGraph harness against (docs/BENCHMARKS.md, "The
plain-ReAct baseline"): a bare propose -> execute loop with NO graph,
NO security brain, NO hypothesis management, and NO progress
evaluation. Each turn the model is handed the mission plus a bounded
transcript of prior turns and proposes ONE action (the same
terminal-protocol contract the runner consumes); run actions execute
through the same scope-policy gate and bounded shell, submit actions
are checked against the target's real flag, and the episode ends on a
solve, an explicit exit, or ``max_turns`` — a naive model that never
submits loops to the cap, exactly as the comparison intends.

The baseline records the same :class:`BenchmarkResult` shape as the
full harness (minus the graph-only fields), so the report compares the
two harnesses field for field on identical scripted models.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from ozzgraph.adapters import AdapterParseError, ParsedAction, TerminalAdapter
from ozzgraph.benchmarks.models import BenchmarkResult, HarnessKind
from ozzgraph.benchmarks.registry import decoy_paths_for
from ozzgraph.benchmarks.scripted import ScriptedModel
from ozzgraph.lab import SyntheticTarget, get_target
from ozzgraph.matrix import LAB_FLAG_PATTERN
from ozzgraph.model_client import ModelMessage, ModelRequest, ModelService
from ozzgraph.policy import ScopePolicy, ScopeViolationError
from ozzgraph.profiles import profile_for_model_id
from ozzgraph.shell import ShellRunner

#: Tool execution bounds for baseline turns (bounded actions, AGENTS.md
#: rule #4 — the same shape the matrix evaluator uses).
_TOOL_TIMEOUT_S = 10.0
_TOOL_STDOUT_LIMIT = 65536
_TOOL_STDERR_LIMIT = 8192

#: Cap on transcript-tail rendering per tool output, so prompts stay
#: bounded even when a tool produced a lot of output.
_TRANSCRIPT_OUTPUT_CAP = 200

#: The baseline's model form: a callable ``prompt -> completion``
#: (the scripted model or :class:`ServiceCallable` wrapping a real
#: endpoint), or a ``complete(ModelRequest)`` service — see
#: docs/BENCHMARKS.md "Real-model runs".
BaselineModel = Callable[[str], Awaitable[str]] | ScriptedModel


class ServiceCallable:
    """Adapt a :class:`~ozzgraph.model_client.ModelService` to the callable form.

    The real-model CLI path (docs/BENCHMARKS.md, "Real-model runs")
    evaluates one :class:`~ozzgraph.model_client.ModelService` against
    BOTH harnesses: the full harness consumes the service form
    directly, while the ReAct baseline consumes the callable form —
    this adapter bridges the two without duplicating the completion
    plumbing.
    """

    def __init__(self, service: ModelService, *, model_id: str = "benchmark-real") -> None:
        self._service = service
        self._model_id = model_id

    async def __call__(self, prompt: str) -> str:
        """One completion from the wrapped service."""
        response = await self._service.complete(
            ModelRequest(
                model=self._model_id,
                messages=[ModelMessage(role="user", content=prompt)],
            )
        )
        return response.choices[0].message.content or ""


async def run_react_benchmark(
    target_name: str,
    model: BaselineModel,
    *,
    working_directory: Path,
    max_turns: int = 12,
    flag_pattern: str = LAB_FLAG_PATTERN,
    target: SyntheticTarget | None = None,
) -> BenchmarkResult:
    """Run one target through the plain-ReAct baseline, hermetically.

    Args:
        target_name: A benchmark target name (docs/BENCHMARKS.md matrix).
        model: The model under evaluation — the deterministic scripted
            model, or any ``prompt -> completion`` callable.
        working_directory: Scratch directory for executed actions.
        max_turns: The episode cap; a model that never submits or exits
            loops exactly to this cap and is recorded unsolved.
        flag_pattern: The regex the run accepts as a flag (the lab
            envelope by default).
        target: Optional SHARED live target instance (started by the
            caller, e.g. :func:`ozzgraph.benchmarks.run_benchmark`);
            when None a fresh instance is started and stopped here.

    Returns:
        The deterministic :class:`BenchmarkResult` for the baseline
        (turns = model calls; solved only when a submit carried the
        real flag).
    """
    start_ns = time.monotonic_ns()
    owned = target is None
    instance = target if target is not None else get_target(target_name)
    if owned:
        instance.start()
    try:
        url = instance.target_value
        flag = instance.flag
        policy = ScopePolicy(target_allowlist=("127.0.0.1",))
        shell = ShellRunner()
        adapter = TerminalAdapter(profile_for_model_id("scripted-benchmark"))
        transcript: list[str] = []
        executed: list[str] = []
        flag_found = False
        solved = False
        terminal_status = "max_turns"
        model_calls = 0

        for turn in range(1, max_turns + 1):
            prompt = _compile_prompt(url, flag_pattern, transcript)
            completion = await _invoke(model, prompt)
            model_calls += 1
            action = _parse(adapter, completion)
            if action is None:
                transcript.append(f"TURN {turn}: invalid completion")
                continue
            if action.kind == "submit":
                claim = action.payload or ""
                solved = claim == flag
                terminal_status = "solved" if solved else "submit"
                transcript.append(f"TURN {turn}: submit {claim!r}")
                if solved:
                    break
                continue
            if action.kind == "exit":
                terminal_status = "exit"
                transcript.append(f"TURN {turn}: exit")
                break
            if action.kind != "run" or not (action.payload or "").strip():
                transcript.append(f"TURN {turn}: {action.kind}")
                continue
            command = action.payload or ""
            try:
                policy.check(command)
            except ScopeViolationError:
                transcript.append(f"TURN {turn}: run {command!r} -> REJECTED by policy")
                continue
            result = await shell.run(
                command=command,
                timeout_seconds=_TOOL_TIMEOUT_S,
                stdout_limit=_TOOL_STDOUT_LIMIT,
                stderr_limit=_TOOL_STDERR_LIMIT,
                working_directory=working_directory,
            )
            executed.append(command)
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            if flag in combined:
                flag_found = True
            transcript.append(
                f"TURN {turn}: run {command!r} -> exit {result.exit_code} "
                f"stdout: {_bounded(combined, _TRANSCRIPT_OUTPUT_CAP)}"
            )

        decoy_probed = any(
            any(decoy in command for decoy in decoy_paths_for(target_name)) for command in executed
        )
    finally:
        if owned:
            instance.stop()
    return BenchmarkResult(
        target_name=target_name,
        harness=HarnessKind.REACT,
        status=terminal_status,
        solved=solved,
        flag_found=flag_found,
        turns=model_calls,
        model_calls=model_calls,
        tool_calls=len(executed),
        decoy_probed=decoy_probed,
        failure=None,
        duration_s=(time.monotonic_ns() - start_ns) / 1_000_000_000,
    )


def _compile_prompt(url: str, flag_pattern: str, transcript: Sequence[str]) -> str:
    """The deterministic baseline prompt: mission + bounded transcript."""
    mission = (
        f"Solve the authorized CTF challenge at {url}. "
        f"Find the flag (format: {flag_pattern}) and submit it with a submit action."
    )
    tail = "\n".join(transcript) if transcript else "(none)"
    return f"{mission}\n\nEPISODE SO FAR:\n{tail}\n\nPropose exactly ONE bounded action."


async def _invoke(model: BaselineModel, prompt: str) -> str:
    """One completion from the callable form (plain text)."""
    return await model(prompt)


def _parse(adapter: TerminalAdapter, completion: str) -> ParsedAction | None:
    """Parse one completion; a malformed one is skipped (never fatal)."""
    try:
        return adapter.parse(completion)
    except AdapterParseError:
        return None


def _bounded(text: str, limit: int) -> str:
    """Deterministic truncation for transcript rendering."""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
