"""Signal-handling tests for the supervisor (PR3).

Spawns the real ``python -m ozzgraph`` entry point as a subprocess and sends
SIGTERM/SIGINT to verify graceful termination with the expected exit code and
identity output.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REASON_EXIT_CODES = {"interrupted": 130}


def _spawn(tmp_path: Path, max_runtime_s: int = 600) -> subprocess.Popen:
    env = {
        "PATH": "/usr/bin:/bin",
        "HAL_USER_ID": "user-42",
        "OZZGRAPH_STATE_DIR": str(tmp_path / "state"),
        "OZZGRAPH_MAX_RUNTIME_S": str(max_runtime_s),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "ozzgraph"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _read_until(proc: subprocess.Popen, needle: str, timeout_s: float = 10.0) -> str:
    """Read from proc.stdout line by line until ``needle`` appears."""
    deadline = time.monotonic() + timeout_s
    buffer: list[str] = []
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line == "":
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        buffer.append(line)
        if needle in line:
            return "".join(buffer)
    raise AssertionError(f"timed out waiting for {needle!r}; got: {''.join(buffer)!r}")


@pytest.mark.parametrize(
    ("signum", "expected"),
    [
        (signal.SIGTERM, _REASON_EXIT_CODES["interrupted"]),
        (signal.SIGINT, _REASON_EXIT_CODES["interrupted"]),
    ],
)
def test_signal_during_run_terminates_gracefully(tmp_path, signum, expected) -> None:
    """Sending SIGTERM/SIGINT during run() stops the process with exit 130."""
    proc = _spawn(tmp_path)
    try:
        out = _read_until(proc, "USER ID:")
        assert out.startswith("USER ID: user-42")
        os.kill(proc.pid, signum)
        code = proc.wait(timeout=10)
        assert code == expected
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_user_id_is_first_output_before_any_heartbeat(tmp_path) -> None:
    """USER ID must appear before any HEARTBEAT line."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HAL_USER_ID": "user-42",
        "OZZGRAPH_STATE_DIR": str(tmp_path / "state"),
        "OZZGRAPH_MAX_RUNTIME_S": "3",
        "OZZGRAPH_HEARTBEAT_INTERVAL_S": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozzgraph"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        out = _read_until(proc, "HEARTBEAT", timeout_s=10)
        assert out.index("USER ID: user-42") < out.index("HEARTBEAT")
        code = proc.wait(timeout=10)
        assert code == 3  # budget_exhausted after the short runtime budget
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
