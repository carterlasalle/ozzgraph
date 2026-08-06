"""Unit tests for the heartbeat emitter (PR3)."""

import asyncio

import pytest

from ozzgraph.heartbeat import Heartbeat


class _FakeClock:
    """Deterministic monotonic clock for testing uptime."""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = ticks
        self._i = 0

    def __call__(self) -> float:
        if self._i < len(self._ticks):
            value = self._ticks[self._i]
            self._i += 1
            return value
        return self._ticks[-1]


class _PausedSleeper:
    """Sleeper that blocks until the driver releases it, one step at a time.

    This makes the loop deterministic: the test controls exactly when each
    "interval" elapses.
    """

    def __init__(self) -> None:
        self._release = asyncio.Event()
        self.calls = 0

    async def __call__(self, _: float) -> None:
        self.calls += 1
        await self._release.wait()
        self._release.clear()

    def release(self) -> None:
        """Allow the next interval to elapse."""
        self._release.set()


def _drive(hb: Heartbeat, sleeper: _PausedSleeper, releases: int) -> None:
    """Run the heartbeat, releasing its sleeper ``releases`` times, then stop."""

    async def _drive_async() -> None:
        task = asyncio.create_task(hb.run())
        for _ in range(releases):
            sleeper.release()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        hb.stop()
        sleeper.release()  # let the blocked loop observe the stop event
        await task

    asyncio.run(_drive_async())


def test_interval_s_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Heartbeat(0)
    with pytest.raises(ValueError):
        Heartbeat(-1)


def test_render_includes_uptime_and_summary() -> None:
    hb = Heartbeat(10.0, summary=lambda: "runtime_left=99s", clock=_FakeClock([0.0, 2.5]))
    rendered = hb._render()
    assert rendered.startswith("HEARTBEAT uptime=2.5s")
    assert "runtime_left=99s" in rendered


def test_run_emits_one_heartbeat_then_stops(capsys) -> None:
    hb = Heartbeat(1.0, clock=_FakeClock([0.0, 1.0, 2.0]))
    sleeper = _PausedSleeper()
    hb._sleeper = sleeper
    _drive(hb, sleeper, releases=1)
    out = capsys.readouterr().out
    assert out.count("HEARTBEAT") == 1


def test_run_emits_multiple_heartbeats(capsys) -> None:
    hb = Heartbeat(1.0, clock=_FakeClock([0.0, 1.0, 2.0, 3.0]))
    sleeper = _PausedSleeper()
    hb._sleeper = sleeper
    _drive(hb, sleeper, releases=3)
    out = capsys.readouterr().out
    assert out.count("HEARTBEAT") == 3


def test_stop_before_first_wake_emits_nothing(capsys) -> None:
    hb = Heartbeat(1.0)

    async def _drive_async() -> None:
        hb.stop()
        await hb.run()

    asyncio.run(_drive_async())
    assert capsys.readouterr().out == ""
