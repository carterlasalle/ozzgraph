"""Entry-point tests for ``python -m ozzgraph`` (PR2)."""

import subprocess
import sys
from pathlib import Path

import pytest

from ozzgraph.__main__ import main


def test_main_prints_identity_and_exits_zero(tmp_path, capsys, monkeypatch) -> None:
    """A configured run prints the USER ID and terminates cleanly (exit 0)."""
    monkeypatch.setenv("HAL_USER_ID", "user-42")
    monkeypatch.setenv("OZZGRAPH_STATE_DIR", str(tmp_path / "state"))
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("USER ID: user-42")


def test_main_missing_required_env_fails_loudly(capsys, monkeypatch) -> None:
    """Missing HAL_USER_ID produces a structured failure (exit 1)."""
    monkeypatch.delenv("HAL_USER_ID", raising=False)
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "configuration error" in err
    assert "HAL_USER_ID" in err


def test_main_version_flag(capsys) -> None:
    """--version prints the package version and exits 0."""
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ozzgraph")


def test_module_invocation_requires_env(tmp_path: Path) -> None:
    """Running the module without HAL_USER_ID fails loudly via subprocess."""
    env = {
        "PATH": "/usr/bin:/bin",
        "OZZGRAPH_STATE_DIR": str(tmp_path / "state"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "ozzgraph"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "HAL_USER_ID" in result.stderr


@pytest.mark.parametrize("extra", [(), ("--version",)])
def test_module_invocation_with_env(tmp_path: Path, extra: tuple[str, ...]) -> None:
    """With HAL_USER_ID set, the module prints the identity line and exits 0."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HAL_USER_ID": "user-42",
        "OZZGRAPH_STATE_DIR": str(tmp_path / "state"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "ozzgraph", *extra],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    if not extra:
        assert result.stdout.startswith("USER ID: user-42")
