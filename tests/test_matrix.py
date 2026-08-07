"""Tests for the model-harness matrix (PR28): deterministic metrics.

Covers the docs/TESTING_AND_QA.md "Model-Harness Matrix" contract: a
deterministic scripted model (fixed outputs including malformed outputs,
repair recovery, repetition, policy-rejected tools, unsupported flag and
fact claims, and the correct submission) is evaluated against the three
harness protocols, and every one of the nine documented metrics is
verified against its hand-computed value. Also covers the callable and
ModelService client forms, protocol probing, determinism, and the loud
failure paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ozzgraph.adapters import AdapterRegistryError
from ozzgraph.lab import LabError, lab_flag
from ozzgraph.matrix import (
    LAB_FLAG_PATTERN,
    MATRIX_PROTOCOLS,
    MatrixCompletion,
    MatrixError,
    estimate_tokens,
    evaluate_model,
)
from ozzgraph.model_client import (
    ModelChoice,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)

TARGET = "hidden-routes"
FLAG = lab_flag(TARGET)  # OZ{lab-hidden-routes-<10 hex>}, deterministic

_URL_RE = re.compile(r"http://127\.0\.0\.1:\d+")


class ScriptedModel:
    """A deterministic scripted model: fixed completions per turn/protocol.

    The ten-turn script exercises every metric: valid parses, a
    repairable malformed output (json/three_line) and an unrecoverable
    one (terminal), a policy-rejected tool, a repeated command, an
    unsupported flag claim, an unsupported fact claim, and the correct
    submission. The protocol is detected from the compiled prompt's
    format instructions (like a real model would).
    """

    def __init__(self, flag: str) -> None:
        self.flag = flag
        self._turn = 0
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str | MatrixCompletion:
        self.calls.append(prompt)
        if prompt.startswith("PROBE"):
            self._turn = 0
            return "I am ready."
        if "(none)" in prompt:
            self._turn = 0
        self._turn += 1
        url_match = _URL_RE.search(prompt)
        assert url_match is not None
        if "single JSON object" in prompt:
            protocol = "json"
        elif "exactly three non-empty lines" in prompt:
            protocol = "three_line"
        else:
            protocol = "terminal"
        return MatrixCompletion(
            text=self._completion(self._turn, protocol, url_match.group(0)),
            completion_tokens=100,
        )

    def _completion(self, turn: int, protocol: str, url: str) -> str:
        if turn == 10:
            return self._emit(protocol, "submit", self.flag, "submit the observed flag")
        if turn == 9:
            return self._emit(
                protocol,
                "run",
                "curl -sS --max-time 5 http://10.0.0.1/",
                "the flag should be at the internal address",
            )
        if turn == 8:
            return self._emit(
                protocol,
                "run",
                f"curl -sS --max-time 5 {url}/",
                "probe the root surface again",
            )
        if turn == 7:
            return self._emit(
                protocol, "submit", "OZ{lab-fabricated-0000000000}", "submit the claimed flag"
            )
        if turn == 6:
            return self._emit(protocol, "think", None, "The flag is OZ{lab-fabricated-0000000000}.")
        if turn == 5:
            return self._emit(
                protocol,
                "run",
                f"curl -sS --max-time 5 {url}/admin",
                "the flag should be behind /admin",
            )
        if turn == 4:
            if protocol == "json":
                # Unclosed string: unparseable and unrepairable.
                return '{"kind": "run", "payload": "curl -sS --max-time 5 '
            if protocol == "three_line":
                # Only two labeled lines: unrepairable.
                return "THOUGHT: x\nACTION: run\n"
            # Terminal has no repair strategy; empty output is invalid.
            return ""
        if turn == 3:
            return self._emit(protocol, "think", None, "robots.txt advertises /admin")
        if turn == 2:
            if protocol == "json":
                # Fenced JSON: parse fails, fence-strip repair succeeds.
                return (
                    f"```json\n"
                    f"{self._emit('json', 'run', f'curl -sS --max-time 5 {url}/robots.txt', 'fetch robots')}"
                    f"\n```"
                )
            if protocol == "three_line":
                # Prose-wrapped labels: parse fails, labeled-line repair succeeds.
                return (
                    "Let me probe the target.\n"
                    f"{self._emit('three_line', 'run', f'curl -sS --max-time 5 {url}/robots.txt', 'fetch robots')}"
                )
            return self._emit(
                "terminal", "run", f"curl -sS --max-time 5 {url}/robots.txt", "fetch robots"
            )
        return self._emit(
            protocol, "run", f"curl -sS --max-time 5 {url}/", "probe the root surface"
        )

    @staticmethod
    def _emit(protocol: str, kind: str, payload: str | None, rationale: str) -> str:
        if protocol == "json":
            action: dict[str, str] = {"kind": kind}
            if payload is not None:
                action["payload"] = payload
            action["rationale"] = rationale
            return json.dumps(action)
        value = payload if payload is not None else "none"
        if protocol == "three_line":
            return f"THOUGHT: {rationale}\nACTION: {kind}\nPAYLOAD: {value}"
        return f"{rationale}\nACTION: {kind}\nPAYLOAD: {value}"


class ThinkOnlyModel:
    """Always emits a valid think action in whatever protocol is prompted."""

    async def __call__(self, prompt: str) -> str:
        if prompt.startswith("PROBE"):
            return "I am ready."
        if "single JSON object" in prompt:
            return json.dumps({"kind": "think", "rationale": "thinking"})
        if "exactly three non-empty lines" in prompt:
            return "THOUGHT: thinking\nACTION: think\nPAYLOAD: none"
        return "thinking\nACTION: think"


class _FakeService:
    """Minimal ModelService-like client exercising the usage-accounting path."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            id="resp-1",
            model=request.model,
            choices=[
                ModelChoice(
                    message=ModelMessage(
                        role="assistant",
                        content='{"kind": "think", "rationale": "thinking"}',
                    )
                )
            ],
            usage=ModelUsage(prompt_tokens=10, completion_tokens=50, total_tokens=60),
            created=1,
        )


class _JsonProbeModel:
    """Responds to the probe in JSON: the probe must detect it."""

    async def __call__(self, prompt: str) -> str:
        if prompt.startswith("PROBE"):
            return '{"kind": "think", "rationale": "json native"}'
        return "thinking\nACTION: think"


# ---------------------------------------------------------------------------
# the nine metrics, from a deterministic scripted model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scripted_model_reports_all_nine_metrics(tmp_path: Path) -> None:
    """Every documented metric is computed exactly as defined.

    Script (per protocol, target hidden-routes): 10 completions — 8
    first-attempt parses + 1 repaired (t2) + 1 unrecoverable (t4); 5
    run actions of which 4 pass the policy gate (t9 is rejected) and 1
    repeats t1's fingerprint (t8); 2 submits of which 1 is not the
    target flag (t7); 1 fabricated flag claim (t6); 1 solved submission
    (t10).
    """
    report = await evaluate_model(
        ScriptedModel(FLAG),
        model_id="scripted-1",
        targets=(TARGET,),
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )

    assert report.model_id == "scripted-1"
    assert report.probe.detected_protocol == "terminal"
    assert [row.protocol for row in report.rows] == list(MATRIX_PROTOCOLS)

    json_row = report.row_for("json")
    assert json_row is not None
    assert json_row.episodes == 1
    assert json_row.steps == 10
    assert json_row.completions == 10
    assert json_row.solved_episodes == 1
    assert json_row.total_completion_tokens == 1000
    metrics = json_row.metrics
    assert metrics.valid_output_rate == 0.9  # (8 + 1) / 10
    assert metrics.correct_tool_selection == 0.8  # 4 / 5 policy-approved runs
    assert metrics.repetition_rate == 0.2  # 1 / 5 repeated runs
    assert metrics.recovery_rate == 0.5  # 1 / 2 first-attempt failures
    assert metrics.output_tokens_per_decision == 100.0  # 1000 / 10
    assert metrics.steps_per_objective == 10.0  # 10 turns / 1 objective
    assert metrics.solve_rate == 1.0  # correct flag submitted
    assert metrics.unsupported_fact_rate == 1.0  # 1 / 1 fabricated claim
    assert metrics.unsupported_flag_rate == 0.5  # 1 / 2 submits not the flag

    three_line_row = report.row_for("three_line")
    assert three_line_row is not None
    assert three_line_row.metrics.valid_output_rate == 0.9
    assert three_line_row.metrics.recovery_rate == 0.5
    assert three_line_row.metrics.correct_tool_selection == 0.8

    terminal_row = report.row_for("terminal")
    assert terminal_row is not None
    # Terminal has no repair strategy: the malformed turn is unrecoverable.
    assert terminal_row.metrics.valid_output_rate == 0.9  # 9 / 10
    assert terminal_row.metrics.recovery_rate == 0.0  # 0 recovered / 1 failure
    assert terminal_row.metrics.correct_tool_selection == 0.8
    assert terminal_row.metrics.repetition_rate == 0.2


@pytest.mark.asyncio
async def test_recorded_interactions_back_the_metrics(tmp_path: Path) -> None:
    """The metrics are computed from the recorded interactions (raw material)."""
    report = await evaluate_model(
        ScriptedModel(FLAG),
        model_id="scripted-1",
        targets=(TARGET,),
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )
    json_row = report.row_for("json")
    assert json_row is not None
    interactions = json_row.interactions

    assert interactions[0].parsed and interactions[0].kind == "run"
    assert interactions[0].tool is not None and interactions[0].tool.exit_code == 0
    assert interactions[1].recovered  # fenced JSON repaired
    assert interactions[3].parsed is False  # unrepairable malformed output
    assert interactions[4].kind == "run"
    assert interactions[4].tool is not None
    assert FLAG in interactions[4].tool.stdout  # /admin served the flag
    assert interactions[5].fact_claim_count == 1
    assert interactions[5].unsupported_fact_claim_count == 1
    assert interactions[6].unsupported_flag
    assert interactions[6].solved is False
    assert interactions[7].kind == "run" and interactions[7].repeated
    assert interactions[8].kind == "run"
    assert interactions[8].policy_approved is False  # policy gate rejection
    assert interactions[8].tool is None  # never executed
    assert interactions[9].solved
    assert interactions[9].unsupported_flag is False

    # The probe is recorded once per model, before any episode.
    assert report.probe.sample == "I am ready."


# ---------------------------------------------------------------------------
# determinism and protocol detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_report_is_deterministic(tmp_path: Path) -> None:
    """The same scripted client reproduces an identical report.

    Lab targets bind an ephemeral loopback port per instance, so the
    recorded completions embed a run-specific port; the report is
    deterministic modulo that port number (a property of the
    environment, not of the client).
    """

    def _normalize_ports(document: str) -> str:
        return re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", document)

    first = await evaluate_model(
        ScriptedModel(FLAG),
        model_id="scripted-1",
        targets=(TARGET,),
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )
    second = await evaluate_model(
        ScriptedModel(FLAG),
        model_id="scripted-1",
        targets=(TARGET,),
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )
    assert _normalize_ports(first.model_dump_json()) == _normalize_ports(second.model_dump_json())


@pytest.mark.asyncio
async def test_probe_classifies_the_models_preferred_protocol(tmp_path: Path) -> None:
    """The probe reuses profiles.probe_protocol on one completion."""
    report = await evaluate_model(
        _JsonProbeModel(),
        model_id="probe-1",
        targets=(TARGET,),
        max_turns=1,
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )
    assert report.probe.sample == '{"kind": "think", "rationale": "json native"}'
    assert report.probe.detected_protocol == "json"


@pytest.mark.asyncio
async def test_default_evaluation_produces_three_protocol_rows(tmp_path: Path) -> None:
    """The default protocols evaluate terminal, three-line, and JSON."""
    report = await evaluate_model(
        ThinkOnlyModel(),
        model_id="think-1",
        targets=(TARGET,),
        max_turns=3,
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )
    assert [row.protocol for row in report.rows] == list(MATRIX_PROTOCOLS)
    assert report.probe.detected_protocol == "terminal"
    for row in report.rows:
        assert row.episodes == 1
        assert row.steps == 3
        assert row.solved_episodes == 0
        metrics = row.metrics
        assert metrics.valid_output_rate == 1.0
        assert metrics.correct_tool_selection == 1.0  # no run actions: vacuous
        assert metrics.repetition_rate == 0.0
        assert metrics.recovery_rate == 1.0  # no failures: vacuous
        assert metrics.steps_per_objective == 3.0
        assert metrics.solve_rate == 0.0
        assert metrics.unsupported_fact_rate == 0.0
        assert metrics.unsupported_flag_rate == 0.0
        # Plain-string completions get the deterministic token estimate.
        assert metrics.output_tokens_per_decision == (
            sum(estimate_tokens(step.completion) for step in row.interactions)
            / len(row.interactions)
        )
        for step in row.interactions:
            assert step.tool is None  # think actions never execute a tool


# ---------------------------------------------------------------------------
# client forms and failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_form_client_uses_usage_accounting(tmp_path: Path) -> None:
    """A ModelService-like client's usage tokens drive the token metric."""
    service = _FakeService()
    report = await evaluate_model(
        service,
        model_id="svc-model",
        targets=(TARGET,),
        protocols=("json",),
        max_turns=2,
        flag_pattern=LAB_FLAG_PATTERN,
        working_directory=tmp_path,
    )
    row = report.rows[0]
    assert row.protocol == "json"
    assert row.total_completion_tokens == 100  # 2 completions x 50 usage tokens
    assert row.metrics.output_tokens_per_decision == 50.0
    assert service.requests
    assert service.requests[0].model == "svc-model"
    assert service.requests[0].messages[0].role == "user"


@pytest.mark.asyncio
async def test_unknown_protocol_fails_loudly(tmp_path: Path) -> None:
    """function_call has no concrete adapter: requesting it is a loud error."""
    with pytest.raises(AdapterRegistryError, match="function_call"):
        await evaluate_model(
            ThinkOnlyModel(),
            model_id="m",
            targets=(TARGET,),
            protocols=("function_call",),
            flag_pattern=LAB_FLAG_PATTERN,
            working_directory=tmp_path,
        )


@pytest.mark.asyncio
async def test_invalid_arguments_fail_loudly(tmp_path: Path) -> None:
    """max_turns < 1 and an invalid flag pattern are MatrixError."""
    with pytest.raises(MatrixError, match="max_turns"):
        await evaluate_model(
            ThinkOnlyModel(),
            model_id="m",
            targets=(TARGET,),
            max_turns=0,
            flag_pattern=LAB_FLAG_PATTERN,
            working_directory=tmp_path,
        )
    with pytest.raises(MatrixError, match="regular expression"):
        await evaluate_model(
            ThinkOnlyModel(),
            model_id="m",
            targets=(TARGET,),
            flag_pattern="OZ[",
            working_directory=tmp_path,
        )


@pytest.mark.asyncio
async def test_unknown_target_fails_loudly(tmp_path: Path) -> None:
    """An unknown target name raises the lab's LabError."""
    with pytest.raises(LabError, match="unknown synthetic target 'nope'"):
        await evaluate_model(
            ThinkOnlyModel(),
            model_id="m",
            targets=("nope",),
            flag_pattern=LAB_FLAG_PATTERN,
            working_directory=tmp_path,
        )
