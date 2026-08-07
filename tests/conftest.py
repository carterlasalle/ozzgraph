"""Shared pytest fixtures for the PR6 MCP client and halctl tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator

import pytest
from mcp_fake import FakeMcpServer, McpHandler

Scenario = Callable[[FakeMcpServer], Awaitable[None]]


@pytest.fixture
def run_mcp() -> Callable[[McpHandler, Scenario], None]:
    """Run an async scenario against a fresh FakeMcpServer in one loop."""

    def _run(handler: McpHandler, scenario: Scenario) -> None:
        async def _main() -> None:
            server = FakeMcpServer(handler)
            await server.start()
            try:
                await scenario(server)
            finally:
                await server.stop()

        asyncio.run(_main())

    return _run


@pytest.fixture
def threaded_mcp() -> Iterator[Callable[[McpHandler], FakeMcpServer]]:
    """Start a FakeMcpServer in a background loop for CLI (sync) tests."""
    servers: list[FakeMcpServer] = []

    def _start(handler: McpHandler) -> FakeMcpServer:
        server = FakeMcpServer(handler)
        server.start_threaded()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.stop_threaded()
