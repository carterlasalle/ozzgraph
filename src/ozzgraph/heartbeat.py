"""Heartbeat emitter for OzzGraph (PR3).

The supervisor starts a heartbeat task before long-running operations so an
external observer can tell the process is alive and progressing
(TECHNICAL_REQUIREMENTS: "start heartbeat before long operations"). The
emitter prints a ``HEARTBEAT ...`` line at a fixed interval until stopped.

The emitter is a plain asyncio task with an explicit stop event; it holds no
global mutable state. The sleep step is injectable so tests can drive the
loop deterministically without waiting on a real clock.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

Sleeper = Callable[[float], Awaitable[None]]


class Heartbeat:
    """Emits a periodic progress line.

    Args:
        interval_s: Seconds between heartbeat lines (must be > 0).
        summary: Callable returning a short budget/progress summary to append
            after ``uptime``. Optional.
        clock: Monotonic clock; overridable for deterministic tests.
        sleeper: Awaitable used to wait out the interval; defaults to
            ``asyncio.sleep``. Overridable for deterministic tests.
    """

    def __init__(
        self,
        interval_s: float,
        summary: Callable[[], str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._interval_s = float(interval_s)
        self._summary = summary
        self._clock = clock
        self._sleeper = sleeper
        self._started_at = clock()
        self._stop_event = asyncio.Event()

    def _render(self) -> str:
        uptime = self._clock() - self._started_at
        suffix = f" {self._summary()}" if self._summary is not None else ""
        return f"HEARTBEAT uptime={uptime:.1f}s{suffix}"

    async def run(self) -> None:
        """Emit a heartbeat line every ``interval_s`` until stopped.

        The loop re-checks the stop event after each wake-up so
        :meth:`stop` takes effect promptly rather than waiting for the next
        interval to elapse.
        """
        while not self._stop_event.is_set():
            await self._sleeper(self._interval_s)
            if not self._stop_event.is_set():
                print(self._render(), flush=True)

    def stop(self) -> None:
        """Signal the emitter to stop after the current wake-up."""
        self._stop_event.set()
