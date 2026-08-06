"""Runtime configuration for OzzGraph (PR2).

Configuration is parsed from environment variables into a validated Pydantic
v2 model. No secrets or model-specific settings live here — this module owns
identity and runtime-directory layout only. Heartbeat/budget knobs arrive in
PR3; logging level arrives with PR4's structured logging.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

HAL_USER_ID_ENV = "HAL_USER_ID"
STATE_DIR_ENV = "OZZGRAPH_STATE_DIR"
ARTIFACT_DIR_ENV = "OZZGRAPH_ARTIFACT_DIR"

DEFAULT_STATE_DIR = "state"
DEFAULT_ARTIFACT_DIR = "state/artifacts"


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
    """

    hal_user_id: str = Field(min_length=1, pattern=r"^\S+$")
    state_dir: Path = Field(default=Path(DEFAULT_STATE_DIR))
    artifact_dir: Path = Field(default=Path(DEFAULT_ARTIFACT_DIR))


def _first_nonempty(mapping: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value.strip() != "":
            return value
    return None


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
        )
    except ValidationError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"invalid configuration: {exc}") from exc
