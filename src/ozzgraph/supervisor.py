"""Supervisor kernel (PR2/PR3).

The supervisor owns startup, identity output, runtime-directory
initialization, heartbeat emission, budget enforcement, signal handling, and
clean termination with a structured reason. It must not contain
challenge-category logic (AGENTS.md architecture rule 10).

PR3 turns :meth:`Supervisor.run` into an asyncio loop that emits heartbeats,
enforces budgets, and terminates gracefully on ``SIGTERM``/``SIGINT``. PR4 adds
structured event logging.
"""

from __future__ import annotations

import asyncio
import signal
from enum import Enum

from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.heartbeat import Heartbeat

_POLL_SECONDS = 0.25


class TerminationReason(str, Enum):
    """Structured reason for a supervisor termination."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class Supervisor:
    """Owns startup, heartbeat, budgets, signal handling, and clean shutdown.

    Args:
        config: Validated runtime configuration.
    """

    def __init__(self, config: OzzGraphConfig) -> None:
        self._config = config
        self._started = False
        self._budgets: Budgets | None = None

    @property
    def config(self) -> OzzGraphConfig:
        """The validated configuration this supervisor runs with."""
        return self._config

    def budgets(self) -> Budgets:
        """The active budget tracker, after :meth:`run` has started."""
        if self._budgets is None:
            raise RuntimeError("budgets not initialized; run() not started")
        return self._budgets

    def start(self) -> None:
        """Print identity immediately, then initialize runtime directories.

        The identity line must be the first output of the process so the
        competition platform can attribute the run (TECHNICAL_REQUIREMENTS).
        Directory creation is idempotent.
        """
        print(f"USER ID: {self._config.hal_user_id}", flush=True)
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._config.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._started = True

    async def run(self) -> TerminationReason:
        """Run the supervisor until a terminal condition.

        Installs ``SIGTERM``/``SIGINT`` handlers, starts the heartbeat, then
        loops until either a budget is exhausted (returning
        ``BUDGET_EXHAUSTED``) or a signal requests a graceful stop (returning
        ``INTERRUPTED``).

        Signal handlers are installed before :meth:`start` so a signal that
        arrives immediately after the identity line is still caught gracefully
        rather than killing the process with the default disposition.

        Returns:
            The structured reason for termination.
        """
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_signal() -> None:
            stop_event.set()

        installed_signals: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal)
                installed_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                # Signal handling requires a running main-thread loop.
                pass

        self.start()
        cfg = self._config
        budgets = Budgets(
            max_tokens=cfg.max_tokens,
            max_model_calls=cfg.max_model_calls,
            max_tool_calls=cfg.max_tool_calls,
            max_workers=cfg.max_workers,
            max_hints=cfg.max_hints,
            max_runtime_s=float(cfg.max_runtime_s),
        )
        self._budgets = budgets
        heartbeat = Heartbeat(
            float(cfg.heartbeat_interval_s),
            summary=lambda: f"runtime_left={budgets.remaining_runtime():.0f}s",
        )

        heartbeat_task = asyncio.create_task(heartbeat.run())
        try:
            while not stop_event.is_set():
                if budgets.is_exhausted():
                    return self.stop(reason=TerminationReason.BUDGET_EXHAUSTED)
                await asyncio.sleep(_POLL_SECONDS)
            return self.stop(reason=TerminationReason.INTERRUPTED)
        finally:
            heartbeat.stop()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            for sig in installed_signals:
                loop.remove_signal_handler(sig)

    def stop(self, reason: TerminationReason = TerminationReason.INTERRUPTED) -> TerminationReason:
        """Terminate cleanly with a structured reason.

        Args:
            reason: Why the run ended.

        Returns:
            The reason passed in, so callers can chain ``run()`` -> reason.
        """
        if not self._started:
            return reason
        self._started = False
        return reason
