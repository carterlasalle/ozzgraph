"""HalCTF MCP integration client for OzzGraph (PR6).

Implements the HalCTF Integration contract from
docs/API_AND_INTEGRATIONS.md: a local terminal-native adapter surface
(``halctl``) wrapped around a JSON-RPC 2.0 MCP wire protocol. Models never
call raw MCP (AGENTS.md invariant 5) — :class:`HalClient` is the only
surface, and privileged operations (flag submission, paid hints, graceful
exit) are guarded so only a supervisor-constructed client can invoke them.

Wire protocol (JSON-RPC 2.0, ``POST {base_url}``):

==================  =====================  ===================================
Method              Params                 Result
==================  =====================  ===================================
``ctf.list``        *(none)*               ``CtfList`` object
``challenge.list``  ``ctf_id`` (optional)  ``ChallengeList`` object
``challenge.get``   ``challenge_id``       ``Challenge`` object
``challenge.status`` ``challenge_id``      ``ChallengeStatus`` object
``flag.submit``     ``challenge_id``,      ``SubmissionResult`` object
                    ``flag``
``hint.request``    ``challenge_id``,      ``HintResult`` object
                    ``index``
``scoreboard.get``  *(none)*               ``{"entries": [...]}``
``exit``            ``reason``             ``null`` or empty object
==================  =====================  ===================================

The official HalCTF MCP tool set (V09, docs/CHANGES_v2.md milestone 9)
is exposed as ``list_ctfs``, ``list_challenges`` (the ``challenges``
tool), ``get_status`` (``status``), ``submit_flag``, ``request_hint``,
and ``get_scoreboard`` (``scoreboard``) — see
:data:`OFFICIAL_HALCTF_TOOLS`. Every upstream response is normalized
into an internal versioned schema (Contract Versioning): unknown
upstream fields are dropped and the remaining payload is validated into
a :class:`Challenge` / :class:`ChallengeStatus` / :class:`SubmissionResult`
/ :class:`HintResult` / :class:`Scoreboard` / :class:`CtfList` /
:class:`ChallengeList` model carrying ``schema_version``. Upstream
renames are absorbed inside the normalizer (``model_validate``) without
leaking into the codebase.

Configuration is constructor-injected with environment fallback (no secrets
or settings enter the ``OzzGraphConfig`` model): the MCP endpoint is
resolved with the deterministic V09 discovery
(:func:`ozzgraph.config.discover_halctf_endpoint` — first non-blank of
``OZZGRAPH_MCP_BASE_URL`` / ``HAL_MCP_ENDPOINT`` / ``HAL_ENDPOINT`` /
``MCP_ENDPOINT`` / ``OPENAI_BASE_URL``, falling back to the localhost
default for standalone ``halctl`` use), plus ``OZZGRAPH_MCP_TIMEOUT_S``,
``OZZGRAPH_MCP_MAX_RETRIES``, and ``OZZGRAPH_HAL_PRIVILEGED``.

Failure policy mirrors the model client (docs/API_AND_INTEGRATIONS.md,
"Integration Failure Policy"): bounded exponential-backoff retries on
transient failures only — HTTP 429, HTTP >= 500, JSON-RPC internal errors
(``-32603``), and httpx transport errors. Other 4xx statuses
(400/401/403/404/422) and application JSON-RPC errors never retry. Every
terminal failure is raised as a single typed :class:`HalServiceError`
carrying ``provider``/``status_code``/``retryable``/``message``, and a
``hal_failure`` event (producer ``hal_client``) is appended to the event log
when one is provided. Retries are always bounded (no infinite retry).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ozzgraph.config import discover_halctf_endpoint
from ozzgraph.events import Event, EventLog

# Provider identity used in normalized errors and failure events.
PROVIDER = "halctf"
PRODUCER = "hal_client"
HAL_FAILURE_EVENT = "hal_failure"

#: The official HalCTF MCP tool set (V09, docs/CHANGES_v2.md milestone
#: 9), keyed by tool name -> :class:`HalClient` method. Every platform
#: tool has exactly one client surface; models reach it only through the
#: kernel's adapters (``halctl`` / the environment services), never raw
#: MCP (AGENTS.md invariant 5).
OFFICIAL_HALCTF_TOOLS: dict[str, str] = {
    "list_ctfs": "list_ctfs",
    "challenges": "list_challenges",
    "status": "get_status",
    "submit_flag": "submit_flag",
    "request_hint": "request_hint",
    "scoreboard": "get_scoreboard",
}

# Internal contract version (Contract Versioning). Bump only via forward-only
# migrations; every normalized model carries this version.
SCHEMA_VERSION = 1

MCP_BASE_URL_ENV = "OZZGRAPH_MCP_BASE_URL"
MCP_TIMEOUT_ENV = "OZZGRAPH_MCP_TIMEOUT_S"
MCP_MAX_RETRIES_ENV = "OZZGRAPH_MCP_MAX_RETRIES"
HAL_PRIVILEGED_ENV = "OZZGRAPH_HAL_PRIVILEGED"

# Base URL includes the MCP endpoint path (mirrors OZZGRAPH_MODEL_BASE_URL's
# "base URL including the API prefix" semantics).
DEFAULT_MCP_BASE_URL = "http://127.0.0.1:9000/mcp"
DEFAULT_MCP_TIMEOUT_S = 60.0
DEFAULT_MCP_MAX_RETRIES = 3
# Hard upper bound: retries are always bounded (no infinite retry).
MAX_RETRY_LIMIT = 10
# Exponential backoff: base * 2 ** (attempt - 1), capped at this ceiling.
BASE_BACKOFF_S = 0.5
MAX_BACKOFF_S = 8.0

# JSON-RPC 2.0 error codes.
RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603

Sleeper = Callable[[float], Awaitable[None]]


class Challenge(BaseModel):
    """Internal v1 schema for one HalCTF challenge (contract-versioned)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    id: str
    title: str
    description: str
    category: str
    points: int = Field(ge=0)
    solved: bool = False
    hint_count: int = Field(default=0, ge=0)
    files: list[str] = Field(default_factory=list)


class ChallengeStatus(BaseModel):
    """Internal v1 schema for a challenge's live status.

    V09 (v2/halctf-adapter): the status carries the smoke-flag signal
    (``smoke_flag`` — whether the smoke-test flag was accepted) and the
    deterministic scoring breakdown (``scoring``), so the environment
    can wire smoke-flag and scoring data without extra calls.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    challenge_id: str
    solved: bool
    attempts: int = Field(ge=0)
    hints_used: int = Field(ge=0)
    points_earned: int = Field(ge=0)
    smoke_flag: bool = False
    scoring: Scoring | None = None
    updated_at: str


class Scoring(BaseModel):
    """Internal v1 schema for a challenge's deterministic scoring breakdown.

    Attributes:
        max_points: The challenge's maximum (full-solve) point value.
        solves: Number of teams that already solved the challenge.
        first_blood: Whether this team was the first to solve.
        hint_penalty: Points deducted per paid hint (the deterministic
            hint cost the HintPolicy layer budgets against).
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    max_points: int = Field(ge=0)
    solves: int = Field(ge=0)
    first_blood: bool = False
    hint_penalty: int = Field(default=0, ge=0)


class SubmissionResult(BaseModel):
    """Internal v1 schema for one flag submission verdict."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    challenge_id: str
    accepted: bool
    message: str
    points: int = Field(ge=0)
    attempts_remaining: int | None = None


class HintResult(BaseModel):
    """Internal v1 schema for one hint request verdict.

    V09: ``cost`` carries the hint's price in points when the platform
    reports one (None when unknown) — the deterministic hint cost the
    paid-hint gate budgets against.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    challenge_id: str
    index: int = Field(ge=0)
    hint: str
    paid: bool
    cost: int | None = Field(default=None, ge=0)


class ScoreboardEntry(BaseModel):
    """Internal v1 schema for one scoreboard row."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    rank: int = Field(ge=1)
    user_id: str
    points: int = Field(ge=0)
    solved: int = Field(ge=0)


class Scoreboard(BaseModel):
    """Internal v1 schema for the full scoreboard."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    entries: list[ScoreboardEntry] = Field(default_factory=list)


class Ctf(BaseModel):
    """Internal v1 schema for one HalCTF competition (``ctf.list``)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    id: str
    name: str
    description: str = ""
    challenge_count: int = Field(default=0, ge=0)
    solved: int = Field(default=0, ge=0)
    points: int = Field(default=0, ge=0)


class CtfList(BaseModel):
    """Internal v1 schema for the ``ctf.list`` result."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    ctfs: list[Ctf] = Field(default_factory=list)


class ChallengeList(BaseModel):
    """Internal v1 schema for the ``challenge.list`` result."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    challenges: list[Challenge] = Field(default_factory=list)


class HalServiceError(RuntimeError):
    """A normalized, typed failure from the HalCTF MCP service.

    Attributes:
        provider: The integration provider name (``"halctf"``).
        status_code: HTTP status of the failing response, or ``None`` for
            transport-level failures (timeouts, connection errors). JSON-RPC
            application errors surface with the HTTP status they rode in on
            (typically 200) and their code in ``message``.
        retryable: Whether the failure was transient (HTTP 429, HTTP >= 500,
            JSON-RPC ``-32603``, transport errors) and could succeed on a
            retry — not whether retries remain.
        message: Human-readable failure detail.
    """

    def __init__(
        self, *, provider: str, status_code: int | None, retryable: bool, message: str
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.message = message


class HalPrivilegeError(RuntimeError):
    """Raised when a non-privileged caller invokes a supervisor-only method.

    Flag submission, paid hint purchase, and graceful exit are privileged
    (AGENTS.md invariant 5); only a client constructed with
    ``privileged=True`` (supervisor) may call them.
    """


T = TypeVar("T", bound=BaseModel)


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


class HalClient:
    """HalCTF MCP client with bounded retry, timeouts, and privilege guards.

    Configuration is constructor-injected with environment fallback:

    ============================ ============================= ================
    Env var                      Default                       Meaning
    ============================ ============================= ================
    ``OZZGRAPH_MCP_BASE_URL``    ``http://127.0.0.1:9000/mcp`` Base URL incl.
                                                                 MCP endpoint
                                                                 path — the
                                                                 FIRST of the
                                                                 deterministic
                                                                 V09 discovery
                                                                 candidates
                                                                 (``OZZGRAPH_
                                                                 MCP_BASE_URL``,
                                                                 ``HAL_MCP_
                                                                 ENDPOINT``,
                                                                 ``HAL_ENDPOINT``,
                                                                 ``MCP_ENDPOINT``,
                                                                 ``OPENAI_BASE_URL``;
                                                                 first non-blank
                                                                 wins)
    ``OZZGRAPH_MCP_TIMEOUT_S``   ``60``                        Request
                                                                 timeout in
                                                                 seconds
    ``OZZGRAPH_MCP_MAX_RETRIES`` ``3``                         Transient
                                                                 retries
                                                                 (``0``
                                                                 disables,
                                                                 max 10)
    ``OZZGRAPH_HAL_PRIVILEGED``  *(unset)*                     Supervisor
                                                                 flag; only a
                                                                 privileged
                                                                 client may
                                                                 submit flags,
                                                                 buy paid
                                                                 hints, or
                                                                 exit
    ============================ ============================= ================

    Retry policy (docs/API_AND_INTEGRATIONS.md, "Integration Failure
    Policy"): bounded exponential backoff on transient failures only — HTTP
    429, HTTP >= 500, JSON-RPC ``-32603`` (internal error), and httpx
    transport errors. Other 4xx (400/401/403/404/422) and application
    JSON-RPC errors never retry. Every failure is raised as a typed
    :class:`HalServiceError`, and a ``hal_failure`` event is appended to the
    event log when one is configured.

    Privilege model (AGENTS.md invariant 5): ``submit_flag``,
    ``request_hint`` for paid hints (``index > 0`` — hint zero is free), and
    ``graceful_exit`` raise :class:`HalPrivilegeError` unless the client was
    constructed with ``privileged=True``.

    Args:
        base_url: MCP endpoint URL including the path prefix.
        timeout_s: Request timeout in seconds (> 0).
        max_retries: Retry count for transient failures; ``0`` disables
            retries; bounded to ``[0, MAX_RETRY_LIMIT]``.
        privileged: Whether this client may invoke supervisor-only methods;
            defaults to the ``OZZGRAPH_HAL_PRIVILEGED`` env var (false when
            unset).
        event_log: Optional append-only event log for ``hal_failure``
            events.
        run_id: Run identifier used to attribute failure events.
        transport: Optional httpx transport (tests inject
            ``httpx.MockTransport`` here).
        sleeper: Awaitable used for backoff sleeps; injectable for
            deterministic tests (defaults to ``asyncio.sleep``).
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
    ) -> None:
        env = os.environ
        # V09: the endpoint is resolved through the deterministic
        # discovery (first non-blank of OZZGRAPH_MCP_BASE_URL /
        # HAL_MCP_ENDPOINT / HAL_ENDPOINT / MCP_ENDPOINT /
        # OPENAI_BASE_URL — ozzgraph.config.discover_halctf_endpoint),
        # falling back to the localhost default for standalone halctl
        # use. The explicit constructor argument always wins.
        resolved_url = discover_halctf_endpoint(env) or DEFAULT_MCP_BASE_URL
        self._base_url = (resolved_url if base_url is None else base_url).rstrip("/")
        if not self._base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an http(s) URL, got {self._base_url!r}")

        resolved_timeout = _env_float(env, MCP_TIMEOUT_ENV, DEFAULT_MCP_TIMEOUT_S)
        timeout = resolved_timeout if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError(f"timeout_s must be > 0, got {timeout!r}")
        self._timeout_s = float(timeout)

        resolved_retries = _env_int(env, MCP_MAX_RETRIES_ENV, DEFAULT_MCP_MAX_RETRIES)
        retries = resolved_retries if max_retries is None else max_retries
        if retries < 0 or retries > MAX_RETRY_LIMIT:
            raise ValueError(f"max_retries must be in [0, {MAX_RETRY_LIMIT}], got {retries!r}")
        self._max_retries = retries

        resolved_privileged = _env_bool(env, HAL_PRIVILEGED_ENV, False)
        self._privileged = resolved_privileged if privileged is None else privileged

        self._event_log = event_log
        self._run_id = run_id
        self._sleeper = sleeper
        self._rpc_id = 0

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout_s),
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client, releasing pooled connections."""
        await self._client.aclose()

    @property
    def privileged(self) -> bool:
        """Whether this client may invoke supervisor-only methods.

        The supervisor-only submission coordinator (PR22) checks this
        flag before calling :meth:`submit_flag`, so the privilege
        boundary is enforced before anything reaches the wire.
        """
        return self._privileged

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def list_ctfs(self) -> CtfList:
        """List the available HalCTF competitions (``ctf.list``).

        The ``list_ctfs`` tool of the official HalCTF MCP tool set (V09).
        Read-only; open to any caller.

        Raises:
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        result, attempts = await self._call_rpc("ctf.list", {})
        return self._parse_result("ctf.list", result, CtfList, attempts)

    async def list_challenges(self, ctf_id: str | None = None) -> ChallengeList:
        """List the challenges of a competition (``challenge.list``).

        The ``challenges`` tool of the official HalCTF MCP tool set
        (V09). ``ctf_id`` narrows the listing to one competition when
        provided; without it the platform's default/active CTF is
        listed. Read-only; open to any caller.

        Raises:
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        params: dict[str, object] = {}
        if ctf_id is not None and ctf_id.strip() != "":
            params["ctf_id"] = ctf_id.strip()
        result, attempts = await self._call_rpc("challenge.list", params)
        return self._parse_result("challenge.list", result, ChallengeList, attempts)

    async def get_challenge(self, challenge_id: str) -> Challenge:
        """Retrieve the challenge's normalized details (``challenge.get``).

        Raises:
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        result, attempts = await self._call_rpc("challenge.get", {"challenge_id": challenge_id})
        return self._parse_result("challenge.get", result, Challenge, attempts)

    async def get_status(self, challenge_id: str) -> ChallengeStatus:
        """Retrieve the challenge's live status (``challenge.status``).

        Raises:
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        result, attempts = await self._call_rpc("challenge.status", {"challenge_id": challenge_id})
        return self._parse_result("challenge.status", result, ChallengeStatus, attempts)

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
        """Submit a flag for the challenge (``flag.submit``).

        Supervisor-only (AGENTS.md invariant 5).

        Raises:
            HalPrivilegeError: If this client is not privileged.
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        self._require_privileged("submit_flag")
        result, attempts = await self._call_rpc(
            "flag.submit", {"challenge_id": challenge_id, "flag": flag}
        )
        return self._parse_result("flag.submit", result, SubmissionResult, attempts)

    async def request_hint(self, challenge_id: str, index: int) -> HintResult:
        """Request a hint by index (``hint.request``).

        Hint zero is free and open to any caller; paid hints (``index > 0``)
        are supervisor-only (AGENTS.md invariant 5).

        Raises:
            HalPrivilegeError: If ``index > 0`` and this client is not
                privileged.
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        if index > 0:
            self._require_privileged("request_hint")
        result, attempts = await self._call_rpc(
            "hint.request", {"challenge_id": challenge_id, "index": index}
        )
        return self._parse_result("hint.request", result, HintResult, attempts)

    async def get_scoreboard(self) -> Scoreboard:
        """Retrieve the competition scoreboard (``scoreboard.get``).

        Read-only extension required by the ``halctl scoreboard`` subcommand
        (docs/API_AND_INTEGRATIONS.md, HalCTF Integration).

        Raises:
            HalServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        result, attempts = await self._call_rpc("scoreboard.get", {})
        return self._parse_result("scoreboard.get", result, Scoreboard, attempts)

    async def graceful_exit(self, reason: str) -> None:
        """Signal a clean, deliberate end of the run (``exit``).

        Supervisor-only (AGENTS.md invariant 5). The MCP result is not
        normalized (nothing is returned).

        Raises:
            HalPrivilegeError: If this client is not privileged.
            HalServiceError: On provider failure (after bounded retries).
        """
        self._require_privileged("graceful_exit")
        await self._call_rpc("exit", {"reason": reason})

    def _require_privileged(self, method: str) -> None:
        """Raise :class:`HalPrivilegeError` for non-privileged callers."""
        if not self._privileged:
            raise HalPrivilegeError(
                f"{method} is supervisor-only; construct HalClient with privileged=True"
            )

    async def _call_rpc(self, method: str, params: Mapping[str, object]) -> tuple[object, int]:
        """Send one JSON-RPC 2.0 request, retrying transient failures.

        Retryable (transient) failures are HTTP 429, HTTP >= 500, JSON-RPC
        ``-32603`` (internal error), and httpx transport errors. All other
        4xx statuses and application JSON-RPC errors fail immediately.
        Retries are bounded by ``max_retries``; ``0`` disables them. Every
        terminal failure raises :class:`HalServiceError` and appends a
        ``hal_failure`` event when an event log is configured.

        Returns:
            The JSON-RPC ``result`` value and the number of attempts that
            were made.
        """
        rpc_id = self._next_id()
        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": dict(params),
        }
        request = self._client.build_request("POST", "", json=payload)
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
                    message=f"transport failure after {attempts} attempt(s): {exc}",
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
                    message=f"provider returned HTTP {response.status_code}: {detail}",
                )
                self._record_failure(status_code=response.status_code, attempts=attempts)
                raise error from None
            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                await response.aclose()
                raise self._parse_error(
                    response.status_code, attempts, f"malformed JSON body: {exc}"
                ) from exc
            await response.aclose()
            if not isinstance(body, dict):
                raise self._parse_error(
                    response.status_code,
                    attempts,
                    f"JSON-RPC response must be an object, got {type(body).__name__}",
                )
            if "error" in body:
                parts = self._rpc_error_parts(body["error"])
                if parts is None:
                    raise self._parse_error(
                        response.status_code, attempts, "malformed JSON-RPC error object"
                    )
                code, message = parts
                retryable = code == RPC_INTERNAL_ERROR
                if retryable and attempts <= self._max_retries:
                    await self._backoff(attempts)
                    continue
                error = HalServiceError(
                    provider=PROVIDER,
                    status_code=response.status_code,
                    retryable=retryable,
                    message=f"JSON-RPC error {code}: {message}",
                )
                self._record_failure(status_code=response.status_code, attempts=attempts)
                raise error from None
            if "result" not in body:
                raise self._parse_error(
                    response.status_code, attempts, "JSON-RPC response has neither result nor error"
                )
            response_id = body.get("id")
            if response_id != rpc_id:
                raise self._parse_error(
                    response.status_code,
                    attempts,
                    f"JSON-RPC response id {response_id!r} does not match request id {rpc_id!r}",
                )
            return body["result"], attempts

    def _next_id(self) -> int:
        """Bump and return the per-client JSON-RPC request id."""
        self._rpc_id += 1
        return self._rpc_id

    def _parse_result(self, method: str, result: object, model: type[T], attempts: int) -> T:
        """Normalize a JSON-RPC result into the internal versioned schema.

        Unknown upstream fields are ignored; a non-object result or a
        payload that fails schema validation fails loudly as a
        non-retryable :class:`HalServiceError`.
        """
        if not isinstance(result, dict):
            raise self._parse_error(
                200, attempts, f"{method} result must be an object, got {type(result).__name__}"
            )
        try:
            return model.model_validate(result)
        except ValidationError as exc:
            raise self._parse_error(200, attempts, f"invalid {method} result: {exc}") from exc

    @staticmethod
    def _rpc_error_parts(rpc_error: object) -> tuple[int, str] | None:
        """Extract ``(code, message)`` from a JSON-RPC error object."""
        if not isinstance(rpc_error, dict):
            return None
        code = rpc_error.get("code")
        message = rpc_error.get("message")
        if not isinstance(code, int):
            return None
        if isinstance(message, str) and message:
            return code, message
        return code, f"JSON-RPC error code {code}"

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

    def _parse_error(self, status_code: int, attempts: int, message: str) -> HalServiceError:
        """Build and record a non-retryable response-parse failure."""
        error = HalServiceError(
            provider=PROVIDER,
            status_code=status_code,
            retryable=False,
            message=f"unparseable provider response (HTTP {status_code}): {message}",
        )
        self._record_failure(status_code=status_code, attempts=attempts)
        return error

    def _record_failure(self, *, status_code: int | None, attempts: int) -> None:
        """Append a ``hal_failure`` event when an event log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=HAL_FAILURE_EVENT,
                producer=PRODUCER,
                payload={
                    "provider": PROVIDER,
                    "status": status_code,
                    "attempts": attempts,
                },
            )
        )
