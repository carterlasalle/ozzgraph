"""Supervisor kernel skeleton (PR2).

The supervisor owns startup, identity output, runtime-directory
initialization, and clean termination with a structured reason. It must not
contain challenge-category logic (AGENTS.md architecture rule 10).

PR3 adds heartbeat, budgets, and signal handling. PR4 adds structured event
logging. Until then the kernel prints identity, prepares runtime directories,
and terminates cleanly.
"""

from __future__ import annotations

from enum import Enum

from ozzgraph.config import OzzGraphConfig


class TerminationReason(str, Enum):
    """Structured reason for a supervisor termination."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class Supervisor:
    """Owns startup, runtime directories, and clean shutdown.

    Args:
        config: Validated runtime configuration.
    """

    def __init__(self, config: OzzGraphConfig) -> None:
        self._config = config
        self._started = False

    @property
    def config(self) -> OzzGraphConfig:
        """The validated configuration this supervisor runs with."""
        return self._config

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

    def run(self) -> TerminationReason:
        """Run the supervisor until a terminal condition.

        PR2 skeleton: start, then terminate cleanly. PR3+ replaces this with
        the heartbeat/budget/lifecycle loop; no model dependency exists yet.

        Returns:
            The structured reason for termination.
        """
        self.start()
        return self.stop(reason=TerminationReason.COMPLETED)

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
