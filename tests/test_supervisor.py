"""Tests for the supervisor kernel (PR2/PR3): identity, dirs, lifecycle,
heartbeat, budget exhaustion, and signal handling."""

import asyncio

import pytest

from ozzgraph.config import OzzGraphConfig
from ozzgraph.supervisor import Supervisor, TerminationReason


def _config(tmp_path, user_id: str = "user-42", **overrides) -> OzzGraphConfig:
    base = {
        "hal_user_id": user_id,
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
    }
    base.update(overrides)
    return OzzGraphConfig(**base)


def test_start_prints_identity_immediately(tmp_path, capsys) -> None:
    """start() prints the USER ID line before anything else."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    out = capsys.readouterr().out
    assert out.startswith("USER ID: user-42")


def test_start_initializes_runtime_directories(tmp_path) -> None:
    """start() creates state and artifact directories."""
    state = tmp_path / "state"
    artifacts = tmp_path / "state" / "artifacts"
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    assert state.is_dir()
    assert artifacts.is_dir()


def test_start_is_idempotent(tmp_path) -> None:
    """Calling start() twice must not raise."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    supervisor.start()


def test_run_exhausts_runtime_budget_without_model_dependency(tmp_path, capsys) -> None:
    """Without a model dependency, the loop runs until the time budget
    exhausts, emitting at least one heartbeat, and reports BUDGET_EXHAUSTED."""
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=2, heartbeat_interval_s=1))
    reason = asyncio.run(supervisor.run())
    assert reason == TerminationReason.BUDGET_EXHAUSTED
    out = capsys.readouterr().out
    assert out.startswith("USER ID: user-42")
    assert "HEARTBEAT" in out


def test_stop_accepts_structured_reason(tmp_path) -> None:
    """stop() accepts a structured termination reason without raising."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    supervisor.stop(reason=TerminationReason.INTERRUPTED)


def test_budgets_accessor_raises_before_run(tmp_path) -> None:
    """budgets() is unavailable until run() starts."""
    supervisor = Supervisor(_config(tmp_path))
    with pytest.raises(RuntimeError):
        supervisor.budgets()


def test_run_exposes_budgets(tmp_path) -> None:
    """run() installs a budget tracker queryable via budgets()."""
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=1))
    asyncio.run(supervisor.run())
    assert supervisor.budgets().is_runtime_exhausted()


def test_run_raises_config_error_when_dirs_unwritable(tmp_path) -> None:
    """A failure to initialize runtime directories fails loudly."""
    # Point artifact_dir inside a regular file so mkdir raises OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad = OzzGraphConfig(
        hal_user_id="user-42",
        state_dir=tmp_path / "state",
        artifact_dir=blocker / "artifacts",
    )
    with pytest.raises(OSError):
        Supervisor(bad).start()
