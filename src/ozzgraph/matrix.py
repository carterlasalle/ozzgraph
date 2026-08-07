"""Model-harness matrix evaluator for OzzGraph (PR28).

Implements the "Model-Harness Matrix" section of docs/TESTING_AND_QA.md:
an evaluation harness that runs ONE model client against the harness
protocols (reusing the :mod:`ozzgraph.adapters` concrete adapters and
:func:`~ozzgraph.profiles.probe_protocol` for protocol detection) and
the synthetic lab targets (:mod:`ozzgraph.lab`), recording every
interaction deterministically and computing the nine documented metrics
per model x protocol.

Design rules (AGENTS.md):

- Deterministic: the episode loop is a pure function of the client and
  the target — the same client always produces the same recorded
  interactions and the same :class:`TraceMetrics` (the only
  non-determinism in a real model is the model itself; a scripted
  client reproduces identical reports byte for byte). Every metric is a
  pure function of the recorded interactions
  (:func:`aggregate_metrics`).

- No network beyond the loopback lab: tool actions run through the
  bounded :class:`~ozzgraph.shell.ShellRunner` and the fail-closed
  :class:`~ozzgraph.policy.ScopePolicy` gate (with ``127.0.0.1``
  allowlisted for the lab), exactly as the harness would execute them —
  a command the policy rejects is never executed and counts as an
  incorrect tool selection.

- Fail loudly (AGENTS.md rule #9): an unknown target raises
  :class:`~ozzgraph.lab.LabError`, an unknown protocol raises
  :class:`~ozzgraph.adapters.AdapterRegistryError`, and an invalid flag
  pattern raises :class:`MatrixError` — nothing is silently skipped.

- The metrics are the golden-trace contract: :class:`TraceMetrics` (in
  :mod:`ozzgraph.traces`) is exactly what a golden trace stores as its
  expected metrics, so a matrix run can be captured into a trace and
  re-verified deterministically.

Metrics (definitions in docs/GOLDEN_TRACES.md, "Metrics"; all computed
from the recorded interactions):

- valid-output rate: completions that yielded a usable action (parsed
  on the first attempt, or recovered via the adapter's repair strategy)
  divided by all completions.
- correct tool selection: run actions the scope-policy gate approved
  (the harness would execute them) divided by all run actions; 1.0 when
  there are none.
- repetition rate: run actions whose fingerprint was already attempted
  in the same episode divided by all run actions; 0.0 when none.
- recovery rate: first-attempt parse failures the adapter's repair
  strategy turned into a usable action, divided by all first-attempt
  failures; 1.0 when there were none.
- output tokens per decision: total completion tokens divided by the
  number of completions; 0.0 when none.
- steps per objective: total turns across episodes divided by the
  number of episodes (one objective per episode); 0.0 when none.
- solve rate: episodes that submitted the target's flag divided by all
  episodes; 0.0 when none.
- unsupported-fact rate: flag-shaped claims (in rationales or non-submit
  payloads) that never appeared in any prior tool output, divided by all
  such claims (AGENTS.md rule #3: a fact requires deterministic
  evidence); 0.0 when none.
- unsupported-flag rate: submit actions whose payload is not the
  target's flag divided by all submit actions; 0.0 when none.
"""

from __future__ import annotations

import math
import re
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.adapters import (
    AdapterParseError,
    ModelAdapter,
    adapter_for,
)
from ozzgraph.config import DEFAULT_FLAG_PATTERN
from ozzgraph.lab import get_target
from ozzgraph.model_client import ModelMessage, ModelRequest, ModelResponse
from ozzgraph.policy import ScopePolicy, ScopeViolationError
from ozzgraph.profiles import (
    PROTOCOL_JSON,
    PROTOCOL_TERMINAL,
    PROTOCOL_THREE_LINE,
    probe_protocol,
    profile_for_model_id,
)
from ozzgraph.shell import ShellRunner
from ozzgraph.traces import TraceMetrics, TraceToolOutput

#: The harness protocols the matrix evaluates by default: terminal,
#: three-line, and JSON. ``function_call`` has no concrete adapter (it
#: requires explicit, evidenced registration, AGENTS.md "never assume
#: function-call support"), so it is only evaluated when a caller
#: registers an adapter and passes it explicitly via ``protocols``.
MATRIX_PROTOCOLS: tuple[str, ...] = (
    PROTOCOL_TERMINAL,
    PROTOCOL_THREE_LINE,
    PROTOCOL_JSON,
)

#: Default deterministic target subset for a matrix run (the lab's
#: registry order; callers pass the full catalogue when they want it).
DEFAULT_TARGETS: tuple[str, ...] = ("http-recon", "hidden-routes")

#: Default cap on turns per episode (each turn is one completion).
DEFAULT_MAX_TURNS = 12

#: The lab flag envelope, for ``flag_pattern`` when evaluating against
#: the synthetic lab (docs/SYNTHETIC_LAB.md).
LAB_FLAG_PATTERN = r"OZ\{[^{}\s]+\}"

#: Action kinds the harness supports; a parsed action of any other kind
#: is not a usable output (the executor's policy layer rejects unknown
#: kinds the same way).
SUPPORTED_KINDS: frozenset[str] = frozenset({"run", "think", "submit", "hint", "exit"})

#: Skill summaries advertised in every matrix prompt (fixed, so prompts
#: are deterministic across runs).
MATRIX_SKILLS: tuple[str, ...] = (
    "recon — bounded curl/wget probes of the target",
    "fetch — download files from the target for analysis",
    "inspect — strings/analysis of downloaded artifacts",
    "submit — submit a flag candidate for validation",
)

#: The fixed probe prompt (sent once per model, before any protocol
#: episode): the model's own preferred output format is classified with
#: :func:`~ozzgraph.profiles.probe_protocol`.
MATRIX_PROBE_PROMPT = (
    "PROBE: respond with a short plain-text line describing your preferred output format."
)

#: Graph summary block compiled into every matrix prompt (the matrix
#: evaluates the model/protocol layer, not the graph layer).
_MATRIX_GRAPH_SUMMARY = "(no graph state — harness-matrix evaluation)"

#: Output contract compiled into every matrix prompt.
_MATRIX_OUTPUT_CONTRACT = (
    "Return exactly one action proposal in the format specified above. "
    "Supported action kinds: run, think, submit, hint, exit."
)

#: Tool execution bounds for matrix episodes (bounded actions, AGENTS.md
#: rule #4).
_TOOL_TIMEOUT_S = 10.0
_TOOL_STDOUT_LIMIT = 4096
_TOOL_STDERR_LIMIT = 2048

#: Cap on transcript-tail rendering per tool output, so prompts stay
#: bounded even when a tool produced a lot of output.
_TRANSCRIPT_OUTPUT_CAP = 200


class MatrixError(RuntimeError):
    """Base error for the matrix layer (AGENTS.md rule #9)."""


class MatrixCompletion(BaseModel):
    """One completion from the model under evaluation.

    Attributes:
        text: The raw completion text.
        prompt_tokens: Prompt token accounting (0 when the client form
            does not provide usage).
        completion_tokens: Completion token accounting; when the client
            form returns plain text, :func:`estimate_tokens` provides a
            deterministic approximation.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    prompt_tokens: int = Field(ge=0, default=0)
    completion_tokens: int = Field(ge=0, default=0)


@runtime_checkable
class _ServiceForm(Protocol):
    """The :class:`~ozzgraph.model_client.ModelService`-like client form."""

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


#: A model under evaluation: either the callable form (``prompt ->
#: completion``, returning a ``str`` or a :class:`MatrixCompletion`), or
#: the service form (an object with an async ``complete(ModelRequest)``
#: method, e.g. :class:`~ozzgraph.model_client.ModelService`).
MatrixClient = Callable[[str], Awaitable[str | MatrixCompletion]] | _ServiceForm


def estimate_tokens(text: str) -> int:
    """Deterministic token approximation for plain-text completions.

    Approximately 4 characters per token (the common heuristic), at
    least 1. Only used when the client form returns plain text without
    usage accounting, so ``output_tokens_per_decision`` stays
    deterministic for scripted clients.
    """
    return max(1, math.ceil(len(text) / 4))


class EpisodeStep(BaseModel):
    """One recorded turn of a matrix episode.

    Attributes:
        turn: 1-based turn index within the episode.
        target_name: The lab target the episode ran against.
        protocol: The protocol family being evaluated.
        completion: The raw model completion.
        completion_tokens: Completion token accounting for this turn.
        parsed: True when the completion (after repair) yielded a usable
            supported-kind action.
        recovered: True when ``parsed`` was reached through the
            adapter's repair strategy.
        kind: The parsed action kind (None when not parsed).
        payload: The parsed action payload (None when not parsed or no
            payload).
        rationale: The parsed action rationale (None when not parsed).
        command: The command text for a run action (the payload).
        policy_approved: True when the run action passed the scope
            policy gate (and was executed).
        tool: The recorded tool output for an executed run action.
        repeated: True when the run action's fingerprint was already
            attempted in this episode.
        fact_claim_count: Flag-shaped strings claimed in this turn's
            rationale / non-submit payload.
        unsupported_fact_claim_count: Of the above, claims that never
            appeared in any prior tool output.
        unsupported_flag: True when a submit action's payload is not the
            target's flag.
        solved: True when the turn submitted the target's flag.
    """

    model_config = ConfigDict(extra="forbid")

    turn: int = Field(ge=1)
    target_name: str
    protocol: str
    completion: str
    completion_tokens: int = Field(ge=0)
    parsed: bool = False
    recovered: bool = False
    kind: str | None = None
    payload: str | None = None
    rationale: str | None = None
    command: str | None = None
    policy_approved: bool = False
    tool: TraceToolOutput | None = None
    repeated: bool = False
    fact_claim_count: int = Field(ge=0, default=0)
    unsupported_fact_claim_count: int = Field(ge=0, default=0)
    unsupported_flag: bool = False
    solved: bool = False


class EpisodeRecord(BaseModel):
    """One complete episode: a protocol against one target.

    Attributes:
        target_name: The lab target this episode ran against.
        protocol: The protocol family evaluated.
        solved: True when the episode submitted the target's flag.
        discovered_flag: True when a tool output contained the target's
            flag (even if it was never submitted).
        steps: The recorded turns, in order.
    """

    model_config = ConfigDict(extra="forbid")

    target_name: str
    protocol: str
    solved: bool
    discovered_flag: bool
    steps: list[EpisodeStep]


class MatrixProbe(BaseModel):
    """The model's protocol probe result (before any episode).

    Attributes:
        model_id: The model under evaluation.
        sample: The model's raw probe completion.
        detected_protocol: :func:`~ozzgraph.profiles.probe_protocol`
            classification of the sample (None when ambiguous).
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    sample: str
    detected_protocol: str | None


class MatrixRow(BaseModel):
    """One model x protocol row of the matrix report.

    Attributes:
        model_id: The model under evaluation.
        protocol: The protocol family evaluated.
        episodes: Number of episodes (targets) run.
        steps: Total turns across the episodes.
        completions: Total completions (equals ``steps``).
        solved_episodes: Episodes that submitted the target's flag.
        total_completion_tokens: Sum of completion tokens.
        metrics: The nine metrics for this row.
        interactions: The recorded turns, in order (the raw material
            every metric derives from; also usable for golden-trace
            capture).
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    protocol: str
    episodes: int = Field(ge=0)
    steps: int = Field(ge=0)
    completions: int = Field(ge=0)
    solved_episodes: int = Field(ge=0)
    total_completion_tokens: int = Field(ge=0)
    metrics: TraceMetrics
    interactions: list[EpisodeStep]


class MatrixReport(BaseModel):
    """The full matrix report for one model.

    Attributes:
        model_id: The model under evaluation.
        probe: The protocol probe result.
        rows: One :class:`MatrixRow` per evaluated protocol, in the
            requested protocol order.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    probe: MatrixProbe
    rows: list[MatrixRow]

    def row_for(self, protocol: str) -> MatrixRow | None:
        """The row for ``protocol``, or None when not evaluated."""
        for row in self.rows:
            if row.protocol == protocol:
                return row
        return None


def _parse_with_repair(
    adapter: ModelAdapter, completion: str
) -> tuple[bool, bool, str | None, str | None, str | None]:
    """Parse one completion into a usable action, applying repair once.

    Returns ``(parsed, recovered, kind, payload, rationale)`` where
    ``parsed`` is True only when the final action's kind is in
    :data:`SUPPORTED_KINDS` (an unknown kind is not a usable output —
    the executor's policy layer rejects unknown kinds the same way), and
    ``recovered`` is True when parsing succeeded only after the
    adapter's repair strategy. Never raises.
    """
    try:
        action = adapter.parse(completion)
    except AdapterParseError as exc:
        repaired_text = adapter.repair(completion, exc)
        if repaired_text is None:
            return False, False, None, None, None
        try:
            action = adapter.parse(repaired_text)
        except AdapterParseError:
            return False, False, None, None, None
        if action.kind not in SUPPORTED_KINDS:
            return False, False, action.kind, None, None
        return True, True, action.kind, action.payload, action.rationale
    if action.kind not in SUPPORTED_KINDS:
        return False, False, action.kind, None, None
    return True, False, action.kind, action.payload, action.rationale


def _flag_claims(text: str | None, flag_re: re.Pattern[str]) -> int:
    """Number of flag-shaped strings in ``text`` (0 for None/empty)."""
    if not text:
        return 0
    return len(flag_re.findall(text))


def _mission(target_value: str, flag_pattern: str) -> str:
    """The deterministic mission block for one episode."""
    return (
        f"Solve the authorized CTF challenge at {target_value}. "
        f"Find the flag (format: {flag_pattern}) and submit it with a submit action."
    )


def _transcript_tail(steps: Sequence[EpisodeStep]) -> str:
    """Deterministic bounded rendering of the episode so far."""
    if not steps:
        return "(none)"
    lines: list[str] = []
    for step in steps:
        if step.kind == "run" and step.tool is not None:
            stdout = " ".join(step.tool.stdout.split())[:_TRANSCRIPT_OUTPUT_CAP]
            lines.append(
                f"TURN {step.turn}: run {step.tool.command!r} -> exit {step.tool.exit_code} "
                f"stdout: {stdout}"
            )
        elif step.kind == "run" and step.command is not None:
            lines.append(f"TURN {step.turn}: run {step.command!r} -> REJECTED by policy")
        elif step.kind == "submit" and step.payload is not None:
            lines.append(f"TURN {step.turn}: submit {step.payload!r}")
        else:
            lines.append(f"TURN {step.turn}: {step.kind if step.kind is not None else 'invalid'}")
    return "\n".join(lines)


def _compile_prompt(
    adapter: ModelAdapter, *, target_value: str, flag_pattern: str, steps: Sequence[EpisodeStep]
) -> str:
    """Compile the deterministic episode prompt for one turn."""
    return adapter.compile_prompt(
        mission=_mission(target_value, flag_pattern),
        graph_summary=_MATRIX_GRAPH_SUMMARY,
        transcript_tail=_transcript_tail(steps),
        skills=MATRIX_SKILLS,
        output_contract=_MATRIX_OUTPUT_CONTRACT,
    )


async def _invoke(client: MatrixClient, prompt: str, *, model_id: str) -> MatrixCompletion:
    """One completion from ``client`` in either supported form.

    The service form (an object with an async ``complete`` method,
    e.g. :class:`~ozzgraph.model_client.ModelService`) is called with a
    normalized :class:`ModelRequest` and its usage accounting is kept;
    the callable form (``prompt -> completion``) is called with the raw
    prompt, and plain-text results get a deterministic token estimate.
    """
    if isinstance(client, _ServiceForm):
        response = await client.complete(
            ModelRequest(
                model=model_id,
                messages=[ModelMessage(role="user", content=prompt)],
            )
        )
        content = response.choices[0].message.content or ""
        return MatrixCompletion(
            text=content,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
    result = await client(prompt)
    if isinstance(result, str):
        return MatrixCompletion(text=result, completion_tokens=estimate_tokens(result))
    return result


async def _run_episode(
    *,
    adapter: ModelAdapter,
    client: MatrixClient,
    model_id: str,
    target_value: str,
    target_name: str,
    flag: str,
    flag_pattern: str,
    max_turns: int,
    working_directory: Path,
) -> EpisodeRecord:
    """Run one deterministic episode: a protocol against one target.

    Each turn compiles the prompt (mission + bounded transcript),
    completes the client, parses (with one repair attempt), and — for a
    run action — gates the command through the scope policy and executes
    it with the bounded shell runner. The episode ends when the correct
    flag is submitted, the model exits, or ``max_turns`` is reached.
    """
    policy = ScopePolicy(target_allowlist=("127.0.0.1",))
    runner = ShellRunner()
    flag_re = re.compile(flag_pattern)
    steps: list[EpisodeStep] = []
    seen_fingerprints: set[str] = set()
    observed_output = ""
    solved = False
    discovered_flag = False

    for turn in range(1, max_turns + 1):
        prompt = _compile_prompt(
            adapter, target_value=target_value, flag_pattern=flag_pattern, steps=steps
        )
        completion = await _invoke(client, prompt, model_id=model_id)
        parsed, recovered, kind, payload, rationale = _parse_with_repair(adapter, completion.text)
        step = EpisodeStep(
            turn=turn,
            target_name=target_name,
            protocol=adapter.protocol,
            completion=completion.text,
            completion_tokens=completion.completion_tokens,
            parsed=parsed,
            recovered=recovered,
            kind=kind,
            payload=payload,
            rationale=rationale,
        )

        if parsed and kind == "run":
            command = payload if payload is not None else ""
            step = step.model_copy(update={"command": command})
            try:
                decision = policy.check(command)
            except ScopeViolationError:
                step = step.model_copy(update={"policy_approved": False})
            else:
                repeated = decision.fingerprint in seen_fingerprints
                seen_fingerprints.add(decision.fingerprint)
                result = await runner.run(
                    command=command,
                    timeout_seconds=_TOOL_TIMEOUT_S,
                    stdout_limit=_TOOL_STDOUT_LIMIT,
                    stderr_limit=_TOOL_STDERR_LIMIT,
                    working_directory=working_directory,
                )
                combined = result.stdout + result.stderr
                if flag in combined:
                    discovered_flag = True
                step = step.model_copy(
                    update={
                        "policy_approved": True,
                        "repeated": repeated,
                        "tool": TraceToolOutput(
                            turn=turn,
                            command=command,
                            exit_code=result.exit_code,
                            stdout=result.stdout,
                            stderr=result.stderr,
                            timeout_state=result.timeout_state,
                        ),
                    }
                )
        elif parsed and kind == "submit":
            claim = payload if payload is not None else ""
            step = step.model_copy(
                update={
                    "unsupported_flag": claim != flag,
                    "solved": claim == flag,
                }
            )

        claims = 0
        unsupported = 0
        for source in (rationale, payload if kind != "submit" else None):
            for match in flag_re.findall(source if source else ""):
                claims += 1
                if match not in observed_output:
                    unsupported += 1
        step = step.model_copy(
            update={
                "fact_claim_count": claims,
                "unsupported_fact_claim_count": unsupported,
            }
        )
        if step.tool is not None:
            observed_output += step.tool.stdout + step.tool.stderr
        if step.solved:
            solved = True
        steps.append(step)
        if solved or kind == "exit":
            break

    return EpisodeRecord(
        target_name=target_name,
        protocol=adapter.protocol,
        solved=solved,
        discovered_flag=discovered_flag,
        steps=steps,
    )


def aggregate_metrics(records: Sequence[EpisodeRecord]) -> TraceMetrics:
    """The nine metrics, computed deterministically from the records.

    Definitions match docs/GOLDEN_TRACES.md, "Metrics" (and the module
    docstring). Every rate is a pure function of the recorded
    interactions, so the same records always produce the same metrics —
    the golden-trace comparison contract is exact float equality.
    """
    steps = [step for record in records for step in record.steps]
    completions = len(steps)
    first_parses = sum(1 for step in steps if step.parsed and not step.recovered)
    recovered = sum(1 for step in steps if step.recovered)
    first_failures = completions - first_parses

    run_steps = [step for step in steps if step.kind == "run"]
    correct_tools = sum(1 for step in run_steps if step.policy_approved)
    repetitions = sum(1 for step in run_steps if step.repeated)

    submit_steps = [step for step in steps if step.kind == "submit"]
    unsupported_flags = sum(1 for step in submit_steps if step.unsupported_flag)

    fact_claims = sum(step.fact_claim_count for step in steps)
    unsupported_facts = sum(step.unsupported_fact_claim_count for step in steps)

    total_tokens = sum(step.completion_tokens for step in steps)
    total_steps = len(steps)
    episodes = len(records)
    solved_episodes = sum(1 for record in records if record.solved)

    return TraceMetrics(
        valid_output_rate=((first_parses + recovered) / completions if completions else 0.0),
        correct_tool_selection=(correct_tools / len(run_steps)) if run_steps else 1.0,
        repetition_rate=(repetitions / len(run_steps)) if run_steps else 0.0,
        recovery_rate=(recovered / first_failures) if first_failures else 1.0,
        output_tokens_per_decision=(total_tokens / completions) if completions else 0.0,
        steps_per_objective=(total_steps / episodes) if episodes else 0.0,
        solve_rate=(solved_episodes / episodes) if episodes else 0.0,
        unsupported_fact_rate=(unsupported_facts / fact_claims) if fact_claims else 0.0,
        unsupported_flag_rate=(unsupported_flags / len(submit_steps)) if submit_steps else 0.0,
    )


async def _probe(client: MatrixClient, *, model_id: str) -> MatrixProbe:
    """Probe the model's preferred protocol with one fixed prompt."""
    completion = await _invoke(client, MATRIX_PROBE_PROMPT, model_id=model_id)
    return MatrixProbe(
        model_id=model_id,
        sample=completion.text,
        detected_protocol=probe_protocol(completion.text),
    )


async def evaluate_model(
    client: MatrixClient,
    *,
    model_id: str,
    targets: Sequence[str] = DEFAULT_TARGETS,
    protocols: Sequence[str] = MATRIX_PROTOCOLS,
    max_turns: int = DEFAULT_MAX_TURNS,
    flag_pattern: str = DEFAULT_FLAG_PATTERN,
    working_directory: Path | None = None,
) -> MatrixReport:
    """Evaluate one model against the harness protocols and lab targets.

    For every protocol in ``protocols``, the model runs one episode per
    target in ``targets`` (fresh, isolated lab instances); every
    interaction is recorded and the nine metrics are computed per
    protocol. A single probe completion (classified with
    :func:`~ozzgraph.profiles.probe_protocol`) records the model's
    preferred protocol before any episode.

    Args:
        client: The model under evaluation — a callable
            (``prompt -> completion``) or a
            :class:`~ozzgraph.model_client.ModelService`-like object
            with an async ``complete(ModelRequest)`` method.
        model_id: The model identifier (reported in the matrix and used
            to resolve its profile for the adapters).
        targets: Lab target names to evaluate against (each fresh and
            isolated per episode).
        protocols: Protocol families to evaluate. Defaults to
            :data:`MATRIX_PROTOCOLS` (terminal, three-line, JSON);
            ``function_call`` has no concrete adapter and is only
            evaluated when an adapter is registered and requested
            explicitly.
        max_turns: Cap on turns per episode (>= 1).
        flag_pattern: The regex the run accepts as a flag (pass
            :data:`LAB_FLAG_PATTERN` for the synthetic lab).
        working_directory: Scratch directory for tool execution; when
            None a fresh temporary directory is used per episode.

    Returns:
        The deterministic :class:`MatrixReport` (JSON-serializable).

    Raises:
        MatrixError: If ``max_turns`` < 1 or ``flag_pattern`` is not a
            valid regular expression.
        LabError: If a target name is not registered.
        AdapterRegistryError: If a requested protocol has no adapter.
    """
    if max_turns < 1:
        raise MatrixError(f"max_turns must be >= 1, got {max_turns}")
    try:
        re.compile(flag_pattern)
    except re.error as exc:
        raise MatrixError(f"flag_pattern is not a valid regular expression: {exc}") from exc

    probe = await _probe(client, model_id=model_id)
    rows: list[MatrixRow] = []
    for protocol in protocols:
        adapter = adapter_for(protocol)(profile_for_model_id(model_id))
        records: list[EpisodeRecord] = []
        for target_name in targets:
            with get_target(target_name) as target:
                if working_directory is None:
                    with tempfile.TemporaryDirectory(
                        prefix=f"ozz-matrix-{target_name}-"
                    ) as temporary:
                        records.append(
                            await _run_episode(
                                adapter=adapter,
                                client=client,
                                model_id=model_id,
                                target_value=target.target_value,
                                target_name=target.name,
                                flag=target.flag,
                                flag_pattern=flag_pattern,
                                max_turns=max_turns,
                                working_directory=Path(temporary),
                            )
                        )
                else:
                    records.append(
                        await _run_episode(
                            adapter=adapter,
                            client=client,
                            model_id=model_id,
                            target_value=target.target_value,
                            target_name=target.name,
                            flag=target.flag,
                            flag_pattern=flag_pattern,
                            max_turns=max_turns,
                            working_directory=working_directory,
                        )
                    )
        steps = [step for record in records for step in record.steps]
        rows.append(
            MatrixRow(
                model_id=model_id,
                protocol=protocol,
                episodes=len(records),
                steps=len(steps),
                completions=len(steps),
                solved_episodes=sum(1 for record in records if record.solved),
                total_completion_tokens=sum(step.completion_tokens for step in steps),
                metrics=aggregate_metrics(records),
                interactions=steps,
            )
        )
    return MatrixReport(model_id=model_id, probe=probe, rows=rows)
