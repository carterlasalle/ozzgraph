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


def test_load_config_applies_budget_defaults() -> None:
    """Budget and heartbeat knobs default without explicit env vars."""
    config = load_config(environ={"HAL_USER_ID": "user-42"})
    assert config.heartbeat_interval_s == 30
    assert config.max_runtime_s == 7200
    assert config.max_tokens == 0
    assert config.max_model_calls == 0
    assert config.max_tool_calls == 0
    assert config.max_workers == 4
    assert config.max_hints == 1


def test_load_config_respects_budget_overrides() -> None:
    """Explicit budget env vars override the defaults."""
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_HEARTBEAT_INTERVAL_S": "5",
            "OZZGRAPH_MAX_RUNTIME_S": "60",
            "OZZGRAPH_MAX_TOKENS": "100000",
            "OZZGRAPH_MAX_MODEL_CALLS": "50",
            "OZZGRAPH_MAX_TOOL_CALLS": "200",
            "OZZGRAPH_MAX_WORKERS": "8",
            "OZZGRAPH_MAX_HINTS": "3",
        }
    )
    assert config.heartbeat_interval_s == 5
    assert config.max_runtime_s == 60
    assert config.max_tokens == 100000
    assert config.max_model_calls == 50
    assert config.max_tool_calls == 200
    assert config.max_workers == 8
    assert config.max_hints == 3


def test_load_config_rejects_non_integer_budget() -> None:
    """A non-integer budget env var fails loudly."""
    with pytest.raises(ConfigError, match="OZZGRAPH_MAX_RUNTIME_S"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_MAX_RUNTIME_S": "soon"})


def test_load_config_ignores_blank_budget_env() -> None:
    """A blank budget env var falls back to the default."""
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_MAX_WORKERS": "  "})
    assert config.max_workers == 4


def test_config_model_validates_budget_ranges() -> None:
    """Budget fields enforce their gt/ge constraints via pydantic."""
    with pytest.raises(ValueError):
        OzzGraphConfig(hal_user_id="u", max_runtime_s=0)
    with pytest.raises(ValueError):
        OzzGraphConfig(hal_user_id="u", max_tokens=-1)
    with pytest.raises(ValueError):
        OzzGraphConfig(hal_user_id="u", max_workers=0)
    with pytest.raises(ValueError):
        OzzGraphConfig(hal_user_id="u", max_hints=0)


def test_load_config_applies_scope_defaults() -> None:
    """Scope-policy knobs default to fail-closed-but-permissive values."""
    config = load_config(environ={"HAL_USER_ID": "user-42"})
    assert config.max_command_length == 4096
    assert config.target_allowlist == ()
    assert config.allowed_command_families == ("shell", "recon", "exploit")


def test_load_config_respects_scope_overrides() -> None:
    """Explicit scope env vars override the defaults."""
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_MAX_COMMAND_LENGTH": "8192",
            "OZZGRAPH_TARGET_ALLOWLIST": "10.0.0.5, challenge.local, 10.0.0.0/8",
            "OZZGRAPH_ALLOWED_COMMAND_FAMILIES": "recon,shell",
        }
    )
    assert config.max_command_length == 8192
    assert config.target_allowlist == ("10.0.0.5", "challenge.local", "10.0.0.0/8")
    assert config.allowed_command_families == ("recon", "shell")


def test_load_config_rejects_non_integer_command_length() -> None:
    """A non-integer command-length env var fails loudly."""
    with pytest.raises(ConfigError, match="OZZGRAPH_MAX_COMMAND_LENGTH"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_MAX_COMMAND_LENGTH": "long"})


def test_load_config_blank_scope_vars_use_defaults() -> None:
    """Blank scope env vars fall back to the defaults."""
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_MAX_COMMAND_LENGTH": "   ",
            "OZZGRAPH_TARGET_ALLOWLIST": "   ",
            "OZZGRAPH_ALLOWED_COMMAND_FAMILIES": "",
        }
    )
    assert config.max_command_length == 4096
    assert config.target_allowlist == ()
    assert config.allowed_command_families == ("shell", "recon", "exploit")


def test_config_model_validates_command_length_range() -> None:
    """max_command_length enforces its gt constraint via pydantic."""
    with pytest.raises(ValueError):
        OzzGraphConfig(hal_user_id="u", max_command_length=0)


def test_load_config_applies_flag_and_submission_defaults() -> None:
    """Flag pattern and submission cap default safely without env vars."""
    config = load_config(environ={"HAL_USER_ID": "user-42"})
    assert config.flag_pattern == r"flag\{[^{}\s]+\}"
    assert config.max_submissions == 3


def test_load_config_respects_flag_and_submission_overrides() -> None:
    """Explicit flag/submission env vars override the defaults."""
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_FLAG_PATTERN": r"CTF\{[^{}\s]+\}",
            "OZZGRAPH_MAX_SUBMISSIONS": "5",
        }
    )
    assert config.flag_pattern == r"CTF\{[^{}\s]+\}"
    assert config.max_submissions == 5


def test_load_config_rejects_invalid_flag_pattern() -> None:
    """An uncompileable flag pattern fails loudly at load time."""
    with pytest.raises(ConfigError, match="flag_pattern"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_FLAG_PATTERN": "flag([bad"})


def test_load_config_rejects_invalid_max_submissions() -> None:
    """A non-integer submission cap fails loudly."""
    with pytest.raises(ConfigError, match="OZZGRAPH_MAX_SUBMISSIONS"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_MAX_SUBMISSIONS": "many"})


def test_config_model_validates_max_submissions_range() -> None:
    """max_submissions enforces its ge constraint via pydantic."""
    with pytest.raises(ValueError):
        OzzGraphConfig(hal_user_id="u", max_submissions=0)


def test_load_config_blank_flag_env_uses_default() -> None:
    """A blank flag-pattern env var falls back to the safe default."""
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_FLAG_PATTERN": "   "})
    assert config.flag_pattern == r"flag\{[^{}\s]+\}"
