"""Bounded shell runner for OzzGraph (PR9).

Executes a shell command with hard bounds (AGENTS.md rule #4: every
action has a timeout, output limit, and fingerprint):

- a wall-clock timeout that kills the command's whole process group, so
  grandchildren cannot outlive the action;
- per-stream output limits that truncate while still draining, so the
  child can never deadlock on a full pipe buffer;
- explicit validation that rejects empty commands, negative limits, and
  missing working directories before anything spawns (fail loudly,
  AGENTS.md rule #9).

On POSIX platforms :meth:`ShellRunner.run` launches the command with
``start_new_session=True`` so the child owns its own process group; a
timeout sends SIGTERM to the group, waits a short grace period, then
SIGKILLs the group. On non-POSIX platforms process groups do not exist,
so the runner degrades to signalling only the direct child (documented
fallback); timeouts and truncation behave identically there.

The result is a :class:`ToolResult` — a pydantic v2 model carrying the
action ID, exit code, bounded stdout/stderr, wall-clock duration,
timeout flag, truncation flags, and artifact IDs (populated by the
executor in a later PR). A nonzero exit code is normal data; only
argument or spawn failures raise :class:`ShellRunnerError`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

#: Grace period between SIGTERM and SIGKILL when killing a timed-out
#: process group, in seconds.
_SIGKILL_GRACE_SECONDS = 0.25

#: Poll interval for the timeout loop, in seconds.
_POLL_INTERVAL_SECONDS = 0.05

#: Bytes read from a captured stream per iteration.
_READ_CHUNK_BYTES = 65536

#: SIGKILL is POSIX-only; non-POSIX platforms fall back to SIGTERM
#: (which maps to a hard terminate there anyway).
_KILL_SIGNAL: int = getattr(signal, "SIGKILL", signal.SIGTERM)


class TruncationState(BaseModel):
    """Which captured streams were cut at their limits.

    Attributes:
        stdout_truncated: True when stdout was cut by ``stdout_limit``.
        stderr_truncated: True when stderr was cut by ``stderr_limit``.
    """

    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ToolResult(BaseModel):
    """Outcome of one bounded shell run.

    Attributes:
        action_id: Fresh uuid4 hex minted for this run.
        command: The exact command string that was executed.
        exit_code: Process exit code; negative when the process was
            killed by a signal (e.g. -9 for SIGKILL), None if the
            process never exited.
        stdout: Captured standard output, truncated to the limit.
        stderr: Captured standard error, truncated to the limit.
        duration: Wall-clock seconds from spawn through cleanup,
            including the timeout kill path.
        timeout_state: True when the run was terminated by the timeout.
        truncation_state: Which streams were truncated.
        artifact_ids: Artifacts produced by this action; always empty in
            this PR (the executor populates it later).
    """

    action_id: str = Field(default_factory=lambda: uuid4().hex)
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    timeout_state: bool
    truncation_state: TruncationState
    artifact_ids: list[str] = Field(default_factory=list)


class ShellRunnerError(RuntimeError):
    """Structured failure for the shell runner (AGENTS.md rule #9).

    Raised for invalid arguments (empty command, negative limits,
    missing working directory) and when the command cannot be spawned.
    A nonzero exit code from the command itself is normal data carried
    in :attr:`ToolResult.exit_code` — never an error here.
    """


class ShellRunner:
    """Run shell commands with bounded output and a process-group timeout.

    On POSIX platforms the command is launched in its own process group
    (``start_new_session=True``); a timeout kills the whole group with
    SIGTERM, a short grace period, then SIGKILL, so grandchildren cannot
    outlive the action. On non-POSIX platforms process groups do not
    exist; the runner degrades to signalling only the direct child
    (documented fallback) while timeouts and truncation behave
    identically. The runner keeps no mutable state, so a single instance
    may be shared across concurrent calls.
    """

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        """Execute ``command`` through the shell with hard bounds.

        Args:
            command: The shell command line to execute.
            timeout_seconds: Wall-clock budget in seconds (>= 0); 0
                fires the timeout immediately after spawn.
            stdout_limit: Maximum characters of stdout to keep (>= 0);
                0 captures nothing. Excess output is still drained so
                the child never blocks on a full pipe buffer.
            stderr_limit: Same semantics as ``stdout_limit`` for stderr.
            working_directory: Directory the command runs in; must exist.

        Raises:
            ShellRunnerError: If ``command`` is empty or whitespace-only,
                ``timeout_seconds``/``stdout_limit``/``stderr_limit`` is
                negative, ``working_directory`` does not exist, or the
                command could not be spawned (the cause is chained).

        Returns:
            The bounded run result. ``exit_code`` may be nonzero — that
            is normal data. A timed-out run reports ``timeout_state``
            True and a ``duration`` that includes the full kill path.
        """
        self._validate(command, timeout_seconds, stdout_limit, stderr_limit, working_directory)

        start = time.monotonic()
        proc = await self._spawn(command, working_directory)
        stdout_task = asyncio.create_task(_drain(proc.stdout, stdout_limit))
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_limit))
        try:
            timeout_fired = await self._wait_or_timeout(proc, timeout_seconds)
            if timeout_fired:
                self._kill_process_group(proc, signal.SIGTERM)
                await asyncio.sleep(_SIGKILL_GRACE_SECONDS)
                self._kill_process_group(proc, _KILL_SIGNAL)
            exit_code = await proc.wait()
            stdout, stdout_truncated = await stdout_task
            stderr, stderr_truncated = await stderr_task
        finally:
            # Never leave a runaway process behind (e.g. if this task is
            # cancelled mid-run); the group kill is a no-op once the
            # process has been reaped.
            if proc.returncode is None:
                self._kill_process_group(proc, _KILL_SIGNAL)
        duration = time.monotonic() - start
        return ToolResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            timeout_state=timeout_fired,
            truncation_state=TruncationState(
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            ),
        )

    @staticmethod
    def _validate(
        command: str,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> None:
        """Reject invalid arguments loudly before anything spawns.

        Raises:
            ShellRunnerError: For an empty/whitespace command, negative
                limits, or a missing working directory.
        """
        if not command.strip():
            raise ShellRunnerError("command must not be empty or whitespace-only")
        if timeout_seconds < 0:
            raise ShellRunnerError(f"timeout_seconds must be >= 0, got {timeout_seconds}")
        if stdout_limit < 0:
            raise ShellRunnerError(f"stdout_limit must be >= 0, got {stdout_limit}")
        if stderr_limit < 0:
            raise ShellRunnerError(f"stderr_limit must be >= 0, got {stderr_limit}")
        if not working_directory.is_dir():
            raise ShellRunnerError(
                f"working_directory is not an existing directory: {working_directory}"
            )

    @staticmethod
    async def _spawn(command: str, working_directory: Path) -> asyncio.subprocess.Process:
        """Launch ``command`` via the shell in its own process group.

        Raises:
            ShellRunnerError: If the shell could not be started (the
                original exception is chained).
        """
        try:
            if os.name == "posix":
                return await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(working_directory),
                    start_new_session=True,
                )
            return await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(working_directory),
            )
        except OSError as exc:
            raise ShellRunnerError(f"failed to spawn command: {exc}") from exc

    @staticmethod
    async def _wait_or_timeout(proc: asyncio.subprocess.Process, timeout_seconds: float) -> bool:
        """Wait for ``proc`` up to ``timeout_seconds``; True when it timed out.

        Polls ``returncode`` instead of awaiting ``proc.wait()`` so the
        wait can be abandoned on timeout without cancelling asyncio's
        internal wait future (which would leak the reap).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while proc.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        return False

    @staticmethod
    def _kill_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Deliver ``sig`` to the whole process group (POSIX).

        On non-POSIX platforms this degrades to signalling only the
        direct child: ``terminate`` for SIGTERM, ``kill`` otherwise.
        ``ProcessLookupError`` (the group is already gone) is expected
        and swallowed; anything else propagates.
        """
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                pass
            return
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> tuple[str, bool]:
    """Read ``stream`` to EOF, keeping at most ``limit`` characters.

    Bytes beyond the limit are still read and discarded so the child
    never blocks on a full pipe buffer (deadlock avoidance); a limit of
    0 therefore still drains. Returns the captured text and whether it
    was truncated.
    """
    if stream is None:
        return "", False
    kept: list[str] = []
    kept_len = 0
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        space = limit - kept_len
        if space > 0:
            kept.append(chunk[:space].decode("utf-8", errors="replace"))
            kept_len += min(len(chunk), space)
        if len(chunk) > space:
            truncated = True
    return "".join(kept), truncated
