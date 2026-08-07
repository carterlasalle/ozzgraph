"""Unit and integration tests for the HalCTF MCP client (PR6).

Contract + integration coverage per docs/TESTING_AND_QA.md, driven against a
fake JSON-RPC 2.0 MCP server over asyncio streams (``mcp_fake.py``):
retrieve challenge, submit smoke flag, transient failure then retry, 4xx
never retried, privileged guard, graceful exit, hal_failure events, and
environment parsing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp_fake import rpc_error, rpc_result
from pydantic import ValidationError

from ozzgraph.events import EventLog
from ozzgraph.hal_client import (
    DEFAULT_MCP_BASE_URL,
    HAL_PRIVILEGED_ENV,
    MAX_RETRY_LIMIT,
    MCP_BASE_URL_ENV,
    MCP_MAX_RETRIES_ENV,
    MCP_TIMEOUT_ENV,
    Challenge,
    ChallengeStatus,
    HalClient,
    HalPrivilegeError,
    HalServiceError,
    HintResult,
    Scoreboard,
    SubmissionResult,
)

CHALLENGE_JSON: dict[str, object] = {
    "id": "web-01",
    "title": "Baby Web",
    "description": "Find the flag in the source.",
    "category": "web",
    "points": 100,
    "solved": False,
    "hint_count": 2,
    "files": ["http://target/robots.txt"],
    # Unknown upstream fields are dropped during normalization.
    "upstream_only_field": "ignored",
}

STATUS_JSON: dict[str, object] = {
    "challenge_id": "web-01",
    "solved": False,
    "attempts": 2,
    "hints_used": 1,
    "points_earned": 0,
    "updated_at": "2026-08-07T00:00:00Z",
}

SUBMISSION_JSON: dict[str, object] = {
    "challenge_id": "web-01",
    "accepted": True,
    "message": "Correct!",
    "points": 100,
    "attempts_remaining": 5,
}

HINT_JSON: dict[str, object] = {
    "challenge_id": "web-01",
    "index": 0,
    "hint": "Inspect the HTML",
    "paid": False,
}

SCOREBOARD_JSON: dict[str, object] = {
    "entries": [
        {"rank": 1, "user_id": "alice", "points": 900, "solved": 9},
        {"rank": 2, "user_id": "bob", "points": 800, "solved": 8},
    ]
}


class _NoopSleeper:
    """Backoff sleeper that returns immediately (deterministic tests)."""

    async def __call__(self, _: float) -> None:
        return None


def _client(server: Any, **kwargs: Any) -> HalClient:
    """Build a HalClient pointed at the fake server with no-op backoff."""
    return HalClient(base_url=server.base_url, sleeper=_NoopSleeper(), **kwargs)


def test_retrieve_challenge_success(run_mcp: Any) -> None:
    """get_challenge returns the normalized internal Challenge schema."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        assert request["method"] == "challenge.get"
        assert request["params"] == {"challenge_id": "web-01"}
        return rpc_result(request, CHALLENGE_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            challenge = await client.get_challenge("web-01")
        assert isinstance(challenge, Challenge)
        assert challenge.schema_version == 1
        assert challenge.id == "web-01"
        assert challenge.title == "Baby Web"
        assert challenge.category == "web"
        assert challenge.points == 100
        assert challenge.solved is False
        assert challenge.hint_count == 2
        assert challenge.files == ["http://target/robots.txt"]

    run_mcp(handler, scenario)


def test_get_status_and_free_hint(run_mcp: Any) -> None:
    """get_status and a free hint (index 0) work without privileges."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        if request["method"] == "challenge.status":
            return rpc_result(request, STATUS_JSON)
        if request["method"] == "hint.request":
            return rpc_result(request, HINT_JSON)
        raise AssertionError(f"unexpected method {request['method']}")

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            status = await client.get_status("web-01")
            hint = await client.request_hint("web-01", 0)
        assert isinstance(status, ChallengeStatus)
        assert status.challenge_id == "web-01"
        assert status.solved is False
        assert status.attempts == 2
        assert status.hints_used == 1
        assert status.points_earned == 0
        assert isinstance(hint, HintResult)
        assert hint.index == 0
        assert hint.hint == "Inspect the HTML"
        assert hint.paid is False

    run_mcp(handler, scenario)


def test_submit_smoke_flag_privileged(run_mcp: Any) -> None:
    """A privileged client submits a smoke flag and gets the verdict."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        assert request["method"] == "flag.submit"
        assert request["params"] == {"challenge_id": "web-01", "flag": "flag{smoke}"}
        return rpc_result(request, SUBMISSION_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server, privileged=True) as client:
            result = await client.submit_flag("web-01", "flag{smoke}")
        assert isinstance(result, SubmissionResult)
        assert result.accepted is True
        assert result.message == "Correct!"
        assert result.points == 100
        assert result.attempts_remaining == 5

    run_mcp(handler, scenario)


def test_scoreboard(run_mcp: Any) -> None:
    """get_scoreboard returns normalized scoreboard entries."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        assert request["method"] == "scoreboard.get"
        return rpc_result(request, SCOREBOARD_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            scoreboard = await client.get_scoreboard()
        assert isinstance(scoreboard, Scoreboard)
        assert [entry.rank for entry in scoreboard.entries] == [1, 2]
        assert scoreboard.entries[0].user_id == "alice"
        assert scoreboard.entries[0].points == 900
        assert scoreboard.entries[1].solved == 8

    run_mcp(handler, scenario)


def test_graceful_exit_privileged(run_mcp: Any) -> None:
    """A privileged client signals a graceful exit with a reason."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        assert request["method"] == "exit"
        assert request["params"] == {"reason": "solved"}
        return rpc_result(request, {"ok": True})

    async def scenario(server: Any) -> None:
        async with _client(server, privileged=True) as client:
            await client.graceful_exit("solved")

    run_mcp(handler, scenario)


def test_transient_503_retried_then_succeeds(run_mcp: Any) -> None:
    """A transient 503 is retried with backoff, then the call succeeds."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            return (503, {"error": {"message": "overloaded"}})
        return rpc_result(request, CHALLENGE_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            challenge = await client.get_challenge("web-01")
        assert challenge.id == "web-01"

    run_mcp(handler, scenario)
    assert calls == 3


def test_429_retried_then_succeeds(run_mcp: Any) -> None:
    """HTTP 429 is transient and retried."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (429, {"error": {"message": "slow down"}})
        return rpc_result(request, STATUS_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            status = await client.get_status("web-01")
        assert status.challenge_id == "web-01"

    run_mcp(handler, scenario)
    assert calls == 2


def test_4xx_never_retried(run_mcp: Any) -> None:
    """A 401 fails immediately: one attempt, non-retryable typed error."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return (401, {"error": {"message": "bad key"}})

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("web-01")
            error = excinfo.value
            assert error.status_code == 401
            assert error.retryable is False
            assert error.provider == "halctf"
            assert "bad key" in error.message

    run_mcp(handler, scenario)
    assert calls == 1


def test_404_never_retried(run_mcp: Any) -> None:
    """A 404 fails immediately (never retried) with a typed error."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return (404, {"error": {"message": "no such challenge"}})

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("missing")
        assert excinfo.value.status_code == 404
        assert excinfo.value.retryable is False

    run_mcp(handler, scenario)
    assert calls == 1


def test_jsonrpc_application_error_never_retried(run_mcp: Any) -> None:
    """An application JSON-RPC error (wrong flag) fails immediately."""
    calls = 0

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return rpc_error(request, -32000, "incorrect flag")

    async def scenario(server: Any) -> None:
        async with _client(server, privileged=True) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.submit_flag("web-01", "flag{nope}")
            error = excinfo.value
            assert error.status_code == 200
            assert error.retryable is False
            assert "incorrect flag" in error.message

    run_mcp(handler, scenario)
    assert calls == 1


def test_jsonrpc_internal_error_retried(run_mcp: Any) -> None:
    """JSON-RPC -32603 (internal error) is transient and retried."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            return rpc_error(request, -32603, "internal server error")
        return rpc_result(request, STATUS_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            status = await client.get_status("web-01")
        assert status.challenge_id == "web-01"

    run_mcp(handler, scenario)
    assert calls == 3


def test_connection_refused_retried_then_fails(run_mcp: Any) -> None:
    """Transport failures retry, then raise a retryable typed error."""
    calls = 0

    def handler(request: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        nonlocal calls
        calls += 1
        return rpc_result(request, CHALLENGE_JSON)

    async def scenario(server: Any) -> None:
        port = server.port
        await server.stop()
        async with HalClient(
            base_url=f"http://127.0.0.1:{port}", sleeper=_NoopSleeper(), max_retries=2
        ) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("web-01")
            error = excinfo.value
            assert error.status_code is None
            assert error.retryable is True
            assert error.provider == "halctf"
            assert "transport failure" in error.message

    run_mcp(handler, scenario)
    assert calls == 0  # server was stopped; nothing was handled


def test_timeout_retried_then_fails(run_mcp: Any) -> None:
    """A slow server times out; the timeout is transient and retried."""

    async def handler(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.3)
        return rpc_result(request, CHALLENGE_JSON)

    async def scenario(server: Any) -> None:
        async with HalClient(
            base_url=server.base_url,
            timeout_s=0.05,
            max_retries=1,
            sleeper=_NoopSleeper(),
        ) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("web-01")
            error = excinfo.value
            assert error.status_code is None
            assert error.retryable is True
            assert "transport failure" in error.message

    run_mcp(handler, scenario)


def test_malformed_response_raises_typed_parse_error(run_mcp: Any) -> None:
    """A non-object JSON-RPC result fails loudly, never retried."""
    calls = 0

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"jsonrpc": "2.0", "id": request["id"], "result": ["not", "an", "object"]}

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("web-01")
            error = excinfo.value
            assert error.status_code == 200
            assert error.retryable is False
            assert "must be an object" in error.message

    run_mcp(handler, scenario)
    assert calls == 1


def test_missing_schema_field_fails_loudly(run_mcp: Any) -> None:
    """A result missing a required field fails loudly (no silent defaults)."""
    calls = 0

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        broken = dict(CHALLENGE_JSON)
        del broken["id"]
        return rpc_result(request, broken)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("web-01")
            error = excinfo.value
            assert error.retryable is False
            assert "invalid challenge.get result" in error.message

    run_mcp(handler, scenario)
    assert calls == 1


def test_privileged_guard_denies_non_supervisor(run_mcp: Any) -> None:
    """submit_flag/paid hint/graceful_exit raise HalPrivilegeError by default."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("privileged method must never reach the wire")

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            with pytest.raises(HalPrivilegeError, match="submit_flag"):
                await client.submit_flag("web-01", "flag{x}")
            with pytest.raises(HalPrivilegeError, match="request_hint"):
                await client.request_hint("web-01", 1)
            with pytest.raises(HalPrivilegeError, match="graceful_exit"):
                await client.graceful_exit("solved")

    run_mcp(handler, scenario)


def test_free_hint_open_but_paid_hint_guarded(run_mcp: Any) -> None:
    """Hint zero is free; paid hints (index > 0) are supervisor-only."""

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        return rpc_result(request, HINT_JSON)

    async def scenario(server: Any) -> None:
        async with _client(server) as client:
            hint = await client.request_hint("web-01", 0)
            assert hint.index == 0
            with pytest.raises(HalPrivilegeError):
                await client.request_hint("web-01", 1)

    run_mcp(handler, scenario)


def test_privileged_client_can_submit(run_mcp: Any) -> None:
    """A privileged client may invoke all supervisor-only methods."""
    seen: list[str] = []

    def handler(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(request["method"])
        if request["method"] == "flag.submit":
            return rpc_result(request, SUBMISSION_JSON)
        if request["method"] == "hint.request":
            index = request["params"]["index"]
            hint_result = dict(HINT_JSON)
            hint_result["index"] = index
            hint_result["paid"] = index > 0
            return rpc_result(request, hint_result)
        if request["method"] == "exit":
            return rpc_result(request, {"ok": True})
        raise AssertionError(f"unexpected method {request['method']}")

    async def scenario(server: Any) -> None:
        async with _client(server, privileged=True) as client:
            submission = await client.submit_flag("web-01", "flag{x}")
            hint = await client.request_hint("web-01", 2)
            await client.graceful_exit("solved")
        assert submission.accepted is True
        assert hint.index == 2
        assert hint.paid is True

    run_mcp(handler, scenario)
    assert seen == ["flag.submit", "hint.request", "exit"]


def test_max_retries_zero_disables_retry(run_mcp: Any) -> None:
    """max_retries=0 disables retries: one attempt, immediate typed error."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return (503, {"error": {"message": "overloaded"}})

    async def scenario(server: Any) -> None:
        async with _client(server, max_retries=0) as client:
            with pytest.raises(HalServiceError) as excinfo:
                await client.get_challenge("web-01")
            assert excinfo.value.status_code == 503
            assert excinfo.value.retryable is True

    run_mcp(handler, scenario)
    assert calls == 1


def test_failure_event_emitted_after_exhausted_retries(tmp_path: Path, run_mcp: Any) -> None:
    """Exhausted retries append a hal_failure event to the event log."""
    calls = 0

    def handler(request: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return (500, {"error": {"message": "boom"}})

    log = EventLog(tmp_path / "actions.jsonl")

    async def scenario(server: Any) -> None:
        async with _client(server, event_log=log, run_id="run-7") as client:
            with pytest.raises(HalServiceError):
                await client.get_challenge("web-01")

    run_mcp(handler, scenario)
    assert calls == 4  # 1 initial attempt + 3 retries
    records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    event = records[0]
    assert event["event_type"] == "hal_failure"
    assert event["producer"] == "hal_client"
    assert event["run_id"] == "run-7"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["provider"] == "halctf"
    assert payload["status"] == 500
    assert payload["attempts"] == 4


def test_privilege_denial_emits_no_failure_event(tmp_path: Path, run_mcp: Any) -> None:
    """A local privilege denial is not an integration failure: no event."""
    log = EventLog(tmp_path / "actions.jsonl")

    def handler(request: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("must not reach the wire")

    async def scenario(server: Any) -> None:
        async with _client(server, event_log=log) as client:
            with pytest.raises(HalPrivilegeError):
                await client.submit_flag("web-01", "flag{x}")

    run_mcp(handler, scenario)
    assert not log.path.exists()


def test_constructor_defaults_without_env(monkeypatch: Any) -> None:
    """Defaults apply when no env vars are set."""
    for name in (
        MCP_BASE_URL_ENV,
        MCP_TIMEOUT_ENV,
        MCP_MAX_RETRIES_ENV,
        HAL_PRIVILEGED_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    client = HalClient()
    assert client._base_url == DEFAULT_MCP_BASE_URL
    assert client._timeout_s == 60.0
    assert client._max_retries == 3
    assert client._privileged is False
    asyncio.run(client.aclose())


def test_constructor_defaults_read_from_env(monkeypatch: Any) -> None:
    """Env vars drive the constructor defaults."""
    monkeypatch.setenv(MCP_BASE_URL_ENV, "https://mcp.example.com/mcp/")
    monkeypatch.setenv(MCP_TIMEOUT_ENV, "7.5")
    monkeypatch.setenv(MCP_MAX_RETRIES_ENV, "2")
    monkeypatch.setenv(HAL_PRIVILEGED_ENV, "1")

    client = HalClient()
    assert client._base_url == "https://mcp.example.com/mcp"
    assert client._timeout_s == 7.5
    assert client._max_retries == 2
    assert client._privileged is True
    asyncio.run(client.aclose())


def test_privileged_env_true_variants(monkeypatch: Any) -> None:
    """Truthy OZZGRAPH_HAL_PRIVILEGED variants enable privileged methods."""
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(HAL_PRIVILEGED_ENV, value)
        assert HalClient()._privileged is True
        asyncio.run(HalClient().aclose())
    monkeypatch.setenv(HAL_PRIVILEGED_ENV, "garbage")
    assert HalClient()._privileged is False
    asyncio.run(HalClient().aclose())


def test_invalid_env_value_fails_loudly(monkeypatch: Any) -> None:
    """A non-numeric timeout env var fails loudly at construction."""
    monkeypatch.setenv(MCP_TIMEOUT_ENV, "soon")
    with pytest.raises(ValueError, match="OZZGRAPH_MCP_TIMEOUT_S"):
        HalClient()
    monkeypatch.setenv(MCP_TIMEOUT_ENV, "60")
    monkeypatch.setenv(MCP_MAX_RETRIES_ENV, "many")
    with pytest.raises(ValueError, match="OZZGRAPH_MCP_MAX_RETRIES"):
        HalClient()


def test_constructor_rejects_invalid_configuration() -> None:
    """Bogus base URLs, timeouts, and retry counts fail loudly."""
    with pytest.raises(ValueError):
        HalClient(base_url="ftp://nope")
    with pytest.raises(ValueError):
        HalClient(timeout_s=0)
    with pytest.raises(ValueError):
        HalClient(timeout_s=-1.0)
    with pytest.raises(ValueError):
        HalClient(max_retries=-1)
    with pytest.raises(ValueError):
        HalClient(max_retries=MAX_RETRY_LIMIT + 1)


def test_internal_models_validate() -> None:
    """The versioned internal schemas enforce their pydantic v2 contracts."""
    challenge = Challenge.model_validate(CHALLENGE_JSON)
    assert challenge.schema_version == 1
    assert challenge.title == "Baby Web"
    with pytest.raises(ValidationError):
        Challenge.model_validate({"id": "x", "points": -1})
    with pytest.raises(ValidationError):
        ChallengeStatus.model_validate({"challenge_id": "x", "solved": True})
    with pytest.raises(ValidationError):
        SubmissionResult.model_validate({"challenge_id": "x", "accepted": True, "points": -5})
    with pytest.raises(ValidationError):
        HintResult.model_validate({"challenge_id": "x", "index": -1, "hint": "h"})
