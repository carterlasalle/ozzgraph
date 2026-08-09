"""Sidecar transport adapter for HalCTF flag submission (HAL-004).

The real HalCTF competition sidecar — verified cross-repo from the
halctf-team-tottori deployment's live-run logs — speaks PLAIN HTTP at
``127.0.0.1:9000``, NOT JSON-RPC: ``POST /submit`` with a flag yields
``{"status": "correct", "points_awarded": 1}``, and there is also a
``POST /done`` endpoint. :class:`SidecarSubmissionClient` is the
transport adapter at that process boundary (ADR-0011: the halctf
environment owns the sidecar transport):

- ``submit_flag`` POSTs the bounded ``{"challenge_id", "flag"}`` pair
  to ``/submit`` and normalizes the observed response forms into the
  INTERNAL :class:`~ozzgraph.hal_client.SubmissionResult` schema
  (unchanged) — see :data:`ACCEPT_STATUSES` and
  :meth:`SidecarSubmissionClient.submit_flag`.
- ``done`` POSTs a bounded ``{"run_id", "reason"}`` payload to
  ``/done`` BEST-EFFORT: failures are recorded as ``sidecar.done_failed``
  events and swallowed, never fatal — a run must not fail because the
  sidecar was unreachable at teardown.

The adapter implements the
:class:`~ozzgraph.environments.halctf.submissions.SubmissionClient`
protocol (``privileged``, ``submit_flag``, ``aclose``), so the
supervisor-only
:class:`~ozzgraph.environments.halctf.submissions.SubmissionCoordinator`
drives it unchanged; the MCP :class:`~ozzgraph.hal_client.HalClient`
stays the surface for the rest of the tool set. The supervisor-only
privilege boundary is preserved: ``submit_flag`` and ``done`` raise
:class:`~ozzgraph.hal_client.HalPrivilegeError` unless the client was
constructed with ``privileged=True`` (``OZZGRAPH_HAL_PRIVILEGED`` env),
exactly like the MCP client — the coordinator's privilege check holds
for every implementer.

Endpoint resolution is env-first (HAL-002 philosophy), deterministic and
injectable: an explicit ``base_url`` always wins, then
``OZZGRAPH_SIDECAR_BASE_URL``, then the ORIGIN of the resolved MCP
endpoint (:func:`ozzgraph.config.discover_halctf_endpoint` — the
sidecar shares the MCP host:port in the real deployment:
``MCP_ENDPOINT=http://127.0.0.1:9000/mcp`` -> ``http://127.0.0.1:9000``),
falling back to the localhost default for standalone use.
``OPENAI_BASE_URL`` is never consulted (it is the model service).

Failure policy mirrors the model/MCP clients
(docs/API_AND_INTEGRATIONS.md, "Integration Failure Policy"): bounded
exponential-backoff retries on transient failures only — HTTP 429,
HTTP >= 500, and httpx transport errors. Other 4xx statuses and
unparseable response payloads never retry. Every terminal submit
failure is raised as a typed :class:`~ozzgraph.hal_client.HalServiceError`
carrying ``provider``/``status_code``/``retryable``/``message``, and a
``sidecar.failure`` event is appended to the event log when one is
provided. Retries are always bounded (no infinite retry).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

import httpx

from ozzgraph.config import discover_halctf_endpoint
from ozzgraph.events import Event, EventLog
from ozzgraph.hal_client import (
    BASE_BACKOFF_S,
    DEFAULT_MCP_MAX_RETRIES,
    DEFAULT_MCP_TIMEOUT_S,
    HAL_PRIVILEGED_ENV,
    MAX_BACKOFF_S,
    MAX_RETRY_LIMIT,
    PROVIDER,
    HalPrivilegeError,
    HalServiceError,
    SubmissionResult,
)

#: Producer name on every sidecar adapter event.
PRODUCER = "sidecar"

#: Event types emitted by the sidecar adapter (run-only, never
#: replay-required): ``sidecar.failure`` on a terminal /submit failure,
#: ``sidecar.done`` on a successful /done, ``sidecar.done_failed`` on a
#: best-effort /done failure.
SIDECAR_FAILURE_EVENT = "sidecar.failure"
SIDECAR_DONE_EVENT = "sidecar.done"
SIDECAR_DONE_FAILED_EVENT = "sidecar.done_failed"

SIDECAR_BASE_URL_ENV = "OZZGRAPH_SIDECAR_BASE_URL"
SIDECAR_TIMEOUT_ENV = "OZZGRAPH_SIDECAR_TIMEOUT_S"
SIDECAR_MAX_RETRIES_ENV = "OZZGRAPH_SIDECAR_MAX_RETRIES"

#: The real competition sidecar root (plain HTTP, NOT JSON-RPC): the MCP
#: server rides the same host:port under ``/mcp``, the sidecar speaks
#: ``/submit`` + ``/done`` at the root.
DEFAULT_SIDECAR_BASE_URL = "http://127.0.0.1:9000"
DEFAULT_SIDECAR_TIMEOUT_S = DEFAULT_MCP_TIMEOUT_S
DEFAULT_SIDECAR_MAX_RETRIES = DEFAULT_MCP_MAX_RETRIES

SUBMIT_PATH = "/submit"
DONE_PATH = "/done"

#: Sidecar status strings that mean the flag was accepted (HAL-004).
#: ``already_solved`` is an accept: the flag was correct, the platform
#: simply registered it earlier (its ``points_awarded`` may be 0).
ACCEPT_STATUSES: frozenset[str] = frozenset(
    {"correct", "accepted", "solved", "success", "already_solved"}
)

#: Response field vocabulary the normalizer reads (upstream renames are
#: absorbed here, never leaking into the schema).
_BOOL_ACCEPT_FIELDS = ("accepted", "success", "solved", "correct")
_POINTS_FIELDS = ("points_awarded", "points")
_MESSAGE_FIELDS = ("message", "msg", "detail")
_ATTEMPTS_FIELDS = ("attempts_remaining", "attempts_left")

Sleeper = Callable[[float], Awaitable[None]]


def _env_str(environ: Mapping[str, str], key: str, default: str) -> str:
    """Read ``key`` from ``environ``, ignoring blank values."""
    value = environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(environ: Mapping[str, str], key: str, default: float) -> float:
    """Parse a float environment variable, falling back to ``default``.

    Raises:
        ValueError: If the variable is set but not a valid number.
    """
    raw = _env_str(environ, key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"environment variable {key} must be a number, got {raw!r}") from None


def _env_int(environ: Mapping[str, str], key: str, default: int) -> int:
    """Parse an integer environment variable, falling back to ``default``.

    Raises:
        ValueError: If the variable is set but not a valid integer.
    """
    raw = _env_str(environ, key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"environment variable {key} must be an integer, got {raw!r}") from None


def _env_bool(environ: Mapping[str, str], key: str, default: bool) -> bool:
    """Parse a boolean environment variable, falling back to ``default``.

    Accepts ``1``/``true``/``yes``/``on`` (case-insensitive) as true.
    """
    raw = _env_str(environ, key, "").lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def _sidecar_root(mcp_endpoint: str) -> str:
    """The ``scheme://host[:port]`` origin of the MCP endpoint URL.

    The sidecar shares the MCP server's host:port in the real
    deployment (``http://127.0.0.1:9000/mcp`` -> ``http://127.0.0.1:9000``).
    Returns ``""`` for a URL without an http(s) origin.
    """
    parts = urlsplit(mcp_endpoint)
    if parts.scheme not in ("http", "https") or parts.netloc == "":
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def discover_halctf_sidecar_base_url(environ: Mapping[str, str]) -> str:
    """The sidecar base URL, resolved env-first (HAL-002 philosophy).

    Deterministic: an explicit ``OZZGRAPH_SIDECAR_BASE_URL`` wins, then
    the origin of the resolved MCP endpoint
    (:func:`ozzgraph.config.discover_halctf_endpoint` — the sidecar
    shares the MCP host:port in the real deployment), falling back to
    the localhost default for standalone use. ``OPENAI_BASE_URL`` is
    never consulted (it is the model service).
    """
    explicit = _env_str(environ, SIDECAR_BASE_URL_ENV, "")
    if explicit != "":
        return explicit
    mcp_endpoint = discover_halctf_endpoint(environ)
    if mcp_endpoint is not None:
        root = _sidecar_root(mcp_endpoint)
        if root != "":
            return root
    return DEFAULT_SIDECAR_BASE_URL


def _parse_error(attempts: int, message: str) -> HalServiceError:
    """Build a non-retryable unparseable-response failure (HTTP 200)."""
    return HalServiceError(
        provider=PROVIDER,
        status_code=200,
        retryable=False,
        message=f"unparseable sidecar response (HTTP 200): {message}",
    )


def _message(payload: Mapping[str, object], default: str) -> str:
    """The platform's message, or ``default`` when absent/not a string."""
    for key in _MESSAGE_FIELDS:
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip() != "":
            return raw.strip()
    return default


def _points(payload: Mapping[str, object]) -> int:
    """Numeric points from ``points_awarded``/``points``, 0 when absent.

    Booleans and non-numeric values are never treated as points; a
    negative value clamps to 0 (the schema requires ``points >= 0``).
    """
    for key in _POINTS_FIELDS:
        raw = payload.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return max(0, raw)
        if isinstance(raw, float) and raw.is_integer():
            return max(0, int(raw))
    return 0


def _attempts_remaining(payload: Mapping[str, object]) -> int | None:
    """The platform's remaining-attempts count, or None when absent."""
    for key in _ATTEMPTS_FIELDS:
        raw = payload.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw >= 0:
            return raw
    return None


def _boolean_signal(payload: Mapping[str, object]) -> bool | None:
    """The first explicit boolean verdict field, or None when absent.

    Precedence: ``accepted``, then ``success``, then ``solved``, then
    ``correct`` — the first PRESENT field decides.

    Raises:
        TypeError: If a verdict field is present but not a bool (fail
            loudly — never coerced).
    """
    for key in _BOOL_ACCEPT_FIELDS:
        raw = payload.get(key)
        if raw is None:
            continue
        if not isinstance(raw, bool):
            raise TypeError(f"{key!r} must be a bool, got {type(raw).__name__} ({raw!r})")
        return raw
    return None


def _normalize_submission(
    challenge_id: str, payload: Mapping[str, object], attempts: int
) -> SubmissionResult:
    """Normalize one sidecar ``/submit`` response body into SubmissionResult.

    Deterministic precedence (the platform's explicit verdict wins):

    1. ``status`` (string): a member of :data:`ACCEPT_STATUSES` ->
       accepted; any other status -> rejected.
    2. Explicit boolean verdict fields (``accepted`` / ``success`` /
       ``solved`` / ``correct``, first present wins).
    3. Points: ``points_awarded``/``points`` > 0 -> accepted; 0 or
       absent -> rejected.

    ``message`` prefers the platform's ``message``/``msg``/``detail``,
    falling back to the status text or a deterministic default; points
    and attempts_remaining map from the platform's numeric fields.
    Unknown upstream fields are ignored — the internal schema is
    unchanged (``extra="ignore"``).

    Raises:
        HalServiceError: If a verdict boolean or ``status`` is
            wrong-typed (fail loudly, never coerced).
    """
    status = payload.get("status")
    if status is not None and not isinstance(status, str):
        raise _parse_error(attempts, f"'status' must be a string, got {type(status).__name__}")
    if isinstance(status, str):
        normalized = status.strip().casefold()
        accepted = normalized in ACCEPT_STATUSES
        default_message = (
            status.strip()
            if status.strip() != ""
            else ("flag accepted" if accepted else "flag rejected")
        )
        message = _message(payload, default=default_message)
    else:
        try:
            signal = _boolean_signal(payload)
        except TypeError as exc:
            raise _parse_error(attempts, str(exc)) from exc
        if signal is not None:
            accepted = signal
        else:
            accepted = _points(payload) > 0
        message = _message(payload, default="flag accepted" if accepted else "flag rejected")
    return SubmissionResult(
        challenge_id=challenge_id,
        accepted=accepted,
        message=message,
        points=_points(payload),
        attempts_remaining=_attempts_remaining(payload),
    )


class SidecarSubmissionClient:
    """Plain-HTTP sidecar transport for HalCTF submissions (HAL-004).

    Implements the
    :class:`~ozzgraph.environments.halctf.submissions.SubmissionClient`
    protocol (``privileged``, ``submit_flag``, ``aclose``) over the real
    competition sidecar's plain-HTTP surface (``POST /submit``,
    ``POST /done``), so the supervisor-only
    :class:`~ozzgraph.environments.halctf.submissions.SubmissionCoordinator`
    drives it unchanged. The MCP
    :class:`~ozzgraph.hal_client.HalClient` stays the surface for the
    rest of the tool set.

    Configuration is constructor-injected with environment fallback
    (no secrets enter the config model): the sidecar base URL resolves
    env-first (:func:`discover_halctf_sidecar_base_url` — explicit arg,
    then ``OZZGRAPH_SIDECAR_BASE_URL``, then the MCP endpoint's origin,
    then the localhost default), plus ``OZZGRAPH_SIDECAR_TIMEOUT_S``,
    ``OZZGRAPH_SIDECAR_MAX_RETRIES``, and the shared
    ``OZZGRAPH_HAL_PRIVILEGED`` supervisor flag.

    Args:
        base_url: Sidecar base URL including no path; defaults through
            the deterministic discovery.
        timeout_s: Request timeout in seconds (> 0).
        max_retries: Retry count for transient failures; ``0`` disables
            retries; bounded to ``[0, MAX_RETRY_LIMIT]``.
        privileged: Whether this client may invoke supervisor-only
            methods; defaults to the ``OZZGRAPH_HAL_PRIVILEGED`` env var
            (false when unset).
        event_log: Optional append-only event log for
            ``sidecar.failure`` / ``sidecar.done`` /
            ``sidecar.done_failed`` events.
        run_id: Run identifier used to attribute events.
        transport: Optional httpx transport (tests inject
            ``httpx.MockTransport`` here).
        sleeper: Awaitable used for backoff sleeps; injectable for
            deterministic tests (defaults to ``asyncio.sleep``).
        environ: Environment mapping for the sidecar discovery
            variables; defaults to ``os.environ``.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        privileged: bool | None = None,
        event_log: EventLog | None = None,
        run_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Sleeper = asyncio.sleep,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        resolved_url = discover_halctf_sidecar_base_url(env)
        self._base_url = (resolved_url if base_url is None else base_url).rstrip("/")
        if not self._base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an http(s) URL, got {self._base_url!r}")

        resolved_timeout = _env_float(env, SIDECAR_TIMEOUT_ENV, DEFAULT_SIDECAR_TIMEOUT_S)
        timeout = resolved_timeout if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError(f"timeout_s must be > 0, got {timeout!r}")
        self._timeout_s = float(timeout)

        resolved_retries = _env_int(env, SIDECAR_MAX_RETRIES_ENV, DEFAULT_SIDECAR_MAX_RETRIES)
        retries = resolved_retries if max_retries is None else max_retries
        if retries < 0 or retries > MAX_RETRY_LIMIT:
            raise ValueError(f"max_retries must be in [0, {MAX_RETRY_LIMIT}], got {retries!r}")
        self._max_retries = retries

        resolved_privileged = _env_bool(env, HAL_PRIVILEGED_ENV, False)
        self._privileged = resolved_privileged if privileged is None else privileged

        self._event_log = event_log
        self._run_id = run_id
        self._sleeper = sleeper

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout_s),
            transport=transport,
        )

    @property
    def privileged(self) -> bool:
        """Whether this client may invoke supervisor-only methods.

        The supervisor-only submission coordinator (PR22) checks this
        flag before calling :meth:`submit_flag`, so the privilege
        boundary is enforced before anything reaches the wire.
        """
        return self._privileged

    @property
    def base_url(self) -> str:
        """The resolved sidecar base URL this client talks to."""
        return self._base_url

    async def aclose(self) -> None:
        """Close the underlying httpx client, releasing pooled connections."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
        """Submit a flag to the sidecar's ``/submit`` endpoint.

        Supervisor-only (AGENTS.md invariant 5). The wire payload is the
        bounded ``{"challenge_id", "flag"}`` pair; the response body is
        normalized into the internal :class:`SubmissionResult` schema
        (:func:`_normalize_submission`) — every observed accept/reject
        form maps deterministically.

        Raises:
            HalPrivilegeError: If this client is not privileged.
            HalServiceError: On provider failure (after bounded retries)
                or on an unparseable response payload.
        """
        self._require_privileged("submit_flag")
        request = self._client.build_request(
            "POST", SUBMIT_PATH, json={"challenge_id": challenge_id, "flag": flag}
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await self._client.send(request)
            except httpx.TransportError as exc:
                if attempts <= self._max_retries:
                    await self._backoff(attempts)
                    continue
                error = HalServiceError(
                    provider=PROVIDER,
                    status_code=None,
                    retryable=True,
                    message=f"sidecar transport failure after {attempts} attempt(s): {exc}",
                )
                self._record_failure(status_code=None, attempts=attempts)
                raise error from exc
            if response.status_code >= 400:
                retryable = response.status_code == 429 or response.status_code >= 500
                detail = self._provider_error_message(response)
                await response.aclose()
                if retryable and attempts <= self._max_retries:
                    await self._backoff(attempts)
                    continue
                error = HalServiceError(
                    provider=PROVIDER,
                    status_code=response.status_code,
                    retryable=retryable,
                    message=f"sidecar returned HTTP {response.status_code}: {detail}",
                )
                self._record_failure(status_code=response.status_code, attempts=attempts)
                raise error from None
            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                await response.aclose()
                raise self._parse_error(attempts, f"malformed JSON body: {exc}") from exc
            await response.aclose()
            if not isinstance(body, dict):
                raise self._parse_error(
                    attempts, f"sidecar response must be an object, got {type(body).__name__}"
                )
            try:
                return _normalize_submission(challenge_id, body, attempts)
            except HalServiceError:
                self._record_failure(status_code=200, attempts=attempts)
                raise

    async def done(self, *, run_id: str = "", reason: str = "") -> None:
        """Signal the end of the run to the sidecar's ``/done`` endpoint.

        Supervisor-only (AGENTS.md invariant 5: only the supervisor may
        exit the run) and BEST-EFFORT (HAL-004): the call fires once and
        NEVER raises — a /done failure must not fail the run. A
        ``sidecar.done`` event is recorded on success and a
        ``sidecar.done_failed`` event on any failure (transport error,
        non-2xx), and the failure is swallowed. The payload is bounded
        (``run_id`` / ``reason``, both optional) — never secrets.

        Raises:
            HalPrivilegeError: If this client is not privileged.
        """
        self._require_privileged("done")
        payload: dict[str, str] = {}
        if run_id != "":
            payload["run_id"] = run_id
        if reason != "":
            payload["reason"] = reason
        request = self._client.build_request("POST", DONE_PATH, json=payload)
        try:
            response = await self._client.send(request)
        except httpx.TransportError as exc:
            self._record_done_failure(status_code=None, message=f"transport failure: {exc}")
            return
        try:
            if response.status_code >= 400:
                detail = self._provider_error_message(response)
                self._record_done_failure(
                    status_code=response.status_code,
                    message=f"sidecar returned HTTP {response.status_code}: {detail}",
                )
                return
            self._record_done(payload)
        finally:
            await response.aclose()

    def _require_privileged(self, method: str) -> None:
        """Raise :class:`HalPrivilegeError` for non-privileged callers."""
        if not self._privileged:
            raise HalPrivilegeError(
                f"{method} is supervisor-only; construct SidecarSubmissionClient "
                "with privileged=True"
            )

    async def _backoff(self, attempts: int) -> None:
        """Sleep for the exponential backoff delay of ``attempts``."""
        delay = min(BASE_BACKOFF_S * (2 ** (attempts - 1)), MAX_BACKOFF_S)
        await self._sleeper(delay)

    @staticmethod
    def _provider_error_message(response: httpx.Response) -> str:
        """Best-effort extraction of the provider's error detail."""
        try:
            payload = response.json()
        except json.JSONDecodeError:
            text = response.text.strip()
            return text[:300] if text else "no detail"
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:300]
        text = response.text.strip()
        return text[:300] if text else "no detail"

    def _parse_error(self, attempts: int, message: str) -> HalServiceError:
        """Build and record a non-retryable response-parse failure."""
        error = _parse_error(attempts, message)
        self._record_failure(status_code=200, attempts=attempts)
        return error

    def _record_failure(self, *, status_code: int | None, attempts: int) -> None:
        """Append a ``sidecar.failure`` event when an event log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=SIDECAR_FAILURE_EVENT,
                producer=PRODUCER,
                payload={
                    "provider": PROVIDER,
                    "status": status_code,
                    "attempts": attempts,
                },
            )
        )

    def _record_done(self, payload: dict[str, str]) -> None:
        """Append a ``sidecar.done`` event when an event log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=SIDECAR_DONE_EVENT,
                producer=PRODUCER,
                payload=dict(payload),
            )
        )

    def _record_done_failure(self, *, status_code: int | None, message: str) -> None:
        """Append a ``sidecar.done_failed`` event when an event log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=SIDECAR_DONE_FAILED_EVENT,
                producer=PRODUCER,
                payload={"provider": PROVIDER, "status": status_code, "message": message},
            )
        )
