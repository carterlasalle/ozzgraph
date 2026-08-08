"""Tests for the deterministic bootstrap runner (PR12).

Contract + integration coverage per docs/TESTING_AND_QA.md: target parsing
(single + namespaced, malformed input), smoke-flag handling, challenge
status retrieval, free hint zero (available and unavailable), reachability
validation (success, failure, fail-closed allowlist), and the structured
event log. HalClient interactions run against the fake JSON-RPC 2.0 MCP
server (``mcp_fake.py``); probes use a scripted fake so tests never touch
the network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp_fake import rpc_error, rpc_result

import ozzgraph
from ozzgraph.bootstrap import (
    PROBE_SPECS,
    BootstrapRunner,
    ProbeResult,
    ProbeSpec,
    Targets,
    load_targets,
)
from ozzgraph.config import ConfigError, OzzGraphConfig
from ozzgraph.events import (
    BOOTSTRAP_CHALLENGE_STATUS,
    BOOTSTRAP_FAILED,
    BOOTSTRAP_HINT_REQUESTED,
    BOOTSTRAP_HINT_UNAVAILABLE,
    BOOTSTRAP_PROBE_RUN,
    BOOTSTRAP_REACHABILITY,
    BOOTSTRAP_SMOKE_SUBMITTED,
    BOOTSTRAP_TARGETS_PARSED,
    EventLog,
)
from ozzgraph.hal_client import HalClient
from ozzgraph.supervisor import Supervisor, TerminationReason

CHALLENGE_ID = "web-01"

STATUS_JSON: dict[str, object] = {
    "challenge_id": CHALLENGE_ID,
    "solved": False,
    "attempts": 2,
    "hints_used": 1,
    "points_earned": 0,
    "updated_at": "2026-08-07T00:00:00Z",
}

SUBMISSION_JSON: dict[str, object] = {
    "challenge_id": CHALLENGE_ID,
    "accepted": True,
    "message": "Correct!",
    "points": 100,
    "attempts_remaining": 5,
}

HINT_JSON: dict[str, object] = {
    "challenge_id": CHALLENGE_ID,
    "index": 0,
    "hint": "Inspect the HTML",
    "paid": False,
}


class _NoopSleeper:
    """Backoff sleeper that returns immediately (deterministic tests)."""

    async def __call__(self, _: float) -> None:
        return None


class _FakeProbeRunner:
    """Scripted ProbeRunner: never touches the network, records calls."""

    def __init__(self, result: ProbeResult | None = None) -> None:
        self._result = (
            result
            if result is not None
            else ProbeResult(status="reachable", detail="fake", exit_code=0, duration=0.01)
        )
        self.calls: list[tuple[str, str]] = []

    async def run(self, spec: ProbeSpec, command: str) -> ProbeResult:
        self.calls.append((spec.kind, command))
        return self._result


def _config(tmp_path: Path, **overrides: object) -> OzzGraphConfig:
    base: dict[str, object] = {
        "hal_user_id": "user-42",
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
        "target_allowlist": ("10.0.0.5", "challenge.local"),
    }
    base.update(overrides)
    return OzzGraphConfig(**base)  # type: ignore[arg-type]


def _runner(
    tmp_path: Path,
    server: Any,
    *,
    environ: dict[str, str] | None = None,
    probe: Any = None,
    **config_overrides: object,
) -> tuple[BootstrapRunner, EventLog, Path]:
    """Build a BootstrapRunner pointed at the fake MCP server."""
    config = _config(tmp_path, **config_overrides)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log = EventLog.for_run(config.state_dir)
    client = HalClient(
        base_url=server.base_url,
        privileged=True,
        sleeper=_NoopSleeper(),
        event_log=log,
        run_id="run-1",
    )
    runner = BootstrapRunner(
        config=config,
        run_id="run-1",
        event_log=log,
        client=client,
        environ=environ,
        probe_runner=probe,
    )
    return runner, log, config.state_dir


def _records(log: EventLog) -> list[dict[str, object]]:
    """Parse every line of the log into dicts."""
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]


def _event_types(records: list[dict[str, object]]) -> list[str]:
    return [str(record["event_type"]) for record in records]


def _payloads(records: list[dict[str, object]], event_type: str) -> list[dict[str, object]]:
    """Payloads of events matching ``event_type``, narrowed to dicts."""
    payloads: list[dict[str, object]] = []
    for record in records:
        if record["event_type"] == event_type:
            payload = record["payload"]
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


# --------------------------------------------------------------------------
# Target parsing
# --------------------------------------------------------------------------


def test_package_imports() -> None:
    """The package still imports (kept from the placeholder suite)."""
    assert ozzgraph.__version__ == "2.0.0"


def test_load_targets_parses_single_and_namespaced() -> None:
    """Single and namespaced targets parse into the validated model."""
    targets = load_targets(
        {
            "OZZGRAPH_TARGET": "http://10.0.0.5:8080",
            "OZZGRAPH_TARGET_HTTP": "10.0.0.6",
            "OZZGRAPH_TARGET_DNS": "challenge.local",
            "OZZGRAPH_TARGET_ALLOWLIST": "10.0.0.0/24",
            "OZZGRAPH_SMOKE_FLAG": "flag{x}",
            "OZZGRAPH_CHALLENGE_ID": "web-01",
        }
    )
    assert isinstance(targets, Targets)
    assert targets.single == "http://10.0.0.5:8080"
    assert targets.namespaced == {"HTTP": "10.0.0.6", "DNS": "challenge.local"}
    specs = targets.specs()
    assert [(s.name, s.category) for s in specs] == [
        ("single", "http"),
        ("DNS", "dns"),
        ("HTTP", "http"),
    ]


def test_load_targets_blank_single_is_unset() -> None:
    """A blank OZZGRAPH_TARGET is treated as unset, like config's helpers."""
    targets = load_targets({"OZZGRAPH_TARGET": "   "})
    assert targets.single is None
    assert targets.namespaced == {}
    assert targets.specs() == []


def test_load_targets_empty_environment_yields_no_targets() -> None:
    """No target variables at all is valid (probes simply do not run)."""
    targets = load_targets({})
    assert targets.single is None
    assert targets.namespaced == {}


def test_load_targets_malformed_namespaced_value_raises_config_error() -> None:
    """A blank namespaced value is a loud configuration error."""
    with pytest.raises(ConfigError, match="OZZGRAPH_TARGET_HTTP"):
        load_targets({"OZZGRAPH_TARGET_HTTP": "   "})


def test_load_targets_unknown_namespace_raises_config_error() -> None:
    """An unsupported namespace (no probe category) fails loudly."""
    with pytest.raises(ConfigError, match="OZZGRAPH_TARGET_SMTP"):
        load_targets({"OZZGRAPH_TARGET_SMTP": "mail.example"})
    with pytest.raises(ConfigError, match="empty namespace"):
        load_targets({"OZZGRAPH_TARGET_": "x"})


def test_targets_model_rejects_unknown_namespace() -> None:
    """The Targets model itself validates namespaces (pydantic backstop)."""
    with pytest.raises(ValueError, match="unsupported target namespaces"):
        Targets(namespaced={"FTP": "10.0.0.9"})  # type: ignore[arg-type]


def test_infer_single_target_category() -> None:
    """A URL single target is HTTP/HTTPS; anything else is DNS."""
    assert load_targets({"OZZGRAPH_TARGET": "https://10.0.0.5"}).specs()[0].category == "https"
    assert load_targets({"OZZGRAPH_TARGET": "http://10.0.0.5"}).specs()[0].category == "http"
    assert load_targets({"OZZGRAPH_TARGET": "challenge.local"}).specs()[0].category == "dns"


# --------------------------------------------------------------------------
# Bootstrap runner: success path
# --------------------------------------------------------------------------


def test_success_path_records_all_bootstrap_events(run_mcp: Any, tmp_path: Path) -> None:
    """A full bootstrap (status + smoke + hint + probes) records every step."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        method = request["method"]
        if method == "challenge.status":
            assert request["params"] == {"challenge_id": CHALLENGE_ID}
            return rpc_result(request, STATUS_JSON)
        if method == "flag.submit":
            assert request["params"] == {"challenge_id": CHALLENGE_ID, "flag": "flag{smoke}"}
            return rpc_result(request, SUBMISSION_JSON)
        if method == "hint.request":
            assert request["params"] == {"challenge_id": CHALLENGE_ID, "index": 0}
            return rpc_result(request, HINT_JSON)
        raise AssertionError(f"unexpected method {method}")

    async def scenario(server: Any) -> None:
        fake = _FakeProbeRunner()
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={
                "OZZGRAPH_CHALLENGE_ID": CHALLENGE_ID,
                "OZZGRAPH_SMOKE_FLAG": "flag{smoke}",
                "OZZGRAPH_TARGET": "http://10.0.0.5:8080",
                "OZZGRAPH_TARGET_DNS": "challenge.local",
            },
            probe=fake,
        )
        await runner.run()
        records = _records(log)
        assert _event_types(records) == [
            BOOTSTRAP_TARGETS_PARSED,
            BOOTSTRAP_CHALLENGE_STATUS,
            BOOTSTRAP_SMOKE_SUBMITTED,
            BOOTSTRAP_HINT_REQUESTED,
            BOOTSTRAP_REACHABILITY,
            BOOTSTRAP_PROBE_RUN,
            BOOTSTRAP_REACHABILITY,
            BOOTSTRAP_PROBE_RUN,
        ]
        for record in records:
            assert record["producer"] == "bootstrap"
            assert record["run_id"] == "run-1"
        assert [call[0] for call in fake.calls] == ["http", "dns"]

        parsed = records[0]["payload"]
        assert isinstance(parsed, dict)
        assert parsed["single"] == "http://10.0.0.5:8080"
        assert parsed["namespaced"] == {"DNS": "challenge.local"}

        status = records[1]["payload"]
        assert isinstance(status, dict)
        assert status["challenge_id"] == CHALLENGE_ID
        assert status["solved"] is False
        assert status["attempts"] == 2

        smoke = records[2]["payload"]
        assert isinstance(smoke, dict)
        assert smoke["accepted"] is True
        assert smoke["points"] == 100

        hint = records[3]["payload"]
        assert isinstance(hint, dict)
        assert hint["index"] == 0
        assert hint["paid"] is False

        reachability = _payloads(records, BOOTSTRAP_REACHABILITY)
        assert [p["status"] for p in reachability] == ["reachable", "reachable"]
        assert [p["category"] for p in reachability] == ["http", "dns"]

        probes = _payloads(records, BOOTSTRAP_PROBE_RUN)
        assert probes[0]["command"] == "curl -sS --max-time 5 -I http://10.0.0.5:8080"
        assert probes[1]["command"] == "dig +short +time=2 +tries=1 challenge.local A"
        assert probes[1]["exit_code"] == 0

    run_mcp(handler, scenario)


def test_smoke_flag_absent_skips_submission(run_mcp: Any, tmp_path: Path) -> None:
    """Without OZZGRAPH_SMOKE_FLAG no flag.submit reaches the wire."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        method = request["method"]
        if method == "challenge.status":
            return rpc_result(request, STATUS_JSON)
        if method == "hint.request":
            return rpc_result(request, HINT_JSON)
        raise AssertionError(f"unexpected method {method}")

    async def scenario(server: Any) -> None:
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={"OZZGRAPH_CHALLENGE_ID": CHALLENGE_ID},
            probe=_FakeProbeRunner(),
        )
        await runner.run()
        records = _records(log)
        assert BOOTSTRAP_SMOKE_SUBMITTED not in _event_types(records)
        methods = [request["method"] for request in server.requests]
        assert "flag.submit" not in methods

    run_mcp(handler, scenario)


def test_smoke_flag_without_challenge_id_fails_loudly(run_mcp: Any, tmp_path: Path) -> None:
    """A smoke flag cannot be honored without a challenge id: ConfigError."""

    async def scenario(server: Any) -> None:
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={"OZZGRAPH_SMOKE_FLAG": "flag{smoke}"},
            probe=_FakeProbeRunner(),
        )
        with pytest.raises(ConfigError, match="OZZGRAPH_CHALLENGE_ID"):
            await runner.run()
        records = _records(log)
        assert BOOTSTRAP_TARGETS_PARSED in _event_types(records)
        failed = _payloads(records, BOOTSTRAP_FAILED)
        assert len(failed) == 1
        assert failed[0]["error_type"] == "ConfigError"
        assert server.request_count == 0

    run_mcp(lambda request: rpc_result(request, {}), scenario)


def test_free_hint_unavailable_path(run_mcp: Any, tmp_path: Path) -> None:
    """A failing free-hint request records hint_unavailable, not a crash."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        method = request["method"]
        if method == "challenge.status":
            return rpc_result(request, STATUS_JSON)
        if method == "hint.request":
            return rpc_error(request, -32000, "no hints available for this challenge")
        raise AssertionError(f"unexpected method {method}")

    async def scenario(server: Any) -> None:
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={"OZZGRAPH_CHALLENGE_ID": CHALLENGE_ID},
            probe=_FakeProbeRunner(),
        )
        await runner.run()
        records = _records(log)
        unavailable = [r for r in records if r["event_type"] == BOOTSTRAP_HINT_UNAVAILABLE]
        assert len(unavailable) == 1
        payload = unavailable[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["index"] == 0
        assert "error" in payload
        assert BOOTSTRAP_HINT_REQUESTED not in _event_types(records)

    run_mcp(handler, scenario)


def test_status_failure_is_recorded_not_fatal(run_mcp: Any, tmp_path: Path) -> None:
    """A Hal outage during status retrieval is an event, not a crash."""

    def handler(request: dict[str, Any]) -> Any:
        return (503, {"error": {"message": "overloaded"}})

    async def scenario(server: Any) -> None:
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={"OZZGRAPH_CHALLENGE_ID": CHALLENGE_ID},
            probe=_FakeProbeRunner(),
        )
        await runner.run()
        records = _records(log)
        statuses = _payloads(records, BOOTSTRAP_CHALLENGE_STATUS)
        assert len(statuses) == 1
        assert "error" in statuses[0]
        assert statuses[0]["error"] is not None
        assert server.request_count >= 1

    run_mcp(handler, scenario)


# --------------------------------------------------------------------------
# Reachability and probes
# --------------------------------------------------------------------------


def test_reachability_failure_path(run_mcp: Any, tmp_path: Path) -> None:
    """An unreachable target records unreachable status and probe detail."""

    async def scenario(server: Any) -> None:
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={"OZZGRAPH_TARGET_DNS": "challenge.local"},
            probe=_FakeProbeRunner(
                ProbeResult(status="unreachable", detail="exit_code=9", exit_code=9)
            ),
        )
        await runner.run()
        assert server.request_count == 0  # no challenge id: no Hal calls
        records = _records(log)
        reachability = _payloads(records, BOOTSTRAP_REACHABILITY)
        assert len(reachability) == 1
        assert reachability[0]["status"] == "unreachable"
        assert reachability[0]["target"] == "challenge.local"
        assert reachability[0]["category"] == "dns"
        probe = _payloads(records, BOOTSTRAP_PROBE_RUN)[0]
        assert probe["exit_code"] == 9
        assert probe["timeout"] is False

    run_mcp(lambda request: rpc_result(request, {}), scenario)


def test_fail_closed_on_empty_allowlist(run_mcp: Any, tmp_path: Path) -> None:
    """An empty target allowlist blocks every probe (fail closed)."""

    async def scenario(server: Any) -> None:
        runner, log, _ = _runner(
            tmp_path,
            server,
            environ={"OZZGRAPH_TARGET_HTTP": "http://10.0.0.5:8080"},
            target_allowlist=(),
        )
        await runner.run()
        records = _records(log)
        reachability = _payloads(records, BOOTSTRAP_REACHABILITY)
        assert len(reachability) == 1
        assert reachability[0]["status"] == "blocked"
        assert "allowlist" in str(reachability[0]["detail"]).lower()
        probe = _payloads(records, BOOTSTRAP_PROBE_RUN)[0]
        assert probe["exit_code"] is None  # never executed

    run_mcp(lambda request: rpc_result(request, {}), scenario)


def test_probe_specs_are_fixed_and_bounded() -> None:
    """Every probe category has a fixed command with explicit bounds."""
    assert set(PROBE_SPECS) == {"http", "https", "dns"}
    for spec in PROBE_SPECS.values():
        assert spec.timeout_seconds > 0
        assert spec.stdout_limit > 0
        assert spec.stderr_limit > 0
        assert "{target}" in spec.command


# --------------------------------------------------------------------------
# Supervisor wiring
# --------------------------------------------------------------------------


def _read_records(state_dir: Path) -> list[dict[str, object]]:
    path = state_dir / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_supervisor_runs_bootstrap_before_main_loop(monkeypatch: Any, tmp_path: Path) -> None:
    """Supervisor.run() records bootstrap events before termination."""
    monkeypatch.setenv("OZZGRAPH_TARGET_HTTP", "http://10.0.0.5:8080")
    supervisor = Supervisor(
        _config(tmp_path, max_runtime_s=1, heartbeat_interval_s=1, target_allowlist=())
    )
    reason = asyncio.run(supervisor.run())
    assert reason == TerminationReason.BUDGET_EXHAUSTED
    records = _read_records(tmp_path / "state")
    types = _event_types(records)
    assert BOOTSTRAP_TARGETS_PARSED in types
    assert BOOTSTRAP_REACHABILITY in types
    assert BOOTSTRAP_PROBE_RUN in types
    assert types.index(BOOTSTRAP_TARGETS_PARSED) < types.index(BOOTSTRAP_REACHABILITY)
    assert types.index(BOOTSTRAP_REACHABILITY) < types.index(BOOTSTRAP_PROBE_RUN)
    assert types[-1] == "termination"
    reachability = _payloads(records, BOOTSTRAP_REACHABILITY)[0]
    assert reachability["status"] == "blocked"  # empty allowlist fails closed


def test_supervisor_terminates_failed_on_bootstrap_config_error(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A malformed target variable ends the run with FAILED, loudly."""
    monkeypatch.setenv("OZZGRAPH_TARGET_SMTP", "mail.example")
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=1, heartbeat_interval_s=1))
    reason = asyncio.run(supervisor.run())
    assert reason == TerminationReason.FAILED
    records = _read_records(tmp_path / "state")
    types = _event_types(records)
    assert BOOTSTRAP_FAILED in types
    assert types[-1] == "termination"
    assert records[-1]["payload"] == {"reason": "failed"}
