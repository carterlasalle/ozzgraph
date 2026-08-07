"""Supervisor kernel (PR2/PR3).

The supervisor owns startup, identity output, runtime-directory
initialization, heartbeat emission, budget enforcement, signal handling, and
clean termination with a structured reason. It must not contain
challenge-category logic (AGENTS.md architecture rule 10).

PR3 turns :meth:`Supervisor.run` into an asyncio loop that emits heartbeats,
enforces budgets, and terminates gracefully on ``SIGTERM``/``SIGINT``. PR4 adds
append-only structured event logging (bootstrap and termination events). PR12
runs the deterministic bootstrap reconnaissance
(:mod:`ozzgraph.bootstrap`) after heartbeat setup and before the main idle
loop, constructing the supervisor-owned privileged HalClient for it.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.bootstrap import BootstrapRunner
from ozzgraph.budgets import Budgets
from ozzgraph.config import ConfigError, OzzGraphConfig
from ozzgraph.events import BOOTSTRAP, TERMINATION, Event, EventLog
from ozzgraph.hal_client import HalClient
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
        self._run_id = uuid4().hex
        self._started = False
        self._budgets: Budgets | None = None
        self._event_log: EventLog | None = None
        self._artifact_store: ArtifactStore | None = None

    @property
    def config(self) -> OzzGraphConfig:
        """The validated configuration this supervisor runs with."""
        return self._config

    @property
    def run_id(self) -> str:
        """Unique identifier for this run, minted once at construction."""
        return self._run_id

    def budgets(self) -> Budgets:
        """The active budget tracker, after :meth:`run` has started."""
        if self._budgets is None:
            raise RuntimeError("budgets not initialized; run() not started")
        return self._budgets

    @property
    def artifact_store(self) -> ArtifactStore:
        """The run's artifact store, after :meth:`start` has run."""
        if self._artifact_store is None:
            raise RuntimeError("artifact store not initialized; start() not called")
        return self._artifact_store

    def start(self) -> None:
        """Print identity immediately, then initialize runtime directories.

        The identity line must be the first output of the process so the
        competition platform can attribute the run (TECHNICAL_REQUIREMENTS).
        Directory creation is idempotent. Once the directories exist, a
        ``bootstrap`` event is appended to ``state_dir/actions.jsonl``
        and the run's artifact store is created at ``state_dir/artifacts``.
        """
        print(f"USER ID: {self._config.hal_user_id}", flush=True)
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._config.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._event_log = EventLog.for_run(self._config.state_dir)
        self._artifact_store = ArtifactStore.for_run(self._config.state_dir)
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=BOOTSTRAP,
                producer="supervisor",
                payload={
                    "hal_user_id": self._config.hal_user_id,
                    "state_dir": str(self._config.state_dir),
                    "artifact_dir": str(self._config.artifact_dir),
                    "budget": {
                        "max_tokens": self._config.max_tokens,
                        "max_model_calls": self._config.max_model_calls,
                        "max_tool_calls": self._config.max_tool_calls,
                        "max_workers": self._config.max_workers,
                        "max_hints": self._config.max_hints,
                        "max_runtime_s": self._config.max_runtime_s,
                    },
                },
            )
        )
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
            bootstrap_reason = await self._run_bootstrap()
            if bootstrap_reason is not None:
                return bootstrap_reason
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

    async def _run_bootstrap(self) -> TerminationReason | None:
        """Run deterministic bootstrap reconnaissance before the main loop.

        The privileged HalClient is constructed here — the supervisor is
        the only component that may own one (AGENTS.md invariant 5) — and
        handed to the bootstrap runner for status retrieval, smoke-flag
        submission, and the free hint. A bootstrap configuration error
        (malformed target variables, unknown namespace, smoke flag
        without a challenge id) terminates the run with ``FAILED`` so the
        failure is structured and loud; Hal service failures are recorded
        as events by the runner and are not fatal.

        Returns:
            The termination reason when bootstrap aborted the run, or
            None when it completed and the main loop may start.
        """
        assert self._event_log is not None  # start() sets it before _started
        client = HalClient(privileged=True, event_log=self._event_log, run_id=self._run_id)
        try:
            runner = BootstrapRunner(
                config=self._config,
                run_id=self._run_id,
                event_log=self._event_log,
                client=client,
            )
            try:
                await runner.run()
            except ConfigError:
                return self.stop(reason=TerminationReason.FAILED)
        finally:
            await client.aclose()
        return None

    def stop(self, reason: TerminationReason = TerminationReason.INTERRUPTED) -> TerminationReason:
        """Terminate cleanly with a structured reason.

        Once started, appends a ``termination`` event carrying the reason to
        the run log before clearing the started flag, so both ``run()``
        terminal paths (budget exhausted, interrupted) end with a structured
        termination record. Stopping before :meth:`start` writes no event.

        Args:
            reason: Why the run ended.

        Returns:
            The reason passed in, so callers can chain ``run()`` -> reason.
        """
        if not self._started:
            return reason
        assert self._event_log is not None  # start() sets it before _started
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=TERMINATION,
                producer="supervisor",
                payload={"reason": reason.value},
            )
        )
        self._started = False
        return reason
