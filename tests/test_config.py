"""Unit tests for runtime configuration parsing (PR2)."""

from pathlib import Path

import pytest

from ozzgraph.config import ConfigError, OzzGraphConfig, load_config


def test_load_config_requires_hal_user_id() -> None:
    """HAL_USER_ID is a required environment variable."""
    with pytest.raises(ConfigError, match="HAL_USER_ID"):
        load_config(environ={})


def test_load_config_rejects_blank_hal_user_id() -> None:
    """A whitespace-only HAL_USER_ID is invalid."""
    with pytest.raises(ConfigError, match="HAL_USER_ID"):
        load_config(environ={"HAL_USER_ID": "   "})


def test_load_config_applies_default_runtime_dirs() -> None:
    """State and artifact dirs default to state/ and state/artifacts/."""
    config = load_config(environ={"HAL_USER_ID": "user-42"})
    assert config.hal_user_id == "user-42"
    assert str(config.state_dir) == "state"
    assert str(config.artifact_dir) == "state/artifacts"


def test_load_config_artifact_default_follows_state_dir() -> None:
    """When only the state dir is overridden, artifacts nest under it."""
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_STATE_DIR": "/tmp/ozz-state"})
    assert str(config.state_dir) == "/tmp/ozz-state"
    assert str(config.artifact_dir) == "/tmp/ozz-state/artifacts"


def test_load_config_respects_explicit_overrides() -> None:
    """Explicit env vars override every default."""
    config = load_config(
        environ={
            "HAL_USER_ID": "user-7",
            "OZZGRAPH_STATE_DIR": "/var/lib/ozzgraph",
            "OZZGRAPH_ARTIFACT_DIR": "/var/lib/ozzgraph/evidence",
        }
    )
    assert config.hal_user_id == "user-7"
    assert str(config.state_dir) == "/var/lib/ozzgraph"
    assert str(config.artifact_dir) == "/var/lib/ozzgraph/evidence"


def test_config_model_is_pydantic_v2() -> None:
    """The config model is a pydantic v2 BaseModel with validated fields."""
    OzzGraphConfig(hal_user_id="u", state_dir=Path("s"), artifact_dir=Path("a"))
    assert OzzGraphConfig.model_fields["hal_user_id"].is_required()
