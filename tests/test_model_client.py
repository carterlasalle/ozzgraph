"""Unit tests for the OpenAI-compatible model client (PR5)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ozzgraph.events import EventLog
from ozzgraph.model_client import (
    DEFAULT_MODEL_BASE_URL,
    MAX_RETRY_LIMIT,
    MODEL_API_KEY_ENV,
    MODEL_BASE_URL_ENV,
    MODEL_MAX_RETRIES_ENV,
    MODEL_TIMEOUT_ENV,
    ModelInfo,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelService,
    ModelServiceError,
    ModelStreamEvent,
    ModelUsage,
)


class _NoopSleeper:
    """Backoff sleeper that returns immediately (deterministic tests)."""

    async def __call__(self, _: float) -> None:
        return None


def _service(handler: Any, **kwargs: Any) -> ModelService:
    """Build a ModelService on a MockTransport with a no-op backoff sleeper."""
    return ModelService(transport=httpx.MockTransport(handler), sleeper=_NoopSleeper(), **kwargs)


def _chat_response(
    *, usage: dict[str, int] | None = None, content: str = "hello"
) -> dict[str, object]:
    """A well-formed OpenAI-compatible chat completion response."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def _request() -> ModelRequest:
    """A minimal chat completion request."""
    return ModelRequest(model="test-model", messages=[ModelMessage(role="user", content="ping")])


def test_list_models_success() -> None:
    """GET /models returns normalized ModelInfo entries."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "model-a", "object": "model", "created": 1, "owned_by": "ozz"},
                    {"id": "model-b", "object": "model", "created": 2, "owned_by": "ozz"},
                ],
            },
        )

    service = _service(handler)

    async def _run() -> list[ModelInfo]:
        async with service:
            return await service.list_models()

    models = asyncio.run(_run())
    assert [m.id for m in models] == ["model-a", "model-b"]
    assert [m.owned_by for m in models] == ["ozz", "ozz"]


def test_complete_success_with_usage() -> None:
    """POST /chat/completions returns a normalized response with usage."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_response())

    service = _service(handler)

    async def _run() -> ModelResponse:
        async with service:
            return await service.complete(_request())

    response = asyncio.run(_run())
    assert response.id == "chatcmpl-1"
    assert response.model == "test-model"
    assert response.created == 1780000000
    assert len(response.choices) == 1
    assert response.choices[0].message.role == "assistant"
    assert response.choices[0].message.content == "hello"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage == ModelUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7)
    # Non-streaming payload: stream forced off, unset optionals omitted.
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["messages"] == [{"role": "user", "content": "ping"}]
    assert "temperature" not in captured["body"]
    assert "max_tokens" not in captured["body"]


def test_complete_passes_response_format_through() -> None:
    """response_format (structured output) is passed through untouched."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_response(content='{"ok": true}'))

    service = _service(handler)
    request = ModelRequest(
        model="test-model",
        messages=[ModelMessage(role="user", content="json please")],
        response_format={"type": "json_object"},
    )

    async def _run() -> ModelResponse:
        async with service:
            return await service.complete(request)

    response = asyncio.run(_run())
    assert response.choices[0].message.content == '{"ok": true}'
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_streaming_yields_deltas_and_final_usage() -> None:
    """stream_complete yields content deltas and the provider's usage."""
    captured: dict[str, Any] = {}
    sse = (
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
        '"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=sse.encode(), headers={"content-type": "text/event-stream"}
        )

    service = _service(handler)

    async def _run() -> list[ModelStreamEvent]:
        async with service:
            return [event async for event in service.stream_complete(_request())]

    events = asyncio.run(_run())
    deltas = [e.delta for e in events if e.delta is not None]
    assert deltas == ["Hello", " world"]
    usages = [e.usage for e in events if e.usage is not None]
    assert usages == [ModelUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13)]
    assert captured["body"]["stream"] is True


def test_streaming_without_usage_yields_only_deltas() -> None:
    """Streams without a usage chunk still yield deltas (usage optional)."""
    sse = (
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"a"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode())

    service = _service(handler)

    async def _run() -> list[ModelStreamEvent]:
        async with service:
            return [event async for event in service.stream_complete(_request())]

    events = asyncio.run(_run())
    assert [e.delta for e in events if e.delta is not None] == ["a", "b"]
    assert all(e.usage is None for e in events)


def test_retry_then_success_on_503() -> None:
    """A transient 503 is retried with backoff, then succeeds."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json=_chat_response())

    service = _service(handler)

    async def _run() -> ModelResponse:
        async with service:
            return await service.complete(_request())

    response = asyncio.run(_run())
    assert response.model == "test-model"
    assert calls == 3


def test_timeout_raises_model_service_error() -> None:
    """Transport timeouts are retried, then raised as ModelServiceError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    service = _service(handler)

    async def _run() -> None:
        async with service:
            await service.complete(_request())

    with pytest.raises(ModelServiceError) as excinfo:
        asyncio.run(_run())
    error = excinfo.value
    assert error.retryable is True
    assert error.status_code is None
    assert error.provider == "openai-compatible"
    assert "transport failure" in error.message


def test_401_is_non_retryable_and_does_not_retry() -> None:
    """A 401 fails immediately with a non-retryable error and one attempt."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    service = _service(handler)

    async def _run() -> None:
        async with service:
            await service.complete(_request())

    with pytest.raises(ModelServiceError) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 401
    assert excinfo.value.retryable is False
    assert "bad key" in excinfo.value.message
    assert calls == 1


def test_max_retries_zero_disables_retry() -> None:
    """max_retries=0 disables retries: one attempt, immediate typed error."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    service = _service(handler, max_retries=0)

    async def _run() -> None:
        async with service:
            await service.complete(_request())

    with pytest.raises(ModelServiceError) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 503
    assert excinfo.value.retryable is True
    assert calls == 1


def test_missing_usage_raises_typed_error() -> None:
    """A response without token usage fails loudly with a typed error."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = _chat_response()
        del body["usage"]
        return httpx.Response(200, json=body)

    service = _service(handler)

    async def _run() -> None:
        async with service:
            await service.complete(_request())

    with pytest.raises(ModelServiceError) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 200
    assert excinfo.value.retryable is False
    assert "unparseable" in excinfo.value.message
    assert "usage" in excinfo.value.message
    assert calls == 1


def test_failure_event_emitted_after_exhausted_retries(tmp_path: Path) -> None:
    """Exhausted retries append a model_failure event to the event log."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {"message": "boom"}})

    log = EventLog(tmp_path / "actions.jsonl")
    service = _service(handler, event_log=log, run_id="run-7")

    async def _run() -> None:
        async with service:
            await service.complete(_request())

    with pytest.raises(ModelServiceError):
        asyncio.run(_run())

    records = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    event = records[0]
    assert event["event_type"] == "model_failure"
    assert event["producer"] == "model_client"
    assert event["run_id"] == "run-7"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["provider"] == "openai-compatible"
    assert payload["status"] == 500
    assert payload["attempts"] == 4  # 1 initial attempt + 3 retries
    assert calls == 4


def test_api_key_sent_as_bearer_header() -> None:
    """An API key is sent as an Authorization bearer header."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_chat_response())

    service = _service(handler, api_key="dummy-bearer-token")

    async def _run() -> None:
        async with service:
            await service.complete(_request())

    asyncio.run(_run())
    assert captured["auth"] == "Bearer dummy-bearer-token"


def test_constructor_defaults_without_env(monkeypatch: Any) -> None:
    """Defaults apply when no env vars are set."""
    for name in (
        MODEL_BASE_URL_ENV,
        MODEL_API_KEY_ENV,
        MODEL_TIMEOUT_ENV,
        MODEL_MAX_RETRIES_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    service = ModelService()
    assert service._base_url == DEFAULT_MODEL_BASE_URL
    assert service._timeout_s == 60.0
    assert service._max_retries == 3
    assert service._api_key is None
    asyncio.run(service.aclose())


def test_constructor_defaults_read_from_env(monkeypatch: Any) -> None:
    """Env vars drive the constructor defaults."""
    monkeypatch.setenv(MODEL_BASE_URL_ENV, "https://llm.example.com/v1/")
    monkeypatch.setenv(MODEL_API_KEY_ENV, "dummy-bearer-token")
    monkeypatch.setenv(MODEL_TIMEOUT_ENV, "7.5")
    monkeypatch.setenv(MODEL_MAX_RETRIES_ENV, "2")

    service = ModelService()
    assert service._base_url == "https://llm.example.com/v1"
    assert service._timeout_s == 7.5
    assert service._max_retries == 2
    assert service._api_key == "dummy-bearer-token"
    asyncio.run(service.aclose())


def test_invalid_env_value_fails_loudly(monkeypatch: Any) -> None:
    """A non-numeric timeout env var fails loudly at construction."""
    monkeypatch.setenv(MODEL_TIMEOUT_ENV, "soon")
    with pytest.raises(ValueError, match="OZZGRAPH_MODEL_TIMEOUT_S"):
        ModelService()


def test_constructor_rejects_invalid_configuration() -> None:
    """Bogus base URLs, timeouts, and retry counts fail loudly."""
    with pytest.raises(ValueError):
        ModelService(base_url="ftp://nope")
    with pytest.raises(ValueError):
        ModelService(timeout_s=0)
    with pytest.raises(ValueError):
        ModelService(timeout_s=-1.0)
    with pytest.raises(ValueError):
        ModelService(max_retries=-1)
    with pytest.raises(ValueError):
        ModelService(max_retries=MAX_RETRY_LIMIT + 1)


def test_request_models_validate() -> None:
    """Request/response models enforce their pydantic v2 contracts."""
    request = ModelRequest(model="m", messages=[ModelMessage(role="user", content="hi")])
    assert request.stream is False
    assert request.temperature is None
    assert request.response_format is None
    with pytest.raises(ValidationError):
        ModelRequest(model="m", messages=[])
    with pytest.raises(ValidationError):
        ModelUsage(prompt_tokens=-1, completion_tokens=0, total_tokens=-1)
    with pytest.raises(ValidationError):
        # Missing usage and empty choices both fail loudly.
        ModelResponse.model_validate(
            {"id": "x", "model": "m", "choices": [], "usage": {}, "created": 1}
        )
