"""Runtime configuration for OzzGraph (PR2/PR3).

Configuration is parsed from environment variables into a validated Pydantic
v2 model. No secrets or model-specific settings live here — this module owns
identity, runtime-directory layout, the heartbeat/budget knobs, and the scope
policy knobs (command-length limit, target allowlist, permitted command
families). Structured logging level arrives with PR4.

V08 (v2/local-assessment) adds the optional scope file
(``OZZGRAPH_SCOPE_FILE``: JSON/YAML/TOML allowlist entries merged into
``target_allowlist``) and the optional credentials file
(``OZZGRAPH_CREDENTIALS_FILE``: credential *references* — name/kind/username
plus the name of the environment variable holding the secret; the secret value
itself never enters the file or the config, docs/adr/0010). Both load
deterministically and fail loudly (``ConfigError``) on malformed input.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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

# V08 (v2/local-assessment): optional scope and credentials files. The
# scope file holds a list of allowlist entries merged into
# ``target_allowlist``; the credentials file holds *references* to
# operator-supplied credentials (name/kind/username + the NAME of the
# environment variable whose value is the secret) — the secret value
# itself never enters the file or the config (docs/adr/0010).
SCOPE_FILE_ENV = "OZZGRAPH_SCOPE_FILE"
CREDENTIALS_FILE_ENV = "OZZGRAPH_CREDENTIALS_FILE"

# V09 (v2/halctf-adapter): deterministic env-based discovery of the
# HalCTF runtime (docs/adr/0011). HalCTF mode is selected by the
# presence of any HalCTF runtime variable; the MCP endpoint is resolved
# from the first non-blank of :data:`HALCTF_ENDPOINT_CANDIDATES`. The
# local default is unchanged: with none of these variables set, the run
# uses :class:`~ozzgraph.environments.local.LocalEnvironment` and the
# V08 ``OZZGRAPH_TARGET`` classification stays authoritative.
# ``HAL_USER_ID`` is operator identity, required for EVERY run (local
# included) — it is deliberately NOT a mode selector.
HAL_CTF_ID_ENV = "HAL_CTF_ID"
HAL_CHALLENGE_ID_ENV = "HAL_CHALLENGE_ID"
HAL_ENDPOINT_ENV = "HAL_ENDPOINT"
HAL_MCP_ENDPOINT_ENV = "HAL_MCP_ENDPOINT"
MCP_ENDPOINT_ENV = "MCP_ENDPOINT"
OPENAI_BASE_URL_ENV = "OPENAI_BASE_URL"

#: Ordered endpoint candidates for the HalCTF MCP runtime (V09): the
#: first non-blank wins. ``OZZGRAPH_MCP_BASE_URL`` is the explicit
#: legacy knob (its literal mirrors ``hal_client.MCP_BASE_URL_ENV`` —
#: hal_client consults this discovery function, so the two cannot
#: disagree); the ``HAL_*`` family is the platform-injected shape;
#: ``MCP_ENDPOINT`` and ``OPENAI_BASE_URL`` are generic platform
#: variables that may carry the endpoint. ``OPENAI_BASE_URL`` alone
#: does NOT select HalCTF mode (it is a model endpoint in local mode) —
#: it only resolves the endpoint once another variable selected the
#: mode.
HALCTF_ENDPOINT_CANDIDATES: tuple[str, ...] = (
    "OZZGRAPH_MCP_BASE_URL",
    HAL_MCP_ENDPOINT_ENV,
    HAL_ENDPOINT_ENV,
    MCP_ENDPOINT_ENV,
    OPENAI_BASE_URL_ENV,
)

#: Variables that select HalCTF mode (V09): any non-blank value means
#: the run targets the HalCTF runtime, not a local assessment.
HALCTF_CHALLENGE_ID_VARS: tuple[str, ...] = (
    HAL_CTF_ID_ENV,
    HAL_CHALLENGE_ID_ENV,
    "OZZGRAPH_CHALLENGE_ID",
)
HALCTF_MODE_VARS: tuple[str, ...] = (
    *HALCTF_CHALLENGE_ID_VARS,
    HAL_MCP_ENDPOINT_ENV,
    HAL_ENDPOINT_ENV,
    MCP_ENDPOINT_ENV,
)

# HAL-001 (HalCTF real-runtime snapshot): the competition platform
# injects named service targets as ``HAL_TARGET_<NAME>_IP`` /
# ``HAL_TARGET_<NAME>_PORT`` pairs (one pair per named service) plus a
# single-service ``HAL_TARGET_IP`` / ``HAL_TARGET_PORT`` form, challenge
# metadata (``HAL_CHALLENGE_*``), runtime identity (``HAL_AGENT_MODEL`` /
# ``HAL_RUN_ID`` / ``HAL_TEAM_UUID``), flag-like variables
# (``BONUS_FLAG``, ``FLAG_*``), and infrastructure endpoints
# (``OPENAI_BASE_URL`` / ``MCP_ENDPOINT``) that are model/MCP
# infrastructure — never attack targets. The exact shape was verified
# cross-repo from kazuki005276ssh/halctf-team-tottori committed
# live-run logs.
HAL_TARGET_IP_ENV = "HAL_TARGET_IP"
HAL_TARGET_PORT_ENV = "HAL_TARGET_PORT"
HAL_TARGET_ENV_PREFIX = "HAL_TARGET_"
HAL_TARGET_IP_SUFFIX = "_IP"
HAL_TARGET_PORT_SUFFIX = "_PORT"
HAL_CHALLENGE_NAME_ENV = "HAL_CHALLENGE_NAME"
HAL_CHALLENGE_CATEGORY_ENV = "HAL_CHALLENGE_CATEGORY"
HAL_AGENT_MODEL_ENV = "HAL_AGENT_MODEL"
HAL_RUN_ID_ENV = "HAL_RUN_ID"
HAL_TEAM_UUID_ENV = "HAL_TEAM_UUID"
BONUS_FLAG_ENV = "BONUS_FLAG"
FLAG_LIKE_PREFIX = "FLAG_"

#: The HalCTF sidecar MCP authority (verified from live-run logs): the
#: sidecar is infrastructure, never an attack target.
HALCTF_SIDECAR_AUTHORITY = "127.0.0.1:9000"

#: Accepted document suffixes for scope/credentials files, by format.
_SCOPE_FILE_SUFFIXES = (".json", ".yaml", ".yml", ".toml")

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


class Credential(BaseModel):
    """A *reference* to an operator-supplied credential (V08).

    Never carries the secret itself (AGENTS.md: no secrets in config):
    ``secret_env`` names the environment variable whose VALUE is the
    secret, read at runtime by the consuming component
    (:func:`credential_secret`). A credential must carry at least one
    of ``username`` / ``secret_env`` — a record with neither holds
    nothing.

    Attributes:
        name: Stable credential name (referenced from scope data).
        kind: Credential kind (e.g. ``http_basic``, ``api_token``,
            ``ssh_key``) — a bounded label, never a secret.
        username: Optional username for the credential.
        secret_env: Name of the environment variable holding the
            secret value; validated as a well-formed variable name.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    username: str | None = Field(default=None, max_length=256)
    secret_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @model_validator(mode="after")
    def _at_least_one_secret_source(self) -> Credential:
        """Reject a credential with neither a username nor a secret env."""
        if self.username is None and self.secret_env is None:
            raise ValueError("credential must carry a username or a secret_env reference")
        return self


def credential_secret(credential: Credential, environ: Mapping[str, str] | None = None) -> str:
    """Resolve a credential's secret from its named environment variable.

    The secret lives ONLY in the environment: it is never stored in the
    credentials file, in the config model, or in any graph entity
    (AGENTS.md: no committed secrets). A credential without
    ``secret_env`` has no secret to resolve — callers holding only a
    username (e.g. a public CI token with an empty secret) get an empty
    string.

    Raises:
        ConfigError: If ``secret_env`` is set but the named variable is
            missing or blank in the environment (fail loudly).
    """
    if credential.secret_env is None:
        return ""
    env = os.environ if environ is None else environ
    value = env.get(credential.secret_env)
    if value is None or value.strip() == "":
        raise ConfigError(
            f"credential {credential.name!r} references "
            f"{credential.secret_env!r}, which is not set in the environment"
        )
    return value


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
        scope_file: Optional path to a scope file whose allowlist
            entries are merged into ``target_allowlist`` (V08).
        credentials: Credential *references* (name/kind/username +
            secret env-var names), never secrets (V08).
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

    # V08 (v2/local-assessment): optional scope file (allowlist entries
    # merged into ``target_allowlist`` at load time) and the credential
    # reference list (names/kinds/usernames + secret env-var names —
    # never the secrets themselves).
    scope_file: Path | None = None
    credentials: tuple[Credential, ...] = Field(default_factory=tuple)

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


# ---------------------------------------------------------------------------
# V09: deterministic HalCTF runtime discovery (docs/adr/0011)
# ---------------------------------------------------------------------------


def halctf_mode_selected(environ: Mapping[str, str]) -> bool:
    """True when the environment selects the HalCTF runtime (V09).

    HalCTF mode is selected by the presence of any
    :data:`HALCTF_MODE_VARS` variable (``HAL_CTF_ID``,
    ``HAL_CHALLENGE_ID``, ``HAL_ENDPOINT``, ``HAL_MCP_ENDPOINT``,
    ``MCP_ENDPOINT``, or the legacy ``OZZGRAPH_CHALLENGE_ID``).
    ``HAL_USER_ID`` is identity, required for every run — it never
    selects HalCTF mode, so the local default (``LocalEnvironment``
    with the V08 ``OZZGRAPH_TARGET`` classification) is unchanged when
    no HalCTF runtime variable is set.
    """
    return _first_nonempty(environ, *HALCTF_MODE_VARS) is not None


def discover_halctf_challenge_id(environ: Mapping[str, str]) -> str:
    """The configured HalCTF challenge id, or ``""`` when unset.

    Deterministic first-non-blank over :data:`HALCTF_CHALLENGE_ID_VARS`
    (``HAL_CTF_ID``, ``HAL_CHALLENGE_ID``, legacy
    ``OZZGRAPH_CHALLENGE_ID``).
    """
    return _first_nonempty(environ, *HALCTF_CHALLENGE_ID_VARS) or ""


def discover_halctf_endpoint(environ: Mapping[str, str]) -> str | None:
    """The configured HalCTF MCP endpoint, or None when no candidate is set.

    Deterministic first-non-blank over
    :data:`HALCTF_ENDPOINT_CANDIDATES` (``OZZGRAPH_MCP_BASE_URL``,
    ``HAL_MCP_ENDPOINT``, ``HAL_ENDPOINT``, ``MCP_ENDPOINT``,
    ``OPENAI_BASE_URL``). None means HalCTF mode cannot reach the
    platform — the caller fails loudly.
    """
    return _first_nonempty(environ, *HALCTF_ENDPOINT_CANDIDATES)


def require_halctf_endpoint(environ: Mapping[str, str]) -> str:
    """Resolve the HalCTF MCP endpoint, failing loudly when missing.

    HalCTF mode without a discoverable endpoint is a configuration
    error (AGENTS.md rule #9): the adapter cannot reach the platform,
    so the run must not start. The environment adapter calls this at
    construction; ``load_config`` calls it whenever
    :func:`halctf_mode_selected` is true, so the CLI fails at startup.

    Raises:
        ConfigError: If no endpoint candidate is set.
    """
    endpoint = discover_halctf_endpoint(environ)
    if endpoint is None:
        candidates = ", ".join(HALCTF_ENDPOINT_CANDIDATES)
        raise ConfigError(
            f"HalCTF mode is selected but no MCP endpoint is configured: set one of {candidates}"
        )
    return endpoint


@dataclass(frozen=True)
class HalCTFTargetService:
    """One named HalCTF target service parsed from ``HAL_TARGET_*`` env (HAL-001).

    Attributes:
        name: Service name — ``"default"`` for the single-service
            ``HAL_TARGET_IP`` form, otherwise the ``<NAME>`` of the
            ``HAL_TARGET_<NAME>_IP`` pair (casefolded).
        ip: The injected service IP address.
        port: The injected service port, or ``None`` when
            ``HAL_TARGET_<NAME>_PORT`` is unset/blank — a host-only
            entry; no port is ever invented.
    """

    name: str
    ip: str
    port: int | None


def _url_authority(value: str) -> str:
    """Normalized ``host[:port]`` authority of a URL or bare host[:port]."""
    text = value.strip()
    if "://" in text:
        text = urlsplit(text).netloc
    if "@" in text:  # strip userinfo
        text = text.rsplit("@", 1)[1]
    return text.casefold()


def halctf_infra_authorities(environ: Mapping[str, str]) -> frozenset[str]:
    """Authorities that are HalCTF infrastructure, never attack targets (HAL-001).

    The sidecar (``127.0.0.1:9000``), the model service
    (``OPENAI_BASE_URL``), the MCP server (``MCP_ENDPOINT``), and the
    resolved MCP endpoint itself — all ``host[:port]`` normalized. A
    ``HAL_TARGET_*`` service whose host[:port] matches one of these is
    infrastructure, not an assessment target, and is excluded.
    """
    authorities = {HALCTF_SIDECAR_AUTHORITY}
    for key in (OPENAI_BASE_URL_ENV, MCP_ENDPOINT_ENV):
        value = _first_nonempty(environ, key)
        if value is not None:
            authorities.add(_url_authority(value))
    endpoint = discover_halctf_endpoint(environ)
    if endpoint is not None:
        authorities.add(_url_authority(endpoint))
    return frozenset(authorities)


def _optional_port(environ: Mapping[str, str], key: str) -> int | None:
    """Optional port env: unset/blank -> ``None``; invalid -> ``ConfigError``.

    Raises:
        ConfigError: If the variable is set but not a valid integer
            (fail loudly, AGENTS.md rule #9).
    """
    raw = _first_nonempty(environ, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"environment variable {key} must be an integer, got {raw!r}") from None


def discover_halctf_services(environ: Mapping[str, str]) -> tuple[HalCTFTargetService, ...]:
    """Parse ``HAL_TARGET_*`` env into the ordered target services (HAL-001).

    Sources, in deterministic (name-sorted) order:

    - the single-service form ``HAL_TARGET_IP`` with the optional
      ``HAL_TARGET_PORT`` -> service named ``"default"``;
    - every ``HAL_TARGET_<NAME>_IP`` / ``HAL_TARGET_<NAME>_PORT`` pair
      -> service named ``<NAME>``.

    A service with an IP but no PORT stays a host-only entry (no port is
    invented); a set-but-invalid PORT fails loudly (``ConfigError``).
    Services whose host[:port] matches a HalCTF infrastructure
    authority (:func:`halctf_infra_authorities`) are excluded — the
    sidecar, model service, and MCP server are not attack targets.
    """
    raw: dict[str, HalCTFTargetService] = {}
    ip = _first_nonempty(environ, HAL_TARGET_IP_ENV)
    if ip is not None:
        raw["default"] = HalCTFTargetService(
            name="default",
            ip=ip,
            port=_optional_port(environ, HAL_TARGET_PORT_ENV),
        )
    for key, value in environ.items():
        if not key.startswith(HAL_TARGET_ENV_PREFIX) or not key.endswith(HAL_TARGET_IP_SUFFIX):
            continue
        name = key[len(HAL_TARGET_ENV_PREFIX) : -len(HAL_TARGET_IP_SUFFIX)]
        if not name:
            continue  # the bare HAL_TARGET_IP form, handled above
        ip_value = value.strip()
        if not ip_value:
            continue
        port_key = f"{HAL_TARGET_ENV_PREFIX}{name}{HAL_TARGET_PORT_SUFFIX}"
        raw[name.casefold()] = HalCTFTargetService(
            name=name.casefold(),
            ip=ip_value,
            port=_optional_port(environ, port_key),
        )
    infra = halctf_infra_authorities(environ)
    services: list[HalCTFTargetService] = []
    for service_name in sorted(raw):
        service = raw[service_name]
        authority = f"{service.ip}:{service.port}" if service.port is not None else service.ip
        if authority.casefold() in infra:
            continue
        services.append(service)
    return tuple(services)


def halctf_target_allowlist(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The policy allowlist entries derived from ``HAL_TARGET_*`` (HAL-001).

    Each service contributes its bare IP (so bare-IP destinations such
    as ``nmap 10.0.0.5`` pass the policy gate) and, when it carries a
    port, its ``IP:PORT`` authority (so URL destinations such as
    ``curl http://10.0.0.5:8080`` pass the gate). Sorted and
    deduplicated; deterministic.
    """
    entries: set[str] = set()
    for service in discover_halctf_services(environ):
        entries.add(service.ip)
        if service.port is not None:
            entries.add(f"{service.ip}:{service.port}")
    return tuple(sorted(entries))


@dataclass(frozen=True)
class HalCTFRuntimeSnapshot:
    """Full platform-injected HalCTF runtime metadata (HAL-001).

    One frozen value object capturing EVERYTHING the competition
    platform injects for a live run, parsed deterministically from
    ``environ`` by :func:`build_halctf_runtime_snapshot`: the
    ``HAL_TARGET_*`` services (already infra-filtered), the challenge
    metadata (``HAL_CHALLENGE_ID`` / ``HAL_CHALLENGE_NAME`` /
    ``HAL_CHALLENGE_CATEGORY``), runtime identity (``HAL_AGENT_MODEL``
    / ``HAL_RUN_ID`` / ``HAL_TEAM_UUID``), the flag-like variables
    (``BONUS_FLAG`` + every ``FLAG_*``), and the model/MCP
    infrastructure endpoints (``OPENAI_BASE_URL`` /
    ``MCP_ENDPOINT``). Absent metadata is ``None`` — never invented;
    ``flag_like`` is empty when no flag-like variable is set.

    Attributes:
        services: The infra-filtered ``HAL_TARGET_*`` services
            (:func:`discover_halctf_services`).
        challenge_id: The resolved challenge id (first non-blank of
            :data:`HALCTF_CHALLENGE_ID_VARS`), or ``None``.
        challenge_name: ``HAL_CHALLENGE_NAME``, or ``None``.
        challenge_category: ``HAL_CHALLENGE_CATEGORY``, or ``None``.
        agent_model: ``HAL_AGENT_MODEL``, or ``None``.
        run_id: ``HAL_RUN_ID``, or ``None``.
        team_uuid: ``HAL_TEAM_UUID``, or ``None``.
        flag_like: Non-blank values of ``BONUS_FLAG`` then every
            ``FLAG_*`` variable (sorted by variable name) — the
            platform-injected flag-like environment, in deterministic
            order.
        openai_base_url: ``OPENAI_BASE_URL``, or ``None``.
        mcp_endpoint: The resolved MCP endpoint
            (:func:`discover_halctf_endpoint` — the same resolution the
            environment adapter drives), or ``None``.
    """

    services: tuple[HalCTFTargetService, ...]
    challenge_id: str | None
    challenge_name: str | None
    challenge_category: str | None
    agent_model: str | None
    run_id: str | None
    team_uuid: str | None
    flag_like: tuple[str, ...]
    openai_base_url: str | None
    mcp_endpoint: str | None


def build_halctf_runtime_snapshot(environ: Mapping[str, str]) -> HalCTFRuntimeSnapshot:
    """Parse the full platform-injected HalCTF runtime from ``environ`` (HAL-001).

    Every field comes from the existing deterministic discovery helpers
    (first non-blank wins), so the snapshot and the environment adapter
    / policy gate can never disagree: services via
    :func:`discover_halctf_services` (infra-excluded), challenge id
    first-non-blank over :data:`HALCTF_CHALLENGE_ID_VARS`, the MCP
    endpoint via :func:`discover_halctf_endpoint`. Absent or blank
    metadata parses to ``None`` — challenge metadata is never required
    for a run, so its absence is graceful; the only loud failure is
    truly unrecoverable configuration (``ConfigError`` from
    :func:`discover_halctf_services` on a set-but-invalid
    ``HAL_TARGET_*`` port).
    """
    flag_like: list[str] = []
    bonus_flag = _first_nonempty(environ, BONUS_FLAG_ENV)
    if bonus_flag is not None:
        flag_like.append(bonus_flag)
    for key in sorted(environ):
        if not key.startswith(FLAG_LIKE_PREFIX):
            continue
        value = _first_nonempty(environ, key)
        if value is not None:
            flag_like.append(value)
    return HalCTFRuntimeSnapshot(
        services=discover_halctf_services(environ),
        challenge_id=_first_nonempty(environ, *HALCTF_CHALLENGE_ID_VARS),
        challenge_name=_first_nonempty(environ, HAL_CHALLENGE_NAME_ENV),
        challenge_category=_first_nonempty(environ, HAL_CHALLENGE_CATEGORY_ENV),
        agent_model=_first_nonempty(environ, HAL_AGENT_MODEL_ENV),
        run_id=_first_nonempty(environ, HAL_RUN_ID_ENV),
        team_uuid=_first_nonempty(environ, HAL_TEAM_UUID_ENV),
        flag_like=tuple(flag_like),
        openai_base_url=_first_nonempty(environ, OPENAI_BASE_URL_ENV),
        mcp_endpoint=discover_halctf_endpoint(environ),
    )


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


def _env_path(environ: Mapping[str, str], key: str) -> Path | None:
    """Read an optional path environment variable; blank means unset."""
    raw = _first_nonempty(environ, key)
    return None if raw is None else Path(raw)


def _load_document(path: Path) -> object:
    """Parse a JSON / YAML / TOML document, selected by file suffix.

    Raises:
        ConfigError: If the file cannot be read, its suffix is not a
            supported document format, or its content does not parse
            (fail loudly, AGENTS.md rule #9).
    """
    suffix = path.suffix.casefold()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        if suffix == ".json":
            return json.loads(raw)
        if suffix in (".yaml", ".yml"):
            return yaml.safe_load(raw)
        if suffix == ".toml":
            return tomllib.loads(raw)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"malformed document {path}: {exc}") from exc
    raise ConfigError(
        f"unsupported file format {suffix!r} for {path}; use one of "
        f"{', '.join(_SCOPE_FILE_SUFFIXES)}"
    )


def _load_scope_entries(path: Path) -> tuple[str, ...]:
    """The sorted, deduplicated allowlist entries from a scope file.

    Accepted shapes (deterministic): a top-level list of strings, or an
    object holding an ``allowlist`` list of strings (the natural TOML
    shape). Blank entries are rejected.

    Raises:
        ConfigError: If the document is not one of the accepted shapes
            or holds non-string entries.
    """
    document = _load_document(path)
    entries: list[object]
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        raw = document.get("allowlist")
        if not isinstance(raw, list):
            raise ConfigError(
                f"scope file {path} must be a list of allowlist entries or an "
                "object with an 'allowlist' list of entries"
            )
        entries = raw
    else:
        raise ConfigError(
            f"scope file {path} must be a list of allowlist entries or an "
            "object with an 'allowlist' list of entries"
        )
    validated: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"scope file {path} must contain only non-empty string entries")
        validated.append(entry.strip())
    return tuple(sorted(set(validated)))


def _load_credential_records(path: Path) -> tuple[Credential, ...]:
    """Validated credential references from a credentials file.

    Accepted shapes (deterministic): a top-level list of records, or an
    object holding a ``credentials`` list of records (the natural TOML
    shape). Records are validated by :class:`Credential` — unknown
    fields, blank names, or a missing username/secret_env are rejected.

    Raises:
        ConfigError: If the document is not one of the accepted shapes
            or any record fails validation.
    """
    document = _load_document(path)
    records: list[object]
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict):
        raw = document.get("credentials")
        if not isinstance(raw, list):
            raise ConfigError(
                f"credentials file {path} must be a list of credential records or "
                "an object with a 'credentials' list of records"
            )
        records = raw
    else:
        raise ConfigError(
            f"credentials file {path} must be a list of credential records or "
            "an object with a 'credentials' list of records"
        )
    for record in records:
        if not isinstance(record, dict):
            raise ConfigError(f"credentials file {path} must contain only credential objects")
    try:
        credentials = [Credential.model_validate(record) for record in records]
    except ValidationError as exc:
        raise ConfigError(f"invalid credential record in {path}: {exc}") from exc
    return tuple(sorted(credentials, key=lambda credential: credential.name))


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

    # V09: HalCTF mode without a discoverable MCP endpoint is a
    # configuration error (fail loudly at startup, AGENTS.md rule #9).
    # The local default is untouched: no HalCTF runtime variable means
    # the run is a local assessment and no endpoint is required.
    if halctf_mode_selected(env):
        require_halctf_endpoint(env)

    state_dir = Path(env.get(STATE_DIR_ENV, DEFAULT_STATE_DIR))
    artifact_dir = Path(env.get(ARTIFACT_DIR_ENV, str(state_dir / "artifacts")))

    # V08: optional scope + credentials files. A configured-but-missing
    # or malformed file raises ConfigError loudly — never a silent
    # partial allowlist (AGENTS.md rule #9).
    scope_file = _env_path(env, SCOPE_FILE_ENV)
    credentials_file = _env_path(env, CREDENTIALS_FILE_ENV)
    scope_entries = _load_scope_entries(scope_file) if scope_file is not None else ()
    credentials = _load_credential_records(credentials_file) if credentials_file is not None else ()
    allowlist = _env_csv(env, TARGET_ALLOWLIST_ENV, DEFAULT_TARGET_ALLOWLIST)
    # Scope-file entries MERGE into the allowlist (docs/adr/0010);
    # the merged set is sorted so the result is deterministic.
    merged_allowlist = tuple(sorted(set(allowlist) | set(scope_entries)))
    # HAL-001: the platform-injected HAL_TARGET_* services ARE the
    # operator's targets — derive their allowlist entries at load time so
    # the supervisor's ScopePolicy (built from config.target_allowlist)
    # authorizes exactly the discovered services with no manual
    # OZZGRAPH_TARGET_ALLOWLIST step. The environment adapter derives the
    # same entries from the same parser, so scope data and the gate can
    # never disagree.
    if halctf_mode_selected(env):
        merged_allowlist = tuple(sorted(set(merged_allowlist) | set(halctf_target_allowlist(env))))

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
            target_allowlist=merged_allowlist,
            allowed_command_families=_env_csv(
                env, ALLOWED_COMMAND_FAMILIES_ENV, DEFAULT_ALLOWED_COMMAND_FAMILIES
            ),
            flag_pattern=_env_str(env, FLAG_PATTERN_ENV, DEFAULT_FLAG_PATTERN),
            max_submissions=_env_int(env, MAX_SUBMISSIONS_ENV, DEFAULT_MAX_SUBMISSIONS),
            scope_file=scope_file,
            credentials=credentials,
        )
    except ValidationError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"invalid configuration: {exc}") from exc
