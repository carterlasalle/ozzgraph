"""HAL-011 halctf-real-contract regression fixture (reusable data, no tests).

The existing benchmark/suite drives the kernel against synthetic lab
targets and scripted models, not an actual HalCTF runtime contract.
This module reproduces the REAL platform contract verified cross-repo
from kazuki005276ssh/halctf-team-tottori committed live-run logs (the
same source tests/test_halctf_adapter.py HAL-001 verified):

- the exact platform-injected env shape — named service pairs
  (``HAL_TARGET_FERRY_IP``/``HAL_TARGET_FERRY_PORT``,
  ``HAL_TARGET_UNDERWORLD_IP``/``HAL_TARGET_UNDERWORLD_PORT``),
  ``HAL_CHALLENGE_ID=18``, ``HAL_CHALLENGE_NAME``,
  ``HAL_CHALLENGE_CATEGORY``, runtime identity (``HAL_AGENT_MODEL`` /
  ``HAL_RUN_ID`` / ``HAL_TEAM_UUID``), flag-like env values, and the
  infrastructure endpoints (``OPENAI_BASE_URL`` / ``MCP_ENDPOINT``),
- the observed target HTTP surface — ``GET /fetch`` scripted statuses
  403 / 404 / 502 / 200 (the 200 path serves the challenge flag),
- the observed sidecar wire responses — ``POST /submit`` returns
  ``{"status": "correct", "points_awarded": 1}`` and ``POST /done``
  returns ``200``.

The fixture is reusable data: :func:`tottori_env` builds the env dict
and the server classes (:class:`HalctfTargetServer`,
:class:`ContractSidecarServer`, :class:`ScriptedModelServer`) are real
plain-HTTP listeners (stdlib ``http.server`` only — no new
dependencies) on ephemeral loopback ports, so a test can stand up the
whole contract and spawn ``python -m ozzgraph`` against it.

:data:`TOTTORI_FLAG` matches the value Tottori's live-run env carried
as ``FLAG_MAIN``; the flag is served ONLY by the 200 ``/fetch/ok``
path — never in the 403/404/502 bodies, never in the easy initial
surface.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlsplit

#: The challenge id Tottori's platform injected (HAL_CHALLENGE_ID).
TOTTORI_CHALLENGE_ID = "18"

#: Challenge metadata injected alongside the id.
TOTTORI_CHALLENGE_NAME = "underworld"
TOTTORI_CHALLENGE_CATEGORY = "web"

#: Runtime identity injected for the live run.
TOTTORI_AGENT_MODEL = "gpt-4o-mini"
TOTTORI_RUN_ID = "run-2026-08-09"
TOTTORI_TEAM_UUID = "team-42"

#: The challenge flag. The fixture serves it from the target's 200
#: ``/fetch/ok`` path; Tottori's live-run env carried the same value
#: as ``FLAG_MAIN``. The HalCTF flag extractor's generalized default
#: pattern (HAL-007) matches the ``flag{...}`` envelope.
TOTTORI_FLAG = "flag{underworld-main}"

#: The exact platform-observed ``/submit`` response (HAL-004 live-run
#: logs): status ``correct`` with one point awarded.
SIDECAR_SUBMIT_RESPONSE: dict[str, object] = {"status": "correct", "points_awarded": 1}

#: The observed target ``/fetch`` surface: one scripted status per
#: path. The 200 path is where the challenge flag lives.
FETCH_STATUSES: dict[str, int] = {
    "/fetch": 403,
    "/fetch/missing": 404,
    "/fetch/down": 502,
    "/fetch/ok": 200,
}

#: Body served for the scripted non-200 ``/fetch`` statuses.
_FETCH_ERROR_BODY = b'{"error": "request failed"}'

#: Flag-like env values the platform injects (BONUS_FLAG first, then
#: every FLAG_* variable, sorted — the HAL-001 snapshot order).
TOTTORI_FLAG_LIKE: tuple[str, ...] = ("flag{bonus}", TOTTORI_FLAG)


class _ThreadedHTTPServer(ThreadingHTTPServer):
    """A loopback-only threaded HTTP server (daemon threads, ephemeral port)."""

    daemon_threads = True
    allow_reuse_address = True


def _reply(
    handler: BaseHTTPRequestHandler, status: int, body: bytes, *, ctype: str = "application/json"
) -> None:
    """Write one HTTP response and silence request logging."""
    handler.send_response(status)
    handler.send_header("content-type", ctype)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _silent_log(handler: BaseHTTPRequestHandler, msg: str, *args: object) -> None:
    """Silence the stub servers' request logging (they are fixtures)."""


class HalctfTargetServer:
    """A real plain-HTTP listener reproducing the observed target surface.

    Serves the scripted ``/fetch`` statuses (:data:`FETCH_STATUSES`)
    plus a benign ``/`` page. Every request is recorded in
    :attr:`requests` so a test can prove the harness actually probed
    the service (no allowlist refusal). The 200 ``/fetch/ok`` path
    serves the challenge flag in the response body.
    """

    def __init__(self, *, service: str, flag: str = TOTTORI_FLAG) -> None:
        self.service = service
        self.flag = flag
        self.requests: list[tuple[str, str]] = []
        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _target_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        address = self._server.server_address
        return f"http://{address[0]}:{address[1]}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def _target_handler(server: HalctfTargetServer) -> type[BaseHTTPRequestHandler]:
    """The target's GET handler: record the request, serve the surface."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            server.requests.append((self.command, self.path))
            status = FETCH_STATUSES.get(self.path)
            if status is not None:
                if status == 200:
                    body = f"token: {server.flag}\n".encode()
                    _reply(self, 200, body, ctype="text/plain")
                else:
                    _reply(self, status, _FETCH_ERROR_BODY)
            elif self.path == "/":
                body = f"{server.service} service: reachable\n".encode()
                _reply(self, 200, body, ctype="text/plain")
            else:
                _reply(self, 404, _FETCH_ERROR_BODY)

        log_message = _silent_log  # type: ignore[assignment]

    return _Handler


class ContractSidecarServer:
    """The real plain-HTTP sidecar surface (the HAL-004 wire shape).

    ``POST /submit`` answers with the observed
    ``{"status": "correct", "points_awarded": 1}`` (configurable via
    ``submit_response`` for rejection-path tests) and records the body
    in :attr:`submits`; ``POST /done`` answers ``200`` and records the
    body in :attr:`dones`. Everything else (including the MCP ``/mcp``
    path, which shares the host:port in the real deployment) is a 404.
    """

    def __init__(self, *, submit_response: dict[str, object] | None = None) -> None:
        self._submit_response = (
            dict(SIDECAR_SUBMIT_RESPONSE) if submit_response is None else submit_response
        )
        self.submits: list[dict[str, object]] = []
        self.dones: list[dict[str, object]] = []
        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _sidecar_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        address = self._server.server_address
        return f"http://{address[0]}:{address[1]}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def _sidecar_handler(server: ContractSidecarServer) -> type[BaseHTTPRequestHandler]:
    """The sidecar handler: /submit and /done POSTs, everything else 404."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # The MCP server shares this origin in the real deployment;
            # the sidecar itself only speaks /submit and /done.
            _reply(self, 404, b'{"error": {"message": "not found"}}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            if self.path == "/submit":
                server.submits.append(payload)
                _reply(self, 200, json.dumps(server._submit_response).encode("utf-8"))
            elif self.path == "/done":
                server.dones.append(payload)
                _reply(self, 200, b'{"ok": true}')
            else:
                _reply(self, 404, b'{"error": {"message": "not found"}}')

        log_message = _silent_log  # type: ignore[assignment]

    return _Handler


class ScriptedModelServer:
    """A stub OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Serves the scripted completions in order (the same shape
    ``ModelResponse`` validates — tests/test_model_client.py), counting
    calls so a test can assert the child run consumed exactly the
    scripted script. ``GET /v1/models`` answers a minimal model list.
    Loopback-only, ephemeral port; ``OPENAI_BASE_URL`` in the fixture
    env points here (the HAL-003 model routing).
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.calls = 0
        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _model_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        address = self._server.server_address
        return f"http://{address[0]}:{address[1]}/v1"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _next_completion(self) -> str:
        with self._lock:
            self.calls += 1
            index = min(self.calls - 1, len(self._responses) - 1)
            return self._responses[index]


def _model_handler(server: ScriptedModelServer) -> type[BaseHTTPRequestHandler]:
    """POST /v1/chat/completions -> the next scripted completion."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/v1/models":
                body = json.dumps(
                    {"object": "list", "data": [{"id": "scripted", "owned_by": "fixture"}]}
                ).encode("utf-8")
                _reply(self, 200, body)
            else:
                _reply(self, 404, b'{"error": {"message": "not found"}}')

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            if self.path != "/v1/chat/completions":
                _reply(self, 404, b'{"error": {"message": "not found"}}')
                return
            payload = {
                "id": f"chatcmpl-hal011-{server.calls + 1}",
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
            _reply(self, 200, json.dumps(payload).encode("utf-8"))

        log_message = _silent_log  # type: ignore[assignment]

    return _Handler


def tottori_env(
    *,
    ferry_url: str,
    underworld_url: str,
    model_base_url: str,
    sidecar_base_url: str,
    mcp_endpoint: str,
) -> dict[str, str]:
    """The full platform-injected env shape (Tottori's live-run contract).

    Args:
        ferry_url: The real ferry service URL (``http://127.0.0.1:PORT``).
        underworld_url: The real underworld service URL.
        model_base_url: The model service base URL (``OPENAI_BASE_URL``
            — routed into the model client by HAL-003).
        sidecar_base_url: The sidecar origin (``OZZGRAPH_SIDECAR_BASE_URL``
            — env-first sidecar resolution, HAL-004).
        mcp_endpoint: The MCP endpoint (``MCP_ENDPOINT``); in the real
            deployment the sidecar shares its host:port.

    Returns:
        The platform env: named ``HAL_TARGET_<NAME>_IP`` /
        ``HAL_TARGET_<NAME>_PORT`` pairs, challenge metadata, runtime
        identity, flag-like values, and the model/MCP infrastructure
        endpoints — exactly the shape tests/test_halctf_adapter.py
        HAL-001 verified.
    """
    ferry_host, ferry_port = _split_url(ferry_url)
    underworld_host, underworld_port = _split_url(underworld_url)
    return {
        "HAL_TARGET_FERRY_IP": ferry_host,
        "HAL_TARGET_FERRY_PORT": str(ferry_port),
        "HAL_TARGET_UNDERWORLD_IP": underworld_host,
        "HAL_TARGET_UNDERWORLD_PORT": str(underworld_port),
        "HAL_CHALLENGE_ID": TOTTORI_CHALLENGE_ID,
        "HAL_CHALLENGE_NAME": TOTTORI_CHALLENGE_NAME,
        "HAL_CHALLENGE_CATEGORY": TOTTORI_CHALLENGE_CATEGORY,
        "HAL_AGENT_MODEL": TOTTORI_AGENT_MODEL,
        "HAL_RUN_ID": TOTTORI_RUN_ID,
        "HAL_TEAM_UUID": TOTTORI_TEAM_UUID,
        "BONUS_FLAG": TOTTORI_FLAG_LIKE[0],
        "FLAG_MAIN": TOTTORI_FLAG,
        "OPENAI_BASE_URL": model_base_url,
        "MCP_ENDPOINT": mcp_endpoint,
        "OZZGRAPH_SIDECAR_BASE_URL": sidecar_base_url,
    }


def _split_url(url: str) -> tuple[str, int]:
    """The (host, port) of an ``http://host:port`` fixture URL."""
    parts = urlsplit(url)
    assert parts.scheme == "http" and parts.hostname is not None and parts.port is not None
    return parts.hostname, parts.port
