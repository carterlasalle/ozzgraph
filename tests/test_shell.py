"""Tests for the bounded shell runner (PR9).

Covers representative success output, nonzero exit codes as normal
data, stderr capture, working-directory honoring, process-group
timeout (grandchildren must not survive), stdout/stderr truncation
without deadlock, zero and exact-boundary limits, and loud argument
errors (AGENTS.md tool-change testing expectations).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ozzgraph.shell import ShellRunner, ShellRunnerError


@pytest.mark.asyncio
async def test_success_echo(tmp_path: Path) -> None:
    """echo hello exits 0 with hello on stdout and no limits hit."""
    result = await ShellRunner().run(
        command="echo hello",
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.duration >= 0
    assert not result.timeout_state
    assert not result.truncation_state.stdout_truncated
    assert not result.truncation_state.stderr_truncated
    assert result.artifact_ids == []
    assert len(result.action_id) == 32
    assert result.command == "echo hello"


@pytest.mark.asyncio
async def test_failure_exit_code_is_data(tmp_path: Path) -> None:
    """A nonzero exit code is normal data, not an error."""
    result = await ShellRunner().run(
        command="sh -c 'exit 3'",
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 3
    assert not result.timeout_state


@pytest.mark.asyncio
async def test_stderr_capture(tmp_path: Path) -> None:
    """Output written to stderr is captured in stderr."""
    result = await ShellRunner().run(
        command="sh -c 'echo err >&2'",
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert result.stderr.strip() == "err"
    assert result.stdout == ""


@pytest.mark.asyncio
async def test_working_directory_honored(tmp_path: Path) -> None:
    """The command runs inside working_directory (pwd matches it)."""
    result = await ShellRunner().run(
        command="pwd",
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert Path(result.stdout.strip()) == tmp_path.resolve()


@pytest.mark.asyncio
async def test_timeout_kills_and_reports(tmp_path: Path) -> None:
    """A long-running command is killed at the timeout, with the kill
    path included in duration."""
    result = await ShellRunner().run(
        command="sleep 5",
        timeout_seconds=1,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.timeout_state
    assert result.duration < 5
    assert result.duration >= 1.0


@pytest.mark.asyncio
async def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    """Grandchildren die with the process group: a delayed marker file
    scheduled in a background child must never appear."""
    marker = tmp_path / "marker"
    result = await ShellRunner().run(
        command=f"sh -c '(sleep 0.6; touch {marker}) & sleep 30'",
        timeout_seconds=0.2,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.timeout_state
    await asyncio.sleep(1.2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_stdout_truncation(tmp_path: Path) -> None:
    """stdout is capped at the limit and the child still completes."""
    result = await ShellRunner().run(
        command="seq 1 100000",
        timeout_seconds=30,
        stdout_limit=100,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert len(result.stdout) == 100
    assert result.truncation_state.stdout_truncated
    assert not result.truncation_state.stderr_truncated


@pytest.mark.asyncio
async def test_simultaneous_overflow_no_deadlock(tmp_path: Path) -> None:
    """Flooding both streams with tiny limits completes and sets both
    truncation flags (concurrent draining, no pipe-buffer deadlock)."""
    command = "sh -c 'i=0; while [ $i -lt 20000 ]; do echo out; echo err >&2; i=$((i + 1)); done'"
    result = await ShellRunner().run(
        command=command,
        timeout_seconds=30,
        stdout_limit=10,
        stderr_limit=10,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert len(result.stdout) == 10
    assert len(result.stderr) == 10
    assert result.truncation_state.stdout_truncated
    assert result.truncation_state.stderr_truncated


@pytest.mark.asyncio
async def test_zero_limit_captures_nothing_but_drains(tmp_path: Path) -> None:
    """A limit of 0 captures nothing, still drains (no deadlock), and
    marks the stream truncated when it produced output."""
    result = await ShellRunner().run(
        command="seq 1 100000",
        timeout_seconds=30,
        stdout_limit=0,
        stderr_limit=0,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.truncation_state.stdout_truncated


@pytest.mark.asyncio
async def test_output_exactly_at_limit_is_not_truncated(tmp_path: Path) -> None:
    """Output exactly at the limit is fully captured, not truncated."""
    result = await ShellRunner().run(
        command="printf 'abcdef'",
        timeout_seconds=10,
        stdout_limit=6,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert result.stdout == "abcdef"
    assert not result.truncation_state.stdout_truncated


@pytest.mark.asyncio
async def test_empty_command_raises(tmp_path: Path) -> None:
    """Empty and whitespace-only commands are rejected loudly."""
    runner = ShellRunner()
    with pytest.raises(ShellRunnerError):
        await runner.run(
            command="",
            timeout_seconds=10,
            stdout_limit=1024,
            stderr_limit=1024,
            working_directory=tmp_path,
        )
    with pytest.raises(ShellRunnerError):
        await runner.run(
            command="   ",
            timeout_seconds=10,
            stdout_limit=1024,
            stderr_limit=1024,
            working_directory=tmp_path,
        )


@pytest.mark.asyncio
async def test_missing_working_directory_raises(tmp_path: Path) -> None:
    """A nonexistent working directory is rejected loudly."""
    with pytest.raises(ShellRunnerError):
        await ShellRunner().run(
            command="echo hi",
            timeout_seconds=10,
            stdout_limit=1024,
            stderr_limit=1024,
            working_directory=tmp_path / "does-not-exist",
        )


@pytest.mark.asyncio
async def test_negative_limits_raise(tmp_path: Path) -> None:
    """Negative stdout/stderr limits and timeout are rejected loudly."""
    runner = ShellRunner()
    with pytest.raises(ShellRunnerError):
        await runner.run(
            command="echo hi",
            timeout_seconds=10,
            stdout_limit=-1,
            stderr_limit=1024,
            working_directory=tmp_path,
        )
    with pytest.raises(ShellRunnerError):
        await runner.run(
            command="echo hi",
            timeout_seconds=10,
            stdout_limit=1024,
            stderr_limit=-1,
            working_directory=tmp_path,
        )
    with pytest.raises(ShellRunnerError):
        await runner.run(
            command="echo hi",
            timeout_seconds=-1,
            stdout_limit=1024,
            stderr_limit=1024,
            working_directory=tmp_path,
        )
