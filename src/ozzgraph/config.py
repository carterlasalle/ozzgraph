"""Runtime configuration for OzzGraph (PR2/PR3).

Configuration is parsed from environment variables into a validated Pydantic
v2 model. No secrets or model-specific settings live here — this module owns
identity, runtime-directory layout, the heartbeat/budget knobs, and the scope
policy knobs (command-length limit, target allowlist, permitted command
families). Structured logging level arrives with PR4.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from ozzgraph.policy import (
    DEFAULT_ALLOWED_COMMAND_FAMILIES,
    DEFAULT_MAX_COMMAND_LENGTH,
    DEFAULT_TARGET_ALLOWLIST,
)

HAL_USER_ID_ENV = "HAL_USER_ID"
STATE_DIR_ENV = "OZZGRAPH_STATE_DIR"
ARTIFACT_DIR_ENV = "OZZGRAPH_ARTIFACT_DIR"

HEARTBEAT_INTERVAL_ENV = "OZZGRAPH_HEARTBEAT_INTERVAL_S"
MAX_RUNTIME_ENV = "OZZGRAPH_MAX_RUNTIME_S"
MAX_TOKENS_ENV = "OZZGRAPH_MAX_TOKENS"
MAX_MODEL_CALLS_ENV = "OZZGRAPH_MAX_MODEL_CALLS"
MAX_TOOL_CALLS_ENV = "OZZGRAPH_MAX_TOOL_CALLS"
MAX_WORKERS_ENV = "OZZGRAPH_MAX_WORKERS"
MAX_HINTS_ENV = "OZZGRAPH_MAX_HINTS"

# Scope-policy knobs (PR10): command-length limit, target allowlist,
# and permitted command families. Defaults come from ozzgraph.policy so
# config and the runtime gate share one source of truth.
MAX_COMMAND_LENGTH_ENV = "OZZGRAPH_MAX_COMMAND_LENGTH"
TARGET_ALLOWLIST_ENV = "OZZGRAPH_TARGET_ALLOWLIST"
ALLOWED_COMMAND_FAMILIES_ENV = "OZZGRAPH_ALLOWED_COMMAND_FAMILIES"

# Flag provenance and submission knobs (PR22): the deterministic flag
# pattern the candidate extractor scans observation/artifact text with,
# and the submission attempt cap (per candidate and in total) the
# supervisor-only coordinator enforces.
FLAG_PATTERN_ENV = "OZZGRAPH_FLAG_PATTERN"
MAX_SUBMISSIONS_ENV = "OZZGRAPH_MAX_SUBMISSIONS"

DEFAULT_STATE_DIR = "state"
DEFAULT_ARTIFACT_DIR = "state/artifacts"

# Heartbeat emits a progress line every interval.
DEFAULT_HEARTBEAT_INTERVAL_S = 30
# A run is forcibly terminated once it exceeds its runtime budget.
DEFAULT_MAX_RUNTIME_S = 7200
# Cumulative budgets. Zero means "unlimited" (no upper bound).
DEFAULT_MAX_TOKENS = 0
DEFAULT_MAX_MODEL_CALLS = 0
DEFAULT_MAX_TOOL_CALLS = 0
# Maximum concurrent workers (bounded parallelization).
DEFAULT_MAX_WORKERS = 4
# Paid hints are supervisor-only and bounded (max one per detonation).
DEFAULT_MAX_HINTS = 1

# Safe default flag pattern: `flag{...}` with no braces or whitespace
# inside (docs/TECHNICAL_REQUIREMENTS.md, "Flag Submission": a candidate
# must match known format). Overridable via OZZGRAPH_FLAG_PATTERN for
# challenge-specific formats.
DEFAULT_FLAG_PATTERN = r"flag\{[^{}\s]+\}"
# Submission attempt cap: a candidate is submitted at most this many
# times, and the run performs at most this many total submissions
# (docs/TECHNICAL_REQUIREMENTS.md, "Flag Submission": attempt limits).
DEFAULT_MAX_SUBMISSIONS = 3


class ConfigError(RuntimeError):
    """Raised when runtime configuration is missing or invalid."""


class OzzGraphConfig(BaseModel):
    """Validated runtime configuration.

    Attributes:
        hal_user_id: Operator identity, required from ``HAL_USER_ID``. The
            supervisor prints it immediately at startup so the competition
            platform can attribute the run.
        state_dir: Root directory for durable runtime state (graph, events).
        artifact_dir: Directory for raw tool output and downloaded files.
        heartbeat_interval_s: Seconds between heartbeat progress lines.
        max_runtime_s: Wall-clock budget for the run; the supervisor
            terminates with a budget-exhausted reason when exceeded.
        max_tokens: Cumulative token budget across model calls; ``0`` = no cap.
        max_model_calls: Cumulative model-call budget; ``0`` = no cap.
        max_tool_calls: Cumulative tool-call budget; ``0`` = no cap.
        max_workers: Maximum concurrent workers.
        max_hints: Maximum paid hints the supervisor may purchase.
        max_command_length: Ceiling for a single command line, in
            characters; longer commands are rejected by the scope
            policy before execution.
        target_allowlist: Hosts, IPs, and CIDR networks that commands
            may address (comma-separated); empty means no external
            destination is permitted (fail closed).
        allowed_command_families: Command families permitted at the
            policy level (comma-separated); phases and worker scopes
            narrow this per call.
        flag_pattern: Regular expression the flag candidate extractor
            matches observation/artifact text against (PR22).
        max_submissions: Attempt cap for flag submission — per
            candidate and in total (PR22).
    """

    hal_user_id: str = Field(min_length=1, pattern=r"^\S+$")
    state_dir: Path = Field(default=Path(DEFAULT_STATE_DIR))
    artifact_dir: Path = Field(default=Path(DEFAULT_ARTIFACT_DIR))

    heartbeat_interval_s: int = Field(default=DEFAULT_HEARTBEAT_INTERVAL_S, gt=0)
    max_runtime_s: int = Field(default=DEFAULT_MAX_RUNTIME_S, gt=0)
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=0)
    max_model_calls: int = Field(default=DEFAULT_MAX_MODEL_CALLS, ge=0)
    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, ge=0)
    max_workers: int = Field(default=DEFAULT_MAX_WORKERS, ge=1)
    max_hints: int = Field(default=DEFAULT_MAX_HINTS, ge=1)

    max_command_length: int = Field(default=DEFAULT_MAX_COMMAND_LENGTH, ge=1)
    target_allowlist: tuple[str, ...] = Field(default=DEFAULT_TARGET_ALLOWLIST)
    allowed_command_families: tuple[str, ...] = Field(default=DEFAULT_ALLOWED_COMMAND_FAMILIES)

    flag_pattern: str = Field(default=DEFAULT_FLAG_PATTERN, min_length=1)
    max_submissions: int = Field(default=DEFAULT_MAX_SUBMISSIONS, ge=1)

    @field_validator("flag_pattern")
    @classmethod
    def _flag_pattern_must_compile(cls, value: str) -> str:
        """Reject an invalid flag pattern regex loudly (PR22).

        The extractor compiles this pattern at construction; validating
        it here surfaces a bad ``OZZGRAPH_FLAG_PATTERN`` at load time as
        a configuration error instead of a mid-run crash.
        """
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"flag_pattern must be a valid regular expression: {exc}") from exc
        return value


def _first_nonempty(mapping: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value.strip() != "":
            return value
    return None


def _env_str(environ: Mapping[str, str], key: str, default: str) -> str:
    """Read a string environment variable, falling back to a default.

    Blank variables fall back to the default (matching ``_env_int``).
    """
    raw = _first_nonempty(environ, key)
    return default if raw is None else raw


def _env_int(environ: Mapping[str, str], key: str, default: int) -> int:
    """Parse an integer environment variable, falling back to a default.

    Raises:
        ConfigError: If the variable is set but not a valid integer.
    """
    raw = _first_nonempty(environ, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"environment variable {key} must be an integer, got {raw!r}") from None


def _env_csv(environ: Mapping[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a comma-separated environment variable, falling back to a default.

    Blank variables fall back to the default; blank entries are dropped.
    """
    raw = _first_nonempty(environ, key)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_config(environ: Mapping[str, str] | None = None) -> OzzGraphConfig:
    """Build validated configuration from environment variables.

    Args:
        environ: Environment mapping. Defaults to ``os.environ``.

    Raises:
        ConfigError: If ``HAL_USER_ID`` is missing or the resulting model
            fails validation.

    Returns:
        A validated :class:`OzzGraphConfig`.
    """
    env = os.environ if environ is None else environ

    user_id = _first_nonempty(env, HAL_USER_ID_ENV)
    if user_id is None:
        raise ConfigError(f"missing required environment variable {HAL_USER_ID_ENV}")

    state_dir = Path(env.get(STATE_DIR_ENV, DEFAULT_STATE_DIR))
    artifact_dir = Path(env.get(ARTIFACT_DIR_ENV, str(state_dir / "artifacts")))

    try:
        return OzzGraphConfig(
            hal_user_id=user_id,
            state_dir=state_dir,
            artifact_dir=artifact_dir,
            heartbeat_interval_s=_env_int(
                env, HEARTBEAT_INTERVAL_ENV, DEFAULT_HEARTBEAT_INTERVAL_S
            ),
            max_runtime_s=_env_int(env, MAX_RUNTIME_ENV, DEFAULT_MAX_RUNTIME_S),
            max_tokens=_env_int(env, MAX_TOKENS_ENV, DEFAULT_MAX_TOKENS),
            max_model_calls=_env_int(env, MAX_MODEL_CALLS_ENV, DEFAULT_MAX_MODEL_CALLS),
            max_tool_calls=_env_int(env, MAX_TOOL_CALLS_ENV, DEFAULT_MAX_TOOL_CALLS),
            max_workers=_env_int(env, MAX_WORKERS_ENV, DEFAULT_MAX_WORKERS),
            max_hints=_env_int(env, MAX_HINTS_ENV, DEFAULT_MAX_HINTS),
            max_command_length=_env_int(env, MAX_COMMAND_LENGTH_ENV, DEFAULT_MAX_COMMAND_LENGTH),
            target_allowlist=_env_csv(env, TARGET_ALLOWLIST_ENV, DEFAULT_TARGET_ALLOWLIST),
            allowed_command_families=_env_csv(
                env, ALLOWED_COMMAND_FAMILIES_ENV, DEFAULT_ALLOWED_COMMAND_FAMILIES
            ),
            flag_pattern=_env_str(env, FLAG_PATTERN_ENV, DEFAULT_FLAG_PATTERN),
            max_submissions=_env_int(env, MAX_SUBMISSIONS_ENV, DEFAULT_MAX_SUBMISSIONS),
        )
    except ValidationError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"invalid configuration: {exc}") from exc
