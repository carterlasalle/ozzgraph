"""Shared fake JSON-RPC 2.0 MCP server for PR6 tests.

A minimal HTTP-speaking JSON-RPC 2.0 server built on asyncio streams
(docs/TESTING_AND_QA.md: "fake MCP server"). It serves one HTTP request per
connection (``Connection: close``), parses the POST body as a JSON-RPC 2.0
request, and delegates the response to a caller-supplied handler. Handlers
may return either a full JSON-RPC response object or an
``(http_status, body)`` tuple to simulate HTTP-level failures (5xx, 4xx).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Callable
from typing import Any

# A handler maps a parsed JSON-RPC request to a response object or an
# (http_status, body) tuple.
McpHandler = Callable[[dict[str, Any]], Any]

_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class FakeMcpServer:
    """JSON-RPC 2.0 MCP server over asyncio streams (one request per conn)."""

    def __init__(self, handler: McpHandler) -> None:
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port = 0
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        """Server root URL (the client appends the /mcp endpoint path)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def request_count(self) -> int:
        return len(self.requests)

    async def start(self) -> None:
        """Bind and start serving in the caller's event loop."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self._server.sockets is not None
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Close the listener and all accepted connections."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def start_threaded(self) -> None:
        """Bind and serve in a background event-loop thread (CLI tests)."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="fake-mcp-server", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop_threaded(self) -> None:
        """Stop the background event loop and join its thread."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_threaded_server())
        self._loop.run_forever()

    async def _start_threaded_server(self) -> None:
        await self.start()
        self._ready.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header_block = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        content_length = 0
        for line in header_block.decode("latin-1").split("\r\n"):
            lowered = line.lower()
            if lowered.startswith("content-length:"):
                content_length = int(lowered.split(":", 1)[1].strip())
        try:
            body = await reader.readexactly(content_length)
            request = json.loads(body)
        except (asyncio.IncompleteReadError, json.JSONDecodeError):
            writer.close()
            return
        if not isinstance(request, dict):
            writer.close()
            return
        self.requests.append(request)
        try:
            response = self._handler(request)
            if inspect.iscoroutine(response):
                response = await response
        except Exception:  # noqa: BLE001 - defensive: keep the fake server alive
            response = (500, {"error": {"message": "fake server handler crashed"}})
        status = 200
        payload = response
        if isinstance(response, tuple):
            status, payload = response
        body_bytes = json.dumps(payload).encode("utf-8")
        status_text = _STATUS_TEXT.get(status, "OK")
        writer.write(
            (
                f"HTTP/1.1 {status} {status_text}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("latin-1")
        )
        writer.write(body_bytes)
        try:
            await writer.drain()
        except (ConnectionError, BrokenPipeError):  # pragma: no cover - client gone
            pass
        writer.close()


def rpc_result(request: dict[str, Any], result: Any) -> dict[str, Any]:
    """A well-formed JSON-RPC 2.0 success response echoing the request id."""
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def rpc_error(request: dict[str, Any], code: int, message: str) -> dict[str, Any]:
    """A well-formed JSON-RPC 2.0 error response echoing the request id."""
    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": code, "message": message},
    }
