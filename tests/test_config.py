"""Unit tests for runtime configuration parsing (PR2)."""

import json
from pathlib import Path

import pytest

from ozzgraph.config import (
    ConfigError,
    Credential,
    OzzGraphConfig,
    credential_secret,
    load_config,
)


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


def test_load_config_specialists_disabled_by_default() -> None:
    """V07 (HAL-010): the specialist fleet is off by default, keeping
    the V06 model path byte-for-byte unchanged (ADR-0009 consequence)."""
    config = load_config(environ={"HAL_USER_ID": "user-42"})
    assert config.specialists_enabled is False


def test_load_config_specialists_enabled_via_env() -> None:
    """OZZGRAPH_SPECIALISTS_ENABLED (truthy spelling) turns the fleet on."""
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SPECIALISTS_ENABLED": "true"})
    assert config.specialists_enabled is True


def test_load_config_specialists_env_truthy_spellings() -> None:
    """Any accepted truthy spelling enables the fleet (hal_client convention)."""
    for value in ("1", "TRUE", "Yes", "on"):
        config = load_config(
            environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SPECIALISTS_ENABLED": value}
        )
        assert config.specialists_enabled is True


def test_load_config_specialists_env_blank_falls_back() -> None:
    """A blank toggle env var falls back to the default (disabled)."""
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SPECIALISTS_ENABLED": "  "})
    assert config.specialists_enabled is False


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
    # The allowlist is deterministic: sorted, whatever the env order.
    assert config.target_allowlist == ("10.0.0.0/8", "10.0.0.5", "challenge.local")
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


# ---------------------------------------------------------------------------
# V08 scope + credentials files (docs/adr/0010)
# ---------------------------------------------------------------------------


def _scope_file(tmp_path: Path, body: str, suffix: str = ".json") -> Path:
    path = tmp_path / f"scope{suffix}"
    path.write_text(body, encoding="utf-8")
    return path


def test_scope_file_json_list_merges_into_allowlist(tmp_path: Path) -> None:
    """A JSON scope file's entries merge into target_allowlist, sorted."""
    scope = _scope_file(
        tmp_path,
        json.dumps(["10.0.0.0/24", "http://10.0.0.5:3000", "10.0.0.5"]),
    )
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_SCOPE_FILE": str(scope),
            "OZZGRAPH_TARGET_ALLOWLIST": "intranet.example",
        }
    )
    assert config.scope_file == scope
    assert config.target_allowlist == (
        "10.0.0.0/24",
        "10.0.0.5",
        "http://10.0.0.5:3000",
        "intranet.example",
    )


def test_scope_file_toml_allowlist_table(tmp_path: Path) -> None:
    """A TOML scope file uses an ``allowlist`` table (TOML has no bare lists)."""
    scope = _scope_file(
        tmp_path,
        'allowlist = ["10.0.0.0/24", "challenge.local"]\n',
        suffix=".toml",
    )
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(scope)})
    assert config.target_allowlist == ("10.0.0.0/24", "challenge.local")


def test_scope_file_yaml_list(tmp_path: Path) -> None:
    """A YAML scope file loads through the safe loader."""
    scope = _scope_file(
        tmp_path,
        "- 10.0.0.0/24\n- http://10.0.0.5:3000\n",
        suffix=".yaml",
    )
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(scope)})
    assert config.target_allowlist == ("10.0.0.0/24", "http://10.0.0.5:3000")


def test_scope_file_deduplicates_and_sorts(tmp_path: Path) -> None:
    """Duplicate and unsorted entries collapse deterministically."""
    scope = _scope_file(tmp_path, json.dumps(["b.example", "a.example", "b.example"]))
    config = load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(scope)})
    assert config.target_allowlist == ("a.example", "b.example")


def test_scope_file_missing_fails_loudly(tmp_path: Path) -> None:
    """A configured-but-missing scope file is a loud ConfigError."""
    with pytest.raises(ConfigError, match="scope"):
        load_config(
            environ={
                "HAL_USER_ID": "user-42",
                "OZZGRAPH_SCOPE_FILE": str(tmp_path / "nope.json"),
            }
        )


def test_scope_file_malformed_json_fails_loudly(tmp_path: Path) -> None:
    """Malformed JSON content is a loud ConfigError, never a partial list."""
    scope = _scope_file(tmp_path, "{not json")
    with pytest.raises(ConfigError, match="malformed"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(scope)})


def test_scope_file_wrong_shape_fails_loudly(tmp_path: Path) -> None:
    """A scope file that is not a list of strings is rejected."""
    scope = _scope_file(tmp_path, json.dumps({"hosts": ["a.example"]}))
    with pytest.raises(ConfigError, match="allowlist"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(scope)})
    blank = _scope_file(tmp_path, json.dumps(["  "]))
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(blank)})


def test_scope_file_unsupported_format_fails_loudly(tmp_path: Path) -> None:
    """An unknown file suffix is rejected before parsing."""
    scope = _scope_file(tmp_path, "a.example", suffix=".txt")
    with pytest.raises(ConfigError, match="unsupported file format"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_SCOPE_FILE": str(scope)})


def _credentials_file(tmp_path: Path, body: str, suffix: str = ".json") -> Path:
    path = tmp_path / f"credentials{suffix}"
    path.write_text(body, encoding="utf-8")
    return path


def test_credentials_file_json_records_load_deterministically(tmp_path: Path) -> None:
    """Credential references load sorted by name, secrets never stored."""
    creds = _credentials_file(
        tmp_path,
        json.dumps(
            [
                {"name": "web-basic", "kind": "http_basic", "username": "admin"},
                {
                    "name": "api-token",
                    "kind": "api_token",
                    "username": "svc",
                    "secret_env": "OZZGRAPH_API_TOKEN",
                },
            ]
        ),
    )
    config = load_config(
        environ={"HAL_USER_ID": "user-42", "OZZGRAPH_CREDENTIALS_FILE": str(creds)}
    )
    assert [credential.name for credential in config.credentials] == [
        "api-token",
        "web-basic",
    ]
    token = config.credentials[0]
    assert token.kind == "api_token"
    assert token.secret_env == "OZZGRAPH_API_TOKEN"
    # The secret VALUE never appears in the model or the file content.
    assert "supersecret" not in creds.read_text(encoding="utf-8")


def test_credentials_file_toml_records(tmp_path: Path) -> None:
    """A TOML credentials file uses an array of tables."""
    creds = _credentials_file(
        tmp_path,
        '[[credentials]]\nname = "web-basic"\nkind = "http_basic"\nusername = "admin"\n',
        suffix=".toml",
    )
    config = load_config(
        environ={"HAL_USER_ID": "user-42", "OZZGRAPH_CREDENTIALS_FILE": str(creds)}
    )
    assert config.credentials[0].name == "web-basic"
    assert config.credentials[0].username == "admin"


def test_credentials_file_yaml_records(tmp_path: Path) -> None:
    """A YAML credentials file loads through the safe loader."""
    creds = _credentials_file(
        tmp_path,
        "credentials:\n  - name: ssh-key\n    kind: ssh_key\n    secret_env: OZZGRAPH_SSH_KEY\n",
        suffix=".yml",
    )
    config = load_config(
        environ={"HAL_USER_ID": "user-42", "OZZGRAPH_CREDENTIALS_FILE": str(creds)}
    )
    assert config.credentials[0].kind == "ssh_key"
    assert config.credentials[0].secret_env == "OZZGRAPH_SSH_KEY"


def test_credentials_file_missing_fails_loudly(tmp_path: Path) -> None:
    """A configured-but-missing credentials file is a loud ConfigError."""
    with pytest.raises(ConfigError, match="credentials"):
        load_config(
            environ={
                "HAL_USER_ID": "user-42",
                "OZZGRAPH_CREDENTIALS_FILE": str(tmp_path / "nope.json"),
            }
        )


def test_credentials_file_unknown_field_fails_loudly(tmp_path: Path) -> None:
    """A record with an unknown field is rejected (extra='forbid')."""
    creds = _credentials_file(
        tmp_path,
        json.dumps([{"name": "web", "kind": "http_basic", "username": "admin", "password": "x"}]),
    )
    with pytest.raises(ConfigError, match="invalid credential"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_CREDENTIALS_FILE": str(creds)})


def test_credentials_file_without_secret_source_fails_loudly(tmp_path: Path) -> None:
    """A record with neither username nor secret_env holds nothing."""
    creds = _credentials_file(tmp_path, json.dumps([{"name": "web", "kind": "http_basic"}]))
    with pytest.raises(ConfigError, match="username or a secret_env"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_CREDENTIALS_FILE": str(creds)})


def test_credentials_file_malformed_json_fails_loudly(tmp_path: Path) -> None:
    """Malformed JSON content is a loud ConfigError."""
    creds = _credentials_file(tmp_path, "[{broken")
    with pytest.raises(ConfigError, match="malformed"):
        load_config(environ={"HAL_USER_ID": "user-42", "OZZGRAPH_CREDENTIALS_FILE": str(creds)})


def test_credential_secret_resolves_from_environment() -> None:
    """The secret value is read from the named env var at runtime."""
    credential = Credential(name="api", kind="api_token", secret_env="OZZGRAPH_API_TOKEN")
    assert credential_secret(credential, environ={"OZZGRAPH_API_TOKEN": "s3cr3t"}) == "s3cr3t"
    with pytest.raises(ConfigError, match="OZZGRAPH_API_TOKEN"):
        credential_secret(credential, environ={})
    # A username-only credential has no secret to resolve.
    user_only = Credential(name="pub", kind="ci_token", username="ci")
    assert credential_secret(user_only, environ={}) == ""
