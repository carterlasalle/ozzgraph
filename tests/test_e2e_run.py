"""Process-level end-to-end tests for ``ozzgraph run <target>`` (V02).

Drives the V02 vertical slice as a REAL process — ``python -m ozzgraph
run <target>`` spawned with :mod:`subprocess` — against a deliberately
vulnerable synthetic target (the lab's ``hidden-routes`` server: robots.txt
advertises ``/admin``, which serves the lab flag) and a stub
OpenAI-compatible model endpoint. NO component is driven directly: the
discover -> model -> tool -> parse -> graph -> hypothesis -> validate ->
Finding -> exit chain must complete inside the child process, and this
test asserts only observable process outcomes:

- exit code 0 (``TerminationReason.COMPLETED`` mapping),
- stdout ending with the human-readable ``TERMINATION: completed`` line
  (AGENTS.md rule 9),
- the run's SQLite state graph containing the full entity chain
  (target / objective / evidence / hypothesis / finding, plus the
  plan/plan_step/evaluation machinery of the validation path),
- exactly one validated :class:`~ozzgraph.findings.Finding` rendered to
  ``findings.json`` via :class:`~ozzgraph.findings.FindingStore`, whose
  evidence ids all resolve to graph ``evidence`` entities and whose
  graph entity payload matches the JSON render (the graph is the
  authoritative store, AGENTS.md rule 1).

The stub model endpoint (see tests/test_model_client.py for the response
shape) is scripted with three distinct bounded ``curl`` actions against
the live lab target, reproducing the deterministic happy path: two
evidence-backed hypotheses form, the planner branches into a plan, the
plan step's probe of ``/admin`` yields new supporting evidence, the
evaluator confirms the hypothesis (COMPLETE verdict), and the finding is
produced. The scripted call count is asserted — the run must not loop
beyond the scripted script.

Failure-path tests cover the CLI's exit-code mapping deterministically:
a whitespace target and an unsupported scheme exit 1 (failed) with a
loud stderr message before any supervisor work, and a run whose model
never proposes an action terminates budget_exhausted (exit 3) when the
runtime budget is exhausted.

HAL-008 (docs/adr/0012) adds the HalCTF-mode process-boundary policy:
the same subprocess harness drives ``python -m ozzgraph`` under a
HalCTF runtime environment (``HAL_CHALLENGE_ID`` / ``HAL_TARGET_*``),
proving that a budget-exhausted, an unsolved, a gave-up (platform
rejected the flag), and a signal-stopped run all exit 0 while the run
log keeps the full structured termination reason, and that
startup-impossible configuration (missing ``HAL_USER_ID``, an invalid
``HAL_TARGET_PORT``) still exits 1.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

from ozzgraph.findings import Finding
from ozzgraph.lab import get_target
from ozzgraph.state_graph import StateGraph

#: The lab's flag envelope (docs/SYNTHETIC_LAB.md): ``OZ{...}``.
LAB_FLAG_PATTERN = r"OZ\{[^{}\s]+\}"

#: Runtime cap for the happy-path run (actual runs finish in ~1s; the cap
#: only bounds a broken run).
_RUNTIME_S = 120

_TERMINATION_PREFIX = "TERMINATION: "


class _ScriptedModelServer(ThreadingHTTPServer):
    """A stub OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Serves the scripted completions in order, counting calls so the test
    can assert the child run consumed exactly the scripted script. The
    response payload matches the shape ``ModelResponse`` validates
    (tests/test_model_client.py). Loopback-only, ephemeral port.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, responses: list[str]) -> None:
        super().__init__(("127.0.0.1", 0), _ModelHandler)
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.calls = 0

    @property
    def base_url(self) -> str:
        address = self.server_address
        return f"http://{address[0]}:{address[1]}/v1"

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def _next_completion(self) -> str:
        with self._lock:
            self.calls += 1
            index = min(self.calls - 1, len(self._responses) - 1)
            return self._responses[index]


class _ModelHandler(BaseHTTPRequestHandler):
    """POST /chat/completions -> the server's next scripted completion."""

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        server = self.server
        assert isinstance(server, _ScriptedModelServer)
        payload = {
            "id": f"chatcmpl-e2e-{server.calls + 1}",
            "object": "chat.completion",
            "created": 1780000000,
            "model": "scripted",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": server._next_completion()},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence request logging — the lab is a test fixture."""


def _hermetic_env(state_dir: Path, model_base_url: str) -> dict[str, str]:
    """A clean child environment: no inherited OZZGRAPH_* / HAL_* knobs."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("OZZGRAPH_", "HAL_"))
    }
    env.update(
        {
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_STATE_DIR": str(state_dir),
            "OZZGRAPH_MODEL_BASE_URL": model_base_url,
            "OZZGRAPH_MODEL_ID": "deepseek-v4-flash",
            "OZZGRAPH_FLAG_PATTERN": LAB_FLAG_PATTERN,
            "OZZGRAPH_MAX_RUNTIME_S": str(_RUNTIME_S),
            "OZZGRAPH_HEARTBEAT_INTERVAL_S": "300",
        }
    )
    return env


def _run_ozzgraph(
    *args: str,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``python -m ozzgraph ...`` as a REAL child process."""
    return subprocess.run(
        [sys.executable, "-m", "ozzgraph", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )


def _halctf_env(state_dir: Path, model_base_url: str, **overrides: str) -> dict[str, str]:
    """A clean HalCTF-mode child environment (HAL-008 tests).

    Filters inherited ``OZZGRAPH_*`` / ``HAL_*`` knobs like
    :func:`_hermetic_env`, then sets the HalCTF runtime variables:
    ``HAL_CHALLENGE_ID`` selects HalCTF mode (docs/adr/0011), the MCP
    retries/timeout are tiny so the env-only bootstrap's best-effort
    status/hint calls (no MCP endpoint in these tests — they fail to
    the localhost default and are recorded as events, never fatal) fail
    instantly instead of backing off, and the runtime budget is small
    so an unsolved run exhausts quickly. ``overrides`` lets each test
    inject the platform surface (``HAL_TARGET_*`` services, the
    ``OZZGRAPH_SIDECAR_BASE_URL``, or a broken variable for the
    startup-impossible cases).
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("OZZGRAPH_", "HAL_"))
    }
    env.update(
        {
            "HAL_USER_ID": "user-42",
            "HAL_CHALLENGE_ID": "web-01",
            "OZZGRAPH_STATE_DIR": str(state_dir),
            "OZZGRAPH_MODEL_BASE_URL": model_base_url,
            "OZZGRAPH_MODEL_ID": "deepseek-v4-flash",
            "OZZGRAPH_MAX_RUNTIME_S": "2",
            "OZZGRAPH_HEARTBEAT_INTERVAL_S": "300",
            "OZZGRAPH_MCP_MAX_RETRIES": "0",
            "OZZGRAPH_MCP_TIMEOUT_S": "1",
        }
    )
    env.update(overrides)
    return env


class _EntitySnapshot(NamedTuple):
    """One graph entity as the test reads it (id + payload)."""

    id: str
    data: dict[str, object]


def _graph_entity_types(state_dir: Path) -> dict[str, list[_EntitySnapshot]]:
    """Group the run's SQLite graph entities by type."""

    async def _read() -> dict[str, list[_EntitySnapshot]]:
        grouped: dict[str, list[_EntitySnapshot]] = {}
        async with StateGraph(state_dir / "graph.db") as graph:
            for record in await graph.list_entities():
                grouped.setdefault(record.type, []).append(_EntitySnapshot(record.id, record.data))
        return grouped

    return asyncio.run(_read())


def _event_types(state_dir: Path) -> list[dict[str, object]]:
    """Every run-log event from ``state_dir/actions.jsonl``."""
    path = state_dir / "actions.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_run_target_end_to_end_completes_with_finding(tmp_path: Path) -> None:
    """``ozzgraph run <target>`` completes end-to-end as a real process.

    The full chain — discover -> model -> tool -> parse -> graph ->
    hypothesis -> validate -> Finding -> exit — runs inside the child
    process against the live ``hidden-routes`` lab target and the
    scripted model endpoint, and every assertion reads observable
    process output: exit code 0, the termination line, the state graph
    entity chain, and the persisted Finding.
    """
    with get_target("hidden-routes") as target:
        url = target.target_value
        server = _ScriptedModelServer(
            [
                f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {url}/",
                f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {url}/robots.txt",
                f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {url}/admin",
            ]
        )
        server.start()
        try:
            state_dir = tmp_path / "state"
            result = _run_ozzgraph(
                "run",
                url,
                env=_hermetic_env(state_dir, server.base_url),
                cwd=tmp_path,
            )
        finally:
            server.stop()

    # Process outcome: COMPLETED maps to exit 0, and the final stdout
    # line is the human-readable termination summary (AGENTS.md rule 9).
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "USER ID: user-42"
    assert result.stdout.splitlines()[-1] == "TERMINATION: completed"

    # The scripted script was consumed exactly: the run must not loop
    # beyond the three scripted actions.
    assert server.calls == 3

    # The state graph holds the full discover -> ... -> Finding chain.
    entities = _graph_entity_types(state_dir)
    for entity_type in (
        "run",
        "scope",
        "target",
        "objective",
        "action",
        "observation",
        "evidence",
        "hypothesis",
        "plan",
        "plan_step",
        "evaluation",
        "finding",
    ):
        assert entity_type in entities, f"graph is missing {entity_type!r} entities"

    targets = entities["target"]
    objectives = entities["objective"]
    assert len(targets) == 1
    assert targets[0].data["address"] == url
    assert len(objectives) == 1
    assert objectives[0].data["completed"] is True

    evidence_ids = {record.id for record in entities["evidence"]}
    hypothesis_ids = {record.id for record in entities["hypothesis"]}
    findings = entities["finding"]
    assert len(findings) == 1
    finding_id = findings[0].id
    assert finding_id.startswith("finding-hypothesis-")
    assert finding_id.removeprefix("finding-") in hypothesis_ids

    # The finding entity mirrors the validation path: an edge to its
    # hypothesis, evidence ids that all resolve to graph evidence.
    hypothesis_id = finding_id.removeprefix("finding-")

    async def _finding_edge() -> bool:
        async with StateGraph(state_dir / "graph.db") as graph:
            return await graph.get_edge(f"{finding_id}-validates-{hypothesis_id}") is not None

    assert asyncio.run(_finding_edge()) is True

    # findings.json is the operator-facing render of the authoritative
    # graph entity: identical payload, validated against the model.
    findings_path = state_dir / "findings.json"
    assert findings_path.is_file()
    rendered = json.loads(findings_path.read_text(encoding="utf-8"))
    assert len(rendered) == 1
    finding = Finding.model_validate(rendered[0])
    assert finding.id == finding_id
    assert finding.hypothesis_id == hypothesis_id
    assert finding.target_id == targets[0].id
    assert finding.cwe
    assert finding.preconditions
    assert finding.reproduction
    assert "curl" in finding.reproduction
    assert finding.confidence > 0.0 and finding.confidence <= 1.0
    assert finding.impact.confidentiality in {"none", "low", "medium", "high", "unknown"}
    assert set(finding.evidence_ids) <= evidence_ids
    assert findings[0].data == finding.model_dump(mode="json")

    # The run log records the finding production and the terminal event.
    events = _event_types(state_dir)
    event_types = [event["event_type"] for event in events]
    assert "runner.finding_created" in event_types
    assert event_types[-1] == "termination"
    assert events[-1]["payload"] == {"reason": "completed"}

    # V08: a completed run renders the full report bundle (docs/adr/0010).
    report_path = state_dir / "report.json"
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run"]["id"] == events[0]["run_id"]  # the run's own id
    assert report["run"]["environment"] == "local"
    assert report["termination"]["status"] == "completed"
    assert report["counts"]["finding"] == 1
    assert report["findings"][0]["id"] == finding_id
    assert (state_dir / "report.md").is_file()
    assert (state_dir / "report.sarif").is_file()
    assert (state_dir / "evidence").is_dir()
    assert (state_dir / "graph.sqlite").is_file()
    assert (state_dir / "events.jsonl").is_file()


def test_run_target_rejects_whitespace_target(tmp_path: Path) -> None:
    """A whitespace target is a configuration error: exit 1, loud stderr."""
    env = _hermetic_env(tmp_path / "state", "http://127.0.0.1:1/v1")
    result = _run_ozzgraph("run", "http://127.0.0.1:1 bad", env=env, cwd=tmp_path)
    assert result.returncode == 1
    assert "whitespace" in result.stderr
    assert _TERMINATION_PREFIX not in result.stdout
    assert not (tmp_path / "state" / "graph.db").exists()


def test_run_target_rejects_unsupported_scheme(tmp_path: Path) -> None:
    """A non-http(s) scheme is a configuration error: exit 1, loud stderr."""
    env = _hermetic_env(tmp_path / "state", "http://127.0.0.1:1/v1")
    result = _run_ozzgraph("run", "ftp://example.invalid/flag", env=env, cwd=tmp_path)
    assert result.returncode == 1
    assert "unsupported target scheme" in result.stderr
    assert "ftp" in result.stderr


def test_run_target_ends_budget_exhausted_when_model_never_acts(tmp_path: Path) -> None:
    """A model that never proposes an action ends budget_exhausted (exit 3).

    The scripted model only reasons (``think``), so no action ever
    executes; the tiny runtime budget exhausts and the process exits 3
    with the matching termination line — the CLI's exit-code mapping
    exercised end-to-end.
    """
    with get_target("http-recon") as target:
        url = target.target_value
        server = _ScriptedModelServer(["The target is reachable; nothing further to run."])
        server.start()
        try:
            state_dir = tmp_path / "state"
            env = _hermetic_env(state_dir, server.base_url)
            env["OZZGRAPH_MAX_RUNTIME_S"] = "2"
            env["OZZGRAPH_HEARTBEAT_INTERVAL_S"] = "300"
            result = _run_ozzgraph("run", url, env=env, cwd=tmp_path, timeout=60)
        finally:
            server.stop()

    assert result.returncode == 3
    assert result.stdout.splitlines()[-1] == "TERMINATION: budget_exhausted"


# ---------------------------------------------------------------------------
# HAL-008: HalCTF-mode process-boundary exit policy (docs/adr/0012)
# ---------------------------------------------------------------------------


def test_halctf_budget_exhausted_run_exits_zero_with_structured_event(tmp_path: Path) -> None:
    """A HalCTF budget-exhausted run exits 0, keeping the structured reason.

    On the event platform a nonzero container exit is a crash that
    reruns the detonation, so the process boundary must flatten: the
    child exits 0 even though the run terminated BUDGET_EXHAUSTED, and
    the termination event in the run log still records the full
    structured reason (``budget_exhausted``) — the model is never
    collapsed (HAL-008 acceptance 2).
    """
    server = _ScriptedModelServer(["The target is reachable; nothing further to run."])
    server.start()
    try:
        state_dir = tmp_path / "state"
        result = _run_ozzgraph(env=_halctf_env(state_dir, server.base_url), cwd=tmp_path)
    finally:
        server.stop()

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "TERMINATION: budget_exhausted"

    events = _event_types(state_dir)
    assert events[-1]["event_type"] == "termination"
    assert events[-1]["payload"] == {"reason": "budget_exhausted"}


def test_halctf_unsolved_run_exits_zero_without_accepted_submission(tmp_path: Path) -> None:
    """An unsolved HalCTF run (no accepted submission) exits 0 (HAL-008).

    The model genuinely works — one executed curl against the injected
    ``HAL_TARGET_*`` service — but the response body never contains the
    flag, so no candidate is ever submitted and HAL-006 keeps the run
    from completing unscored: the run terminates BUDGET_EXHAUSTED and
    the process exits 0.
    """
    with get_target("http-recon") as target:
        url = target.target_value
        server = _ScriptedModelServer([f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {url}/"])
        server.start()
        try:
            state_dir = tmp_path / "state"
            parsed = urllib.parse.urlsplit(url)
            env = _halctf_env(
                state_dir,
                server.base_url,
                HAL_TARGET_IP=parsed.hostname or "",
                HAL_TARGET_PORT=str(parsed.port or 80),
            )
            result = _run_ozzgraph(env=env, cwd=tmp_path, timeout=60)
        finally:
            server.stop()

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "TERMINATION: budget_exhausted"

    events = _event_types(state_dir)
    assert events[-1]["payload"] == {"reason": "budget_exhausted"}
    # The run worked (one executed action) but never submitted anything:
    # no submission and no flag-candidate events in the run log.
    event_types = [event["event_type"] for event in events]
    assert not any(
        isinstance(event_type, str) and event_type.startswith("submission.")
        for event_type in event_types
    )
    assert not any(
        isinstance(event_type, str) and event_type.startswith("flags.")
        for event_type in event_types
    )
    assert any(event_type == "runner.action_executed" for event_type in event_types)


def test_halctf_gave_up_after_platform_rejection_exits_zero(tmp_path: Path) -> None:
    """A gave-up HalCTF run (platform rejected the flag) exits 0 (HAL-008).

    The model finds the challenge flag, the supervisor submits it
    through the REAL sidecar transport (the scripted server from
    tests/test_flag_loop.py), the platform rejects it, and the run
    keeps investigating until BUDGET_EXHAUSTED — the process exits 0
    with the rejection still recorded in the run log.
    """
    from test_flag_loop import _SidecarServer  # tests/ is on sys.path under pytest

    with get_target("hidden-routes") as target:
        url = target.target_value
        server = _ScriptedModelServer([f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {url}/admin"])
        sidecar = _SidecarServer(verdict="wrong", points=0)
        with sidecar:
            server.start()
            try:
                state_dir = tmp_path / "state"
                parsed = urllib.parse.urlsplit(url)
                env = _halctf_env(
                    state_dir,
                    server.base_url,
                    HAL_TARGET_IP=parsed.hostname or "",
                    HAL_TARGET_PORT=str(parsed.port or 80),
                    OZZGRAPH_SIDECAR_BASE_URL=sidecar.base_url,
                )
                result = _run_ozzgraph(env=env, cwd=tmp_path, timeout=60)
            finally:
                server.stop()

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "TERMINATION: budget_exhausted"

    events = _event_types(state_dir)
    assert events[-1]["payload"] == {"reason": "budget_exhausted"}
    event_types = [event["event_type"] for event in events]
    assert "submission.rejected" in event_types
    assert "submission.accepted" not in event_types

    # Exactly one wire attempt — the rejected flag is never re-submitted.
    assert len(sidecar.submits) == 1
    assert sidecar.submits[0]["challenge_id"] == "web-01"
    submitted_flag = sidecar.submits[0]["flag"]
    assert isinstance(submitted_flag, str)
    assert re.fullmatch(LAB_FLAG_PATTERN, submitted_flag)


def test_halctf_signal_stop_exits_zero_with_structured_event(tmp_path: Path) -> None:
    """A SIGTERM-stopped HalCTF run exits 0, recording ``interrupted`` (HAL-008).

    INTERRUPTED is deliberately flattened to 0 in HalCTF mode: a signal
    stop is how the platform tears a run down, and a 130 would be
    misread as a crash and rerun (docs/adr/0012). The run log still
    records the structured ``interrupted`` reason. (Local mode keeps
    the 130 mapping — tests/test_signals.py.)
    """
    env = _halctf_env(tmp_path / "state", "http://127.0.0.1:1/v1")
    env["OZZGRAPH_MAX_RUNTIME_S"] = "600"  # never exhausts; the test signals it
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozzgraph"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    try:
        assert proc.stdout is not None
        first_line = proc.stdout.readline()
        assert first_line.strip() == "USER ID: user-42"
        os.kill(proc.pid, signal.SIGTERM)
        code = proc.wait(timeout=30)
        assert code == 0
        assert proc.stdout.read().splitlines()[-1] == "TERMINATION: interrupted"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    events = _event_types(tmp_path / "state")
    assert events[-1]["event_type"] == "termination"
    assert events[-1]["payload"] == {"reason": "interrupted"}


def test_halctf_startup_impossible_missing_user_id_exits_one(tmp_path: Path) -> None:
    """Missing ``HAL_USER_ID`` is startup-impossible: exit 1 (HAL-008).

    Load-time configuration errors keep the nonzero boundary — the
    process never started a run, so there is no structured termination
    to flatten.
    """
    env = _halctf_env(tmp_path / "state", "http://127.0.0.1:1/v1")
    env.pop("HAL_USER_ID")
    result = _run_ozzgraph(env=env, cwd=tmp_path)
    assert result.returncode == 1
    assert "HAL_USER_ID" in result.stderr
    assert _TERMINATION_PREFIX not in result.stdout
    assert not (tmp_path / "state" / "actions.jsonl").exists()


def test_halctf_startup_impossible_invalid_target_port_exits_one(tmp_path: Path) -> None:
    """A set-but-invalid ``HAL_TARGET_PORT`` is startup-impossible: exit 1.

    The invalid port fails loudly at load time (``ConfigError``,
    AGENTS.md rule #9) — a configuration error before any supervisor
    work, mapped to exit 1 in every mode.
    """
    env = _halctf_env(
        tmp_path / "state",
        "http://127.0.0.1:1/v1",
        HAL_TARGET_IP="10.0.0.5",
        HAL_TARGET_PORT="not-a-port",
    )
    result = _run_ozzgraph(env=env, cwd=tmp_path)
    assert result.returncode == 1
    assert "HAL_TARGET_PORT" in result.stderr
    assert "must be an integer" in result.stderr
    assert _TERMINATION_PREFIX not in result.stdout
    assert not (tmp_path / "state" / "actions.jsonl").exists()
