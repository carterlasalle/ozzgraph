"""OpenAI-compatible model client for OzzGraph (PR5).

Implements the OpenAI-compatible model service contract from
docs/API_AND_INTEGRATIONS.md: ``GET /v1/models`` and ``POST /v1/chat/completions``
over httpx, with streaming and non-streaming responses, bounded
exponential-backoff retries, request timeouts, token usage extraction,
structured-output passthrough, and provider error normalization into a single
typed :class:`ModelServiceError`.

Configuration is constructor-injected with environment fallback so no secrets
or model settings leak into the ``OzzGraphConfig`` model:
``OZZGRAPH_MODEL_BASE_URL``, ``OZZGRAPH_MODEL_API_KEY``,
``OZZGRAPH_MODEL_TIMEOUT_S``, ``OZZGRAPH_MODEL_MAX_RETRIES``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, Field, ValidationError

from ozzgraph.events import Event, EventLog

# Provider identity used in normalized errors and failure events.
PROVIDER = "openai-compatible"
PRODUCER = "model_client"
MODEL_FAILURE_EVENT = "model_failure"

MODEL_BASE_URL_ENV = "OZZGRAPH_MODEL_BASE_URL"
MODEL_API_KEY_ENV = "OZZGRAPH_MODEL_API_KEY"
MODEL_TIMEOUT_ENV = "OZZGRAPH_MODEL_TIMEOUT_S"
MODEL_MAX_RETRIES_ENV = "OZZGRAPH_MODEL_MAX_RETRIES"

DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL_TIMEOUT_S = 60.0
DEFAULT_MODEL_MAX_RETRIES = 3
# Hard upper bound: retries are always bounded (no infinite retry).
MAX_RETRY_LIMIT = 10
# Exponential backoff: base * 2 ** (attempt - 1), capped at this ceiling.
BASE_BACKOFF_S = 0.5
MAX_BACKOFF_S = 8.0

Sleeper = Callable[[float], Awaitable[None]]


class ModelInfo(BaseModel):
    """A model advertised by the provider's ``GET /models`` endpoint."""

    id: str
    owned_by: str


class ModelUsage(BaseModel):
    """Token accounting for one completion."""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ModelMessage(BaseModel):
    """One chat message in the normalized request/response schema."""

    role: str
    content: str | None = None


class ModelChoice(BaseModel):
    """One completion choice: an index, a message, and a finish reason."""

    index: int = 0
    message: ModelMessage
    finish_reason: str | None = None


class ModelRequest(BaseModel):
    """A chat completion request in the normalized schema.

    Attributes:
        model: The model identifier to call.
        messages: The conversation, oldest first; at least one message.
        temperature: Sampling temperature; omitted from the payload when
            unset.
        max_tokens: Output token cap; omitted when unset.
        stream: Whether to request a streaming response. ``complete`` and
            ``stream_complete`` force their own value regardless.
        response_format: Structured-output hint (e.g. ``{"type":
            "json_object"}``), passed through untouched when set.
    """

    model: str
    messages: list[ModelMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    response_format: dict[str, object] | None = None


class ModelResponse(BaseModel):
    """A normalized non-streaming chat completion response."""

    id: str
    model: str
    choices: list[ModelChoice] = Field(min_length=1)
    usage: ModelUsage
    created: int = Field(ge=0)


class ModelStreamEvent(BaseModel):
    """One item yielded by :meth:`ModelService.stream_complete`.

    Either ``delta`` (incremental content) or ``usage`` (final token
    accounting, when the provider returns it) is set; never both.
    """

    delta: str | None = None
    usage: ModelUsage | None = None


class ModelServiceError(RuntimeError):
    """A normalized, typed failure from the OpenAI-compatible service.

    Attributes:
        provider: The integration provider name (``"openai-compatible"`` for
            this client).
        status_code: HTTP status of the failing response, or ``None`` for
            transport-level failures (timeouts, connection errors).
        retryable: Whether the failure was transient (429, 5xx, transport
            errors) and could succeed on a retry — not whether retries
            remain.
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


class ModelService:
    """OpenAI-compatible model client with bounded retry and timeouts.

    Configuration is constructor-injected with environment fallback, keeping
    the kernel small and secrets out of the config model:

    ============================ =============================== ===========
    Env var                      Default                         Meaning
    ============================ =============================== ===========
    ``OZZGRAPH_MODEL_BASE_URL``   ``http://127.0.0.1:8000/v1``   Base URL
                                                                 incl. API
                                                                 prefix
    ``OZZGRAPH_MODEL_API_KEY``    *(unset)*                      Bearer
                                                                 token
    ``OZZGRAPH_MODEL_TIMEOUT_S``  ``60``                         Request
                                                                 timeout in
                                                                 seconds
    ``OZZGRAPH_MODEL_MAX_RETRIES`` ``3``                         Transient
                                                                 retries
                                                                 (``0``
                                                                 disables,
                                                                 max 10)
    ============================ =============================== ===========

    Retry policy (docs/API_AND_INTEGRATIONS.md, "Integration Failure
    Policy"): bounded exponential backoff on transient failures only — HTTP
    429, HTTP >= 500, and httpx transport errors (connection errors,
    connect/read/write timeouts). Other 4xx (400/401/403/404/422) never
    retry. Every failure is raised as a typed :class:`ModelServiceError`,
    and a ``model_failure`` event is appended to the event log when one is
    configured.

    Args:
        base_url: Base URL including the API prefix.
        api_key: Optional bearer token sent as ``Authorization: Bearer``.
        timeout_s: Request timeout in seconds (> 0).
        max_retries: Retry count for transient failures; ``0`` disables
            retries; bounded to ``[0, MAX_RETRY_LIMIT]``.
        event_log: Optional append-only event log for ``model_failure``
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
        api_key: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        event_log: EventLog | None = None,
        run_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        env = os.environ
        resolved_url = _env_str(env, MODEL_BASE_URL_ENV, DEFAULT_MODEL_BASE_URL)
        self._base_url = (resolved_url if base_url is None else base_url).rstrip("/")
        if not self._base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an http(s) URL, got {self._base_url!r}")

        resolved_key = env.get(MODEL_API_KEY_ENV)
        self._api_key = api_key if api_key is not None else resolved_key
        if self._api_key is not None and self._api_key.strip() == "":
            self._api_key = None

        resolved_timeout = _env_float(env, MODEL_TIMEOUT_ENV, DEFAULT_MODEL_TIMEOUT_S)
        timeout = resolved_timeout if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError(f"timeout_s must be > 0, got {timeout!r}")
        self._timeout_s = float(timeout)

        resolved_retries = _env_int(env, MODEL_MAX_RETRIES_ENV, DEFAULT_MODEL_MAX_RETRIES)
        retries = resolved_retries if max_retries is None else max_retries
        if retries < 0 or retries > MAX_RETRY_LIMIT:
            raise ValueError(f"max_retries must be in [0, {MAX_RETRY_LIMIT}], got {retries!r}")
        self._max_retries = retries

        self._event_log = event_log
        self._run_id = run_id
        self._sleeper = sleeper

        headers = (
            {"Authorization": f"Bearer {self._api_key}"} if self._api_key is not None else None
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout_s),
            headers=headers,
            transport=transport,
        )

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

    async def list_models(self) -> list[ModelInfo]:
        """List models advertised by the provider (``GET /models``).

        Raises:
            ModelServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload.
        """
        request = self._client.build_request("GET", "/models")
        response, attempts = await self._send_with_retry(request)
        try:
            data = response.json()
            return [ModelInfo.model_validate(item) for item in data["data"]]
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise self._parse_error(response.status_code, attempts, exc) from exc

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run a non-streaming chat completion (``POST /chat/completions``).

        The payload is normalized: ``None`` optionals are omitted and
        ``stream`` is forced to ``False`` regardless of the request field.
        ``response_format`` is passed through untouched when set.

        Raises:
            ModelServiceError: On provider failure (after bounded retries) or
                on an unparseable response payload — including missing or
                malformed token ``usage`` (fail loudly).
        """
        payload = request.model_dump(exclude_none=True)
        payload["stream"] = False
        http_request = self._client.build_request("POST", "/chat/completions", json=payload)
        response, attempts = await self._send_with_retry(http_request)
        try:
            data = response.json()
            return ModelResponse.model_validate(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise self._parse_error(response.status_code, attempts, exc) from exc

    async def stream_complete(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Stream a chat completion, yielding incremental content deltas.

        Design note: streaming is an async iterator of typed
        :class:`ModelStreamEvent` objects rather than an async context
        manager, so callers can ``async for`` directly and the iterator owns
        response acquisition and cleanup. Retries are bounded to the
        request-acquisition phase: once the response is open the stream is
        never re-sent, and mid-stream failures raise immediately.

        Yields:
            One event per non-empty content delta, plus a final event
            carrying ``usage`` when the provider includes it in the last
            chunk.
        """
        payload = request.model_dump(exclude_none=True)
        payload["stream"] = True
        http_request = self._client.build_request("POST", "/chat/completions", json=payload)
        response, attempts = await self._send_with_retry(http_request, stream=True)
        saw_data = False
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                saw_data = True
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise self._stream_error(
                        response.status_code,
                        attempts,
                        f"malformed SSE data: {data!r}",
                        retryable=False,
                    ) from exc
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content != "":
                        yield ModelStreamEvent(delta=content)
                usage = chunk.get("usage")
                if usage is not None:
                    try:
                        yield ModelStreamEvent(usage=ModelUsage.model_validate(usage))
                    except ValidationError as exc:
                        raise self._stream_error(
                            response.status_code,
                            attempts,
                            f"malformed usage in stream: {exc}",
                            retryable=False,
                        ) from exc
        except httpx.TransportError as exc:
            raise self._stream_error(
                response.status_code,
                attempts,
                f"stream interrupted: {exc}",
                retryable=True,
            ) from exc
        finally:
            await response.aclose()
        if not saw_data:
            raise self._stream_error(
                response.status_code,
                attempts,
                "stream contained no SSE data",
                retryable=False,
            )

    async def _send_with_retry(
        self, request: httpx.Request, *, stream: bool = False
    ) -> tuple[httpx.Response, int]:
        """Send ``request``, retrying transient failures with bounded backoff.

        Retryable (transient) failures are HTTP 429, HTTP >= 500, and httpx
        transport errors (connection errors, connect/read/write timeouts).
        All other 4xx statuses (400/401/403/404/422) fail immediately.
        Retries are bounded by ``max_retries``; ``0`` disables them. Every
        terminal failure raises :class:`ModelServiceError` and appends a
        ``model_failure`` event when an event log is configured.

        Returns:
            The successful response (HTTP < 400) and the number of HTTP
            attempts that were made.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await self._client.send(request, stream=stream)
            except httpx.TransportError as exc:
                if attempts <= self._max_retries:
                    await self._backoff(attempts)
                    continue
                error = ModelServiceError(
                    provider=PROVIDER,
                    status_code=None,
                    retryable=True,
                    message=f"transport failure after {attempts} attempt(s): {exc}",
                )
                self._record_failure(status_code=None, attempts=attempts)
                raise error from exc
            if response.status_code < 400:
                return response, attempts
            retryable = response.status_code == 429 or response.status_code >= 500
            detail = self._provider_error_message(response)
            await response.aclose()
            if retryable and attempts <= self._max_retries:
                await self._backoff(attempts)
                continue
            error = ModelServiceError(
                provider=PROVIDER,
                status_code=response.status_code,
                retryable=retryable,
                message=f"provider returned HTTP {response.status_code}: {detail}",
            )
            self._record_failure(status_code=response.status_code, attempts=attempts)
            raise error from None

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

    def _parse_error(self, status_code: int, attempts: int, cause: Exception) -> ModelServiceError:
        """Build and record a non-retryable response-parse failure."""
        error = ModelServiceError(
            provider=PROVIDER,
            status_code=status_code,
            retryable=False,
            message=f"unparseable provider response (HTTP {status_code}): {cause}",
        )
        self._record_failure(status_code=status_code, attempts=attempts)
        return error

    def _stream_error(
        self, status_code: int, attempts: int, message: str, *, retryable: bool
    ) -> ModelServiceError:
        """Build and record a streaming failure."""
        error = ModelServiceError(
            provider=PROVIDER,
            status_code=status_code,
            retryable=retryable,
            message=message,
        )
        self._record_failure(status_code=status_code, attempts=attempts)
        return error

    def _record_failure(self, *, status_code: int | None, attempts: int) -> None:
        """Append a ``model_failure`` event when an event log is configured."""
        if self._event_log is None:
            return
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=MODEL_FAILURE_EVENT,
                producer=PRODUCER,
                payload={
                    "provider": PROVIDER,
                    "status": status_code,
                    "attempts": attempts,
                },
            )
        )
