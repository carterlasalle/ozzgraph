"""CLI tests for the halctl adapter (PR6).

Every subcommand must emit exactly one parseable JSON document on stdout and
exit non-zero on failure (normalized JSON error shape). The fake MCP server
runs in a background event-loop thread because ``halctl.main`` owns its own
event loop.
"""

from __future__ import annotations

import json
from typing import Any

from mcp_fake import rpc_result

from ozzgraph.hal_client import (
    HAL_PRIVILEGED_ENV,
    MCP_BASE_URL_ENV,
    MCP_MAX_RETRIES_ENV,
    MCP_TIMEOUT_ENV,
)
from ozzgraph.halctl import CHALLENGE_ID_ENV, main

CHALLENGE_JSON: dict[str, object] = {
    "id": "web-01",
    "title": "Baby Web",
    "description": "Find the flag in the source.",
    "category": "web",
    "points": 100,
    "solved": False,
    "hint_count": 2,
    "files": ["http://target/robots.txt"],
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
    "entries": [{"rank": 1, "user_id": "alice", "points": 900, "solved": 9}]
}


def _configure_env(
    monkeypatch: Any, server: Any, *, privileged: bool = False, challenge_id: str | None = "web-01"
) -> None:
    """Point halctl at the fake server with fast, deterministic settings."""
    monkeypatch.setenv(MCP_BASE_URL_ENV, server.base_url)
    monkeypatch.setenv(MCP_TIMEOUT_ENV, "5")
    monkeypatch.setenv(MCP_MAX_RETRIES_ENV, "0")
    monkeypatch.delenv(HAL_PRIVILEGED_ENV, raising=False)
    monkeypatch.delenv(CHALLENGE_ID_ENV, raising=False)
    if privileged:
        monkeypatch.setenv(HAL_PRIVILEGED_ENV, "1")
    if challenge_id is not None:
        monkeypatch.setenv(CHALLENGE_ID_ENV, challenge_id)


def test_challenge_show_emits_json(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """halctl challenge show --json prints one parseable JSON document."""
    server = threaded_mcp(lambda request: rpc_result(request, CHALLENGE_JSON))
    _configure_env(monkeypatch, server)

    code = main(["challenge", "show", "--json", "--challenge-id", "web-01"])

    assert code == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["id"] == "web-01"
    assert doc["title"] == "Baby Web"
    assert doc["points"] == 100
    assert doc["schema_version"] == 1


def test_status_uses_env_challenge_id(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """halctl status --json falls back to OZZGRAPH_CHALLENGE_ID."""
    server = threaded_mcp(lambda request: rpc_result(request, STATUS_JSON))
    _configure_env(monkeypatch, server)

    code = main(["status", "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["challenge_id"] == "web-01"
    assert doc["attempts"] == 2
    assert doc["solved"] is False


def test_submit_privileged(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """A privileged halctl submits a flag and prints the verdict."""
    server = threaded_mcp(lambda request: rpc_result(request, SUBMISSION_JSON))
    _configure_env(monkeypatch, server, privileged=True)

    code = main(["submit", "--flag", "flag{smoke}", "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["accepted"] is True
    assert doc["message"] == "Correct!"


def test_submit_denied_without_privilege(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """A non-privileged halctl cannot submit: JSON error + non-zero exit."""
    requests: list[dict[str, Any]] = []
    server = threaded_mcp(
        lambda request: requests.append(request) or rpc_result(request, SUBMISSION_JSON)
    )
    _configure_env(monkeypatch, server, privileged=False)

    code = main(["submit", "--flag", "flag{smoke}", "--json"])

    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["type"] == "HalPrivilegeError"
    assert "submit_flag" in doc["error"]["message"]
    assert requests == []  # the privileged method never reached the wire


def test_hint_zero_free_but_paid_hint_guarded(
    threaded_mcp: Any, monkeypatch: Any, capsys: Any
) -> None:
    """Free hint 0 works; a paid hint is denied without privilege."""
    server = threaded_mcp(lambda request: rpc_result(request, HINT_JSON))
    _configure_env(monkeypatch, server, privileged=False)

    code = main(["hint", "--index", "0", "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["index"] == 0
    assert doc["hint"] == "Inspect the HTML"

    code = main(["hint", "--index", "1", "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["type"] == "HalPrivilegeError"


def test_scoreboard_emits_json(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """halctl scoreboard --json prints the normalized scoreboard."""
    server = threaded_mcp(lambda request: rpc_result(request, SCOREBOARD_JSON))
    _configure_env(monkeypatch, server)

    code = main(["scoreboard", "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["entries"][0]["user_id"] == "alice"
    assert doc["entries"][0]["points"] == 900


def test_exit_privileged(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """halctl exit --reason prints the exit confirmation (JSON)."""
    server = threaded_mcp(lambda request: rpc_result(request, {"ok": True}))
    _configure_env(monkeypatch, server, privileged=True)

    code = main(["exit", "--reason", "solved", "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"exited": True, "reason": "solved"}


def test_exit_without_json_flag_still_emits_json(
    threaded_mcp: Any, monkeypatch: Any, capsys: Any
) -> None:
    """The doc example `halctl exit --reason solved` still emits one JSON doc."""
    server = threaded_mcp(lambda request: rpc_result(request, {"ok": True}))
    _configure_env(monkeypatch, server, privileged=True)

    code = main(["exit", "--reason", "solved"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["exited"] is True


def test_exit_denied_without_privilege(threaded_mcp: Any, monkeypatch: Any, capsys: Any) -> None:
    """A non-privileged halctl cannot exit the run."""
    server = threaded_mcp(lambda request: rpc_result(request, {"ok": True}))
    _configure_env(monkeypatch, server, privileged=False)

    code = main(["exit", "--reason", "solved", "--json"])

    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["type"] == "HalPrivilegeError"


def test_missing_challenge_id_is_usage_error(
    threaded_mcp: Any, monkeypatch: Any, capsys: Any
) -> None:
    """A challenge-scoped command without an id fails with a usage error."""
    server = threaded_mcp(lambda request: rpc_result(request, STATUS_JSON))
    _configure_env(monkeypatch, server, challenge_id=None)

    code = main(["status", "--json"])

    assert code == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["type"] == "usage"
    assert "challenge id required" in doc["error"]["message"]


def test_service_failure_emits_normalized_error(
    threaded_mcp: Any, monkeypatch: Any, capsys: Any
) -> None:
    """A provider failure prints the normalized HalServiceError shape."""
    server = threaded_mcp(lambda request: (503, {"error": {"message": "overloaded"}}))
    _configure_env(monkeypatch, server)

    code = main(["challenge", "show", "--json"])

    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    error = doc["error"]
    assert error["type"] == "HalServiceError"
    assert error["provider"] == "halctf"
    assert error["status_code"] == 503
    assert error["retryable"] is True
    assert "overloaded" in error["message"]


def test_client_config_error_emits_normalized_error(
    threaded_mcp: Any, monkeypatch: Any, capsys: Any
) -> None:
    """Invalid configuration fails loudly with a JSON error document."""
    server = threaded_mcp(lambda request: rpc_result(request, CHALLENGE_JSON))
    _configure_env(monkeypatch, server)
    monkeypatch.setenv(MCP_BASE_URL_ENV, "ftp://nope")

    code = main(["scoreboard", "--json"])

    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"]["type"] == "ValueError"
    assert "base_url" in doc["error"]["message"]
