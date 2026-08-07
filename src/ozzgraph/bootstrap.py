"""Deterministic bootstrap reconnaissance for OzzGraph (PR12).

Runs once at supervisor startup, before the main idle loop, with no model
involvement (docs/TECHNICAL_REQUIREMENTS.md, "Bootstrap"). The runner:

1. parses single (``OZZGRAPH_TARGET``) and namespaced
   (``OZZGRAPH_TARGET_<NS>``) target variables into a validated
   :class:`Targets` model (``ConfigError`` on invalid input, matching
   :mod:`ozzgraph.config`),
2. retrieves challenge status via :class:`~ozzgraph.hal_client.HalClient`
   when ``OZZGRAPH_CHALLENGE_ID`` is set,
3. inspects the startup environment for ``OZZGRAPH_SMOKE_FLAG`` and, when
   set, submits it through the privileged client as a pipeline smoke test,
4. requests free hint zero (``hint.request index=0``) when a challenge id
   is available — hint zero is free, not privileged,
5. validates target reachability and runs category-appropriate
   deterministic probes, then
6. records every bootstrap step as a structured event (producer
   ``"bootstrap"``; see :mod:`ozzgraph.events` and docs/adr/0002).

Probes are deterministic and policy-gated: each target maps to exactly one
fixed command (``curl`` for HTTP/HTTPS, ``dig`` for DNS) with explicit
timeouts and output limits, executed through the bounded
:class:`~ozzgraph.shell.ShellRunner`. Every probe passes the scope policy
(length, target allowlist, platform/public-internet blocks, BOOTSTRAP-phase
command families) and the fingerprint store before execution — an empty
allowlist fails closed, so a misconfigured deployment cannot probe anything.
Probe outcomes are data (``bootstrap.reachability`` / ``bootstrap.probe_run``
events), never exceptions.

Failure policy: Hal service failures during bootstrap are recorded in the
step's event payload and are not fatal (the harness must survive MCP
outages); configuration errors (malformed targets, unknown namespace, smoke
flag without a challenge id) record ``bootstrap.failed`` and raise
:class:`ConfigError` so the supervisor terminates with a structured reason.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

from ozzgraph.config import TARGET_ALLOWLIST_ENV, ConfigError, OzzGraphConfig
from ozzgraph.events import (
    BOOTSTRAP_CHALLENGE_STATUS,
    BOOTSTRAP_FAILED,
    BOOTSTRAP_HINT_REQUESTED,
    BOOTSTRAP_HINT_UNAVAILABLE,
    BOOTSTRAP_PROBE_RUN,
    BOOTSTRAP_REACHABILITY,
    BOOTSTRAP_SMOKE_SUBMITTED,
    BOOTSTRAP_TARGETS_PARSED,
    Event,
    EventLog,
)
from ozzgraph.hal_client import HalClient, HalServiceError
from ozzgraph.halctl import CHALLENGE_ID_ENV
from ozzgraph.policy import FingerprintStore, ScopePolicy, ScopeViolationError
from ozzgraph.shell import ShellRunner

#: Producer name recorded on every bootstrap event.
PRODUCER = "bootstrap"

#: Single-target environment variable.
TARGET_ENV = "OZZGRAPH_TARGET"
#: Prefix of namespaced target variables (``OZZGRAPH_TARGET_<NS>``).
TARGET_NAMESPACED_PREFIX = "OZZGRAPH_TARGET_"
#: Smoke-test flag environment variable.
SMOKE_FLAG_ENV = "OZZGRAPH_SMOKE_FLAG"
#: Hint zero is free and not privileged (HalClient privilege model).
FREE_HINT_INDEX = 0

#: Namespace suffix (uppercase) -> probe category. Unknown namespaces are
#: rejected loudly at parse time.
_NAMESPACE_CATEGORIES: dict[str, str] = {
    "HTTP": "http",
    "HTTPS": "https",
    "DNS": "dns",
}

#: Env vars that share the ``OZZGRAPH_TARGET_`` prefix but are scope-policy
#: knobs (config.py), not namespaced targets.
_NON_TARGET_PREFIX_VARS: frozenset[str] = frozenset({TARGET_ALLOWLIST_ENV})

#: Wall-clock budget for one probe, seconds (ShellRunner's hard timeout).
PROBE_TIMEOUT_S = 10.0
#: Output capture limits for one probe (characters per stream).
PROBE_STDOUT_LIMIT = 4096
PROBE_STDERR_LIMIT = 2048

#: URL-scheme prefix detection for target normalization.
_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True)
class ProbeSpec:
    """One deterministic probe definition (category -> fixed command).

    Attributes:
        kind: Probe kind (``http``, ``https``, or ``dns``).
        command: Fixed shell command line; the literal ``{target}`` is
            replaced with the normalized target before execution.
        timeout_seconds: Wall-clock budget passed to the ShellRunner.
        stdout_limit: Maximum stdout characters to keep.
        stderr_limit: Maximum stderr characters to keep.
    """

    kind: str
    command: str
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int


#: Fixed probe commands per category. Deterministic by construction:
#: bounded by an explicit in-command timeout AND the ShellRunner timeout,
#: with output limits, and no model involvement.
PROBE_SPECS: dict[str, ProbeSpec] = {
    "http": ProbeSpec(
        kind="http",
        command="curl -sS --max-time 5 -I {target}",
        timeout_seconds=PROBE_TIMEOUT_S,
        stdout_limit=PROBE_STDOUT_LIMIT,
        stderr_limit=PROBE_STDERR_LIMIT,
    ),
    "https": ProbeSpec(
        kind="https",
        command="curl -sS --max-time 5 -I {target}",
        timeout_seconds=PROBE_TIMEOUT_S,
        stdout_limit=PROBE_STDOUT_LIMIT,
        stderr_limit=PROBE_STDERR_LIMIT,
    ),
    "dns": ProbeSpec(
        kind="dns",
        command="dig +short +time=2 +tries=1 {target} A",
        timeout_seconds=PROBE_TIMEOUT_S,
        stdout_limit=PROBE_STDOUT_LIMIT,
        stderr_limit=PROBE_STDERR_LIMIT,
    ),
}


class ProbeResult(BaseModel):
    """Outcome of one deterministic bootstrap probe.

    Attributes:
        status: ``reachable`` (exit 0, no timeout, non-empty output),
            ``unreachable`` (nonzero exit, timeout, or empty output), or
            ``blocked`` (rejected by the scope policy before execution).
        detail: Human-readable reason for the outcome.
        exit_code: Process exit code, None when the probe never ran.
        stdout: Captured stdout (truncated to the probe's limit).
        stderr: Captured stderr (truncated to the probe's limit).
        duration: Wall-clock seconds of the probe, 0 when never run.
        timeout_state: True when the probe was killed by its timeout.
    """

    status: str
    detail: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timeout_state: bool = False


class ProbeRunner(Protocol):
    """Executes one probe command and returns its outcome."""

    async def run(self, spec: ProbeSpec, command: str) -> ProbeResult: ...


class PolicyProbeRunner:
    """Gate a probe through the scope policy, then run it (bounded).

    Implements AGENTS.md Security Boundaries for bootstrap probes: the
    command is validated by :class:`ScopePolicy` (command length, target
    allowlist — empty fails closed — platform/public-internet blocks,
    BOOTSTRAP-phase command families), its fingerprint is recorded in the
    :class:`FingerprintStore` (rejecting duplicates), and only then is it
    executed by the bounded :class:`ShellRunner`. Policy rejections are
    returned as ``blocked`` :class:`ProbeResult` outcomes — probe data,
    not exceptions.

    Args:
        policy: The scope policy enforcing the target allowlist.
        store: The fingerprint store recording every approved probe.
        shell: The bounded shell runner executing probe commands.
        working_directory: Directory probes run in; must exist.
    """

    def __init__(
        self,
        *,
        policy: ScopePolicy,
        store: FingerprintStore,
        shell: ShellRunner,
        working_directory: Path,
    ) -> None:
        self._policy = policy
        self._store = store
        self._shell = shell
        self._working_directory = working_directory

    async def run(self, spec: ProbeSpec, command: str) -> ProbeResult:
        try:
            decision = self._policy.check(command, phase="BOOTSTRAP")
            self._store.record(decision.fingerprint, canonical=decision.canonical)
        except ScopeViolationError as exc:
            return ProbeResult(status="blocked", detail=str(exc))
        result = await self._shell.run(
            command=command,
            timeout_seconds=spec.timeout_seconds,
            stdout_limit=spec.stdout_limit,
            stderr_limit=spec.stderr_limit,
            working_directory=self._working_directory,
        )
        if result.exit_code == 0 and not result.timeout_state and result.stdout.strip():
            status = "reachable"
            detail = f"exit_code=0 duration={result.duration:.3f}s"
        elif result.timeout_state:
            status = "unreachable"
            detail = f"timed out after {spec.timeout_seconds:g}s"
        else:
            status = "unreachable"
            detail = f"exit_code={result.exit_code} output={'present' if result.stdout.strip() else 'empty'}"
        return ProbeResult(
            status=status,
            detail=detail,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration=result.duration,
            timeout_state=result.timeout_state,
        )


@dataclass(frozen=True)
class TargetSpec:
    """One concrete target to probe, derived from the parsed Targets.

    Attributes:
        name: ``"single"`` for ``OZZGRAPH_TARGET``, otherwise the
            namespace suffix (e.g. ``"HTTP"``).
        category: Probe category (``http``, ``https``, or ``dns``).
        value: Raw target value from the environment.
    """

    name: str
    category: str
    value: str


class Targets(BaseModel):
    """Validated target configuration (single + namespaced variables).

    Attributes:
        single: Value of ``OZZGRAPH_TARGET``, or None when unset.
        namespaced: Mapping of namespace suffix (e.g. ``"HTTP"``) to the
            value of ``OZZGRAPH_TARGET_<NS>``.
    """

    single: str | None = None
    namespaced: dict[str, str] = Field(default_factory=dict)

    @field_validator("namespaced")
    @classmethod
    def _namespaces_must_be_known(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject namespaced entries with no probe category (fail loudly)."""
        unknown = sorted(ns for ns in value if ns.upper() not in _NAMESPACE_CATEGORIES)
        if unknown:
            supported = ", ".join(sorted(_NAMESPACE_CATEGORIES))
            raise ValueError(f"unsupported target namespaces {unknown}; supported: {supported}")
        return value

    def specs(self) -> list[TargetSpec]:
        """Concrete targets to probe, deterministic order (single first).

        The single target's category is inferred from its value (a
        ``http(s)://`` prefix means HTTP/HTTPS, anything else is treated
        as a DNS name); namespaced entries use their namespace category.
        """
        specs: list[TargetSpec] = []
        if self.single is not None:
            specs.append(
                TargetSpec(
                    name="single",
                    category=_infer_category(self.single),
                    value=self.single,
                )
            )
        for namespace, value in sorted(self.namespaced.items()):
            specs.append(
                TargetSpec(
                    name=namespace,
                    category=_NAMESPACE_CATEGORIES[namespace.upper()],
                    value=value,
                )
            )
        return specs


def _env_str(environ: Mapping[str, str], key: str, default: str) -> str:
    """Read ``key`` from ``environ``, ignoring blank values."""
    value = environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _infer_category(value: str) -> str:
    """Probe category for the single target, inferred from its value."""
    if value.startswith("https://"):
        return "https"
    if value.startswith("http://"):
        return "http"
    return "dns"


def load_targets(environ: Mapping[str, str]) -> Targets:
    """Parse single and namespaced target variables into a model.

    ``OZZGRAPH_TARGET`` supplies the single target (blank = unset).
    Every ``OZZGRAPH_TARGET_<NS>`` variable supplies a namespaced target
    and must be non-blank with a known namespace suffix (``HTTP``,
    ``HTTPS``, ``DNS``). ``OZZGRAPH_TARGET_ALLOWLIST`` shares the prefix
    but is a scope-policy knob (config.py), not a target.

    Args:
        environ: Environment mapping (defaults are handled by the caller).

    Raises:
        ConfigError: If a namespaced variable is blank, has an empty
            namespace suffix, or uses an unsupported namespace.

    Returns:
        The validated :class:`Targets` model.
    """
    single = _env_str(environ, TARGET_ENV, "")
    namespaced: dict[str, str] = {}
    for key, value in environ.items():
        if not key.startswith(TARGET_NAMESPACED_PREFIX):
            continue
        if key in _NON_TARGET_PREFIX_VARS:
            continue
        namespace = key[len(TARGET_NAMESPACED_PREFIX) :]
        if namespace == "":
            raise ConfigError(f"environment variable {key!r} has an empty namespace suffix")
        if value is None or not value.strip():
            raise ConfigError(f"environment variable {key} must not be blank")
        category = _NAMESPACE_CATEGORIES.get(namespace.upper())
        if category is None:
            supported = ", ".join(sorted(_NAMESPACE_CATEGORIES))
            raise ConfigError(
                f"unsupported target namespace {namespace!r} in {key}; "
                f"supported namespaces: {supported}"
            )
        namespaced[namespace] = value.strip()
    try:
        return Targets(single=single or None, namespaced=namespaced)
    except ValidationError as exc:  # pragma: no cover - defensive backstop
        raise ConfigError(f"invalid target configuration: {exc}") from exc


def _normalize_target(category: str, value: str) -> str:
    """Prepend the category scheme to scheme-less HTTP/HTTPS targets."""
    if category in ("http", "https") and _SCHEME_RE.match(value) is None:
        return f"{category}://{value}"
    return value


class BootstrapRunner:
    """Runs deterministic bootstrap reconnaissance and records events.

    The runner never constructs clients or executes privileged operations
    itself: the supervisor hands it a privileged :class:`HalClient` (the
    only privileged surface, AGENTS.md invariant 5) and it uses that
    client for status retrieval, smoke submission, and the free hint.
    Probes run through an injectable :class:`ProbeRunner`
    (:class:`PolicyProbeRunner` by default) so tests never touch the
    network.

    Args:
        config: Validated runtime configuration (target allowlist,
            command knobs, state directory).
        run_id: Run identifier recorded on every event.
        event_log: Append-only log every bootstrap event is written to.
        client: HalClient for status/smoke/hint steps; must be
            privileged when the smoke flag is used (supervisor-constructed).
        environ: Environment mapping for targets/smoke/challenge-id
            variables. Defaults to ``os.environ``.
        probe_runner: Probe executor; defaults to a
            :class:`PolicyProbeRunner` built from ``config``.
    """

    def __init__(
        self,
        *,
        config: OzzGraphConfig,
        run_id: str,
        event_log: EventLog,
        client: HalClient,
        environ: Mapping[str, str] | None = None,
        probe_runner: ProbeRunner | None = None,
    ) -> None:
        self._config = config
        self._run_id = run_id
        self._event_log = event_log
        self._client = client
        self._environ = os.environ if environ is None else environ
        self._probe_runner = (
            probe_runner
            if probe_runner is not None
            else PolicyProbeRunner(
                policy=ScopePolicy(
                    max_command_length=config.max_command_length,
                    target_allowlist=config.target_allowlist,
                    allowed_command_families=config.allowed_command_families,
                ),
                store=FingerprintStore.for_run(config.state_dir),
                shell=ShellRunner(),
                working_directory=config.state_dir,
            )
        )

    async def run(self) -> None:
        """Run the full bootstrap sequence, recording every step.

        Sequence: parse targets -> challenge status -> smoke flag
        submission -> free hint zero -> per-target probes. Every step
        appends exactly one structured event.

        Raises:
            ConfigError: On malformed target configuration or a smoke
                flag with no challenge id, after recording
                ``bootstrap.failed``. Hal service failures never raise.
        """
        try:
            targets = load_targets(self._environ)
        except ConfigError as exc:
            self._record(BOOTSTRAP_FAILED, {"error_type": "ConfigError", "message": str(exc)})
            raise

        self._record(
            BOOTSTRAP_TARGETS_PARSED,
            {"single": targets.single, "namespaced": dict(targets.namespaced)},
        )

        challenge_id = _env_str(self._environ, CHALLENGE_ID_ENV, "")

        if challenge_id:
            await self._retrieve_status(challenge_id)

        smoke_flag = _env_str(self._environ, SMOKE_FLAG_ENV, "")
        if smoke_flag:
            if not challenge_id:
                message = (
                    f"{SMOKE_FLAG_ENV} is set but {CHALLENGE_ID_ENV} is not: "
                    "cannot submit the smoke-test flag"
                )
                self._record(BOOTSTRAP_FAILED, {"error_type": "ConfigError", "message": message})
                raise ConfigError(message)
            await self._submit_smoke(challenge_id, smoke_flag)

        if challenge_id:
            await self._request_free_hint(challenge_id)

        for spec in targets.specs():
            await self._probe_target(spec)

    async def _retrieve_status(self, challenge_id: str) -> None:
        """Retrieve challenge status; a service failure is an event."""
        try:
            status = await self._client.get_status(challenge_id)
        except HalServiceError as exc:
            self._record(
                BOOTSTRAP_CHALLENGE_STATUS,
                {"challenge_id": challenge_id, "error": exc.message},
            )
            return
        self._record(
            BOOTSTRAP_CHALLENGE_STATUS,
            {
                "challenge_id": status.challenge_id,
                "solved": status.solved,
                "attempts": status.attempts,
                "hints_used": status.hints_used,
                "points_earned": status.points_earned,
                "updated_at": status.updated_at,
            },
        )

    async def _submit_smoke(self, challenge_id: str, flag: str) -> None:
        """Submit the smoke flag through the privileged client."""
        try:
            result = await self._client.submit_flag(challenge_id, flag)
        except HalServiceError as exc:
            self._record(
                BOOTSTRAP_SMOKE_SUBMITTED,
                {"challenge_id": challenge_id, "error": exc.message},
            )
            return
        self._record(
            BOOTSTRAP_SMOKE_SUBMITTED,
            {
                "challenge_id": result.challenge_id,
                "accepted": result.accepted,
                "message": result.message,
                "points": result.points,
                "attempts_remaining": result.attempts_remaining,
            },
        )

    async def _request_free_hint(self, challenge_id: str) -> None:
        """Request free hint zero; an unavailable hint is an event."""
        try:
            hint = await self._client.request_hint(challenge_id, FREE_HINT_INDEX)
        except HalServiceError as exc:
            self._record(
                BOOTSTRAP_HINT_UNAVAILABLE,
                {
                    "challenge_id": challenge_id,
                    "index": FREE_HINT_INDEX,
                    "error": exc.message,
                },
            )
            return
        self._record(
            BOOTSTRAP_HINT_REQUESTED,
            {
                "challenge_id": hint.challenge_id,
                "index": hint.index,
                "hint": hint.hint,
                "paid": hint.paid,
            },
        )

    async def _probe_target(self, spec: TargetSpec) -> None:
        """Probe one target, recording reachability and probe events."""
        probe = PROBE_SPECS[spec.category]
        command = probe.command.replace("{target}", _normalize_target(spec.category, spec.value))
        result = await self._probe_runner.run(probe, command)
        self._record(
            BOOTSTRAP_REACHABILITY,
            {
                "target": spec.value,
                "category": spec.category,
                "status": result.status,
                "detail": result.detail,
            },
        )
        self._record(
            BOOTSTRAP_PROBE_RUN,
            {
                "target": spec.value,
                "category": spec.category,
                "kind": probe.kind,
                "command": command,
                "exit_code": result.exit_code,
                "timeout": result.timeout_state,
                "duration": round(result.duration, 3),
                "stdout": result.stdout[:500],
                "stderr": result.stderr[:500],
            },
        )

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one bootstrap event (producer ``bootstrap``)."""
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=event_type,
                producer=PRODUCER,
                payload=payload,
            )
        )
