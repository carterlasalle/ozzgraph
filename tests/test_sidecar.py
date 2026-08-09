"""Tests for the plain-HTTP sidecar transport adapter (HAL-004).

Covers ``src/ozzgraph/environments/halctf/sidecar.py``: the
:class:`SidecarSubmissionClient` normalizes every observed sidecar
``/submit`` response form into the INTERNAL
:class:`~ozzgraph.hal_client.SubmissionResult` schema (unchanged —
``challenge_id`` / ``accepted`` / ``message`` / ``points`` /
``attempts_remaining``), retries bounded on transient failures only,
raises typed :class:`~ozzgraph.hal_client.HalServiceError` failures,
enforces the supervisor-only privilege boundary before the wire, and
treats ``/done`` as best-effort (never raises; events recorded). The
coordinator integration test proves the adapter satisfies the
:class:`~ozzgraph.environments.halctf.submissions.SubmissionClient`
protocol end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ozzgraph.environments.halctf import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
    ENTITY_FLAG_CANDIDATE,
    ENTITY_OBSERVATION,
    FIELD_ATTEMPTS,
    FIELD_FLAG,
    FIELD_REJECTED,
    FIELD_VERIFIED,
    SIDECAR_DONE_EVENT,
    SIDECAR_DONE_FAILED_EVENT,
    SIDECAR_FAILURE_EVENT,
    SidecarSubmissionClient,
    SubmissionCoordinator,
    SubmissionRejectedError,
    discover_halctf_sidecar_base_url,
    flag_candidate_id,
)
from ozzgraph.events import EventLog
from ozzgraph.hal_client import HalPrivilegeError, HalServiceError, SubmissionResult
from ozzgraph.router import ENTITY_SUBMISSION
from ozzgraph.state_graph import StateGraph

FLAG = "flag{sidecar-4242}"
CHALLENGE = "web-01"


class _NoopSleeper:
    """Backoff sleeper that returns immediately (deterministic retry tests)."""

    async def __call__(self, _: float) -> None:
        return None


def _client(
    handler: Any,
    *,
    privileged: bool = True,
    event_log: EventLog | None = None,
    max_retries: int = 3,
) -> SidecarSubmissionClient:
    """A sidecar client on a MockTransport with no-op backoff and no env deps."""
    return SidecarSubmissionClient(
        base_url="http://sidecar.test",
        privileged=privileged,
        event_log=event_log,
        run_id="run-1",
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
        sleeper=_NoopSleeper(),
        environ={},
    )


def _read_events(log: EventLog) -> list[dict[str, Any]]:
    """The JSON event objects appended to ``log`` so far."""
    return [json.loads(line) for line in log.path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# accept shapes — every ACCEPT_STATUSES member with points_awarded > 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["correct", "accepted", "solved", "success", "already_solved"])
async def test_every_accept_status_with_points(status: str) -> None:
    """Each accepted status string normalizes to an accepted verdict.

    The wire payload is the bounded ``{"challenge_id", "flag"}`` pair and
    the response's ``points_awarded`` maps into ``SubmissionResult.points``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/submit"
        assert json.loads(request.content) == {"challenge_id": CHALLENGE, "flag": FLAG}
        return httpx.Response(200, json={"status": status, "points_awarded": 5})

    async with _client(handler) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result == SubmissionResult(
        challenge_id=CHALLENGE, accepted=True, message=status, points=5
    )


@pytest.mark.asyncio
async def test_already_solved_accepts_even_with_zero_points() -> None:
    """``already_solved`` is an accept even when the platform grants 0 points."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "already_solved", "points_awarded": 0})

    async with _client(handler) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result.accepted is True
    assert result.points == 0


@pytest.mark.asyncio
async def test_status_is_case_and_whitespace_insensitive() -> None:
    """The status string is stripped + casefolded before the accept check."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "  CORRECT  ", "points_awarded": 1})

    async with _client(handler) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result.accepted is True


@pytest.mark.asyncio
async def test_platform_message_points_and_attempts_aliases_map() -> None:
    """The normalizer absorbs upstream renames (msg/points/attempts_left)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"msg": "Good job!", "points": 3, "attempts_left": 4},
        )

    async with _client(handler) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result == SubmissionResult(
        challenge_id=CHALLENGE, accepted=True, message="Good job!", points=3, attempts_remaining=4
    )


# ---------------------------------------------------------------------------
# reject shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"status": "wrong"},
        {"status": "incorrect"},
        {"points_awarded": 0},
        {"points": 0},
        {},  # absent points and no verdict fields
        {"accepted": False},
        {"success": False},
        {"points": -5},  # negative points clamp to 0 -> rejected
    ],
)
async def test_reject_shapes(body: dict[str, Any]) -> None:
    """Every non-accept response form normalizes to a rejected verdict."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result.accepted is False
    assert result.points == 0


# ---------------------------------------------------------------------------
# precedence — the platform's explicit verdict wins deterministically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_string_wins_over_conflicting_boolean() -> None:
    """A string ``status`` decides; a conflicting bool field is ignored."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "correct", "accepted": False})

    async with _client(handler) as client:
        assert (await client.submit_flag(CHALLENGE, FLAG)).accepted is True


@pytest.mark.asyncio
async def test_reject_status_wins_over_accepting_points() -> None:
    """A non-accept ``status`` decides; the platform's points still map through."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "wrong", "points_awarded": 1})

    async with _client(handler) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result.accepted is False
    assert result.points == 1


@pytest.mark.asyncio
async def test_reject_status_wins_over_accepting_boolean() -> None:
    """A non-accept ``status`` decides even when a bool field says accepted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "wrong", "success": True})

    async with _client(handler) as client:
        assert (await client.submit_flag(CHALLENGE, FLAG)).accepted is False


@pytest.mark.asyncio
async def test_boolean_field_wins_over_points_without_status() -> None:
    """Without ``status``, the first present bool verdict field decides."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": True, "points_awarded": 0})

    async with _client(handler) as client:
        assert (await client.submit_flag(CHALLENGE, FLAG)).accepted is True


@pytest.mark.asyncio
async def test_first_present_boolean_field_wins() -> None:
    """Precedence order: accepted, success, solved, correct — first present."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": False, "success": True})

    async with _client(handler) as client:
        assert (await client.submit_flag(CHALLENGE, FLAG)).accepted is False


# ---------------------------------------------------------------------------
# typed failures — never coerced, retries bounded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_typed_status_raises_non_retryable_service_error() -> None:
    """A non-string ``status`` fails loudly (never coerced)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 42})

    async with _client(handler) as client:
        with pytest.raises(HalServiceError) as exc_info:
            await client.submit_flag(CHALLENGE, FLAG)

    assert exc_info.value.status_code == 200
    assert exc_info.value.retryable is False
    assert "status" in exc_info.value.message


@pytest.mark.asyncio
async def test_wrong_typed_boolean_field_raises_non_retryable_service_error() -> None:
    """A verdict bool field typed as a string fails loudly (never coerced)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": "yes"})

    async with _client(handler) as client:
        with pytest.raises(HalServiceError) as exc_info:
            await client.submit_flag(CHALLENGE, FLAG)

    assert exc_info.value.retryable is False
    assert "bool" in exc_info.value.message


@pytest.mark.asyncio
async def test_malformed_json_body_raises_unparseable() -> None:
    """A 200 with a non-JSON body is an unparseable-response failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    async with _client(handler) as client:
        with pytest.raises(HalServiceError) as exc_info:
            await client.submit_flag(CHALLENGE, FLAG)

    assert exc_info.value.status_code == 200
    assert exc_info.value.retryable is False
    assert "unparseable" in exc_info.value.message


@pytest.mark.asyncio
async def test_non_object_body_raises_unparseable() -> None:
    """A JSON body that is not an object is an unparseable-response failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    async with _client(handler) as client:
        with pytest.raises(HalServiceError, match="must be an object"):
            await client.submit_flag(CHALLENGE, FLAG)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 429])
async def test_retries_transient_failure_then_succeeds(status_code: int) -> None:
    """A 5xx / 429 is retried (bounded) and a later success is returned."""

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"status": "correct", "points_awarded": 1})

    async with _client(handler, max_retries=3) as client:
        result = await client.submit_flag(CHALLENGE, FLAG)

    assert result.accepted is True
    assert calls == 2


@pytest.mark.asyncio
async def test_transient_failure_after_max_retries_raises_retryable() -> None:
    """Retries are bounded: an exhausted 503 raises a retryable error."""

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={})

    async with _client(handler, max_retries=2) as client:
        with pytest.raises(HalServiceError) as exc_info:
            await client.submit_flag(CHALLENGE, FLAG)

    assert calls == 3  # 1 initial attempt + 2 retries
    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_transport_error_after_max_retries_raises_retryable() -> None:
    """A transport error is retried, then raises with status_code None."""

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused")

    async with _client(handler, max_retries=1) as client:
        with pytest.raises(HalServiceError) as exc_info:
            await client.submit_flag(CHALLENGE, FLAG)

    assert calls == 2
    assert exc_info.value.status_code is None
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_other_4xx_is_not_retried() -> None:
    """A non-429 4xx is a terminal, non-retryable failure (no retries)."""

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with _client(handler, max_retries=3) as client:
        with pytest.raises(HalServiceError) as exc_info:
            await client.submit_flag(CHALLENGE, FLAG)

    assert calls == 1
    assert exc_info.value.status_code == 404
    assert exc_info.value.retryable is False
    assert "not found" in exc_info.value.message


# ---------------------------------------------------------------------------
# supervisor-only privilege boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_privileged_submit_raises_before_wire() -> None:
    """submit_flag is refused for a non-privileged client; the wire is never hit."""

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"status": "correct"})

    async with _client(handler, privileged=False) as client:
        with pytest.raises(HalPrivilegeError, match="supervisor-only"):
            await client.submit_flag(CHALLENGE, FLAG)

    assert called is False


@pytest.mark.asyncio
async def test_non_privileged_done_raises_before_wire() -> None:
    """done is supervisor-only too; the wire is never hit."""

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    async with _client(handler, privileged=False) as client:
        with pytest.raises(HalPrivilegeError, match="supervisor-only"):
            await client.done(run_id="run-1")

    assert called is False


# ---------------------------------------------------------------------------
# /done — best-effort, never raises, events recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_success_records_event(tmp_path: Path) -> None:
    """A 2xx /done records a sidecar.done event with the bounded payload."""

    log = EventLog.for_run(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/done"
        assert json.loads(request.content) == {"run_id": "run-1", "reason": "completed"}
        return httpx.Response(200, json={"ok": True})

    async with _client(handler, event_log=log) as client:
        await client.done(run_id="run-1", reason="completed")

    events = _read_events(log)
    assert [event["event_type"] for event in events] == [SIDECAR_DONE_EVENT]
    assert events[0]["payload"] == {"run_id": "run-1", "reason": "completed"}


@pytest.mark.asyncio
async def test_done_transport_error_is_best_effort(tmp_path: Path) -> None:
    """A /done transport failure never raises; a done_failed event is recorded."""

    log = EventLog.for_run(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar unreachable")

    async with _client(handler, event_log=log) as client:
        await client.done(run_id="run-1")  # must not raise

    events = _read_events(log)
    assert [event["event_type"] for event in events] == [SIDECAR_DONE_FAILED_EVENT]
    assert events[0]["payload"]["status"] is None
    assert "transport" in events[0]["payload"]["message"]


@pytest.mark.asyncio
async def test_done_http_error_is_best_effort(tmp_path: Path) -> None:
    """A non-2xx /done never raises; a done_failed event carries the status."""

    log = EventLog.for_run(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "oops"}})

    async with _client(handler, event_log=log) as client:
        await client.done()  # must not raise

    events = _read_events(log)
    assert [event["event_type"] for event in events] == [SIDECAR_DONE_FAILED_EVENT]
    assert events[0]["payload"]["status"] == 500
    assert "oops" in events[0]["payload"]["message"]


# ---------------------------------------------------------------------------
# failure events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_submit_failure_records_sidecar_failure_event(
    tmp_path: Path,
) -> None:
    """A terminal /submit failure appends a sidecar.failure event."""

    log = EventLog.for_run(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 42})

    async with _client(handler, event_log=log) as client:
        with pytest.raises(HalServiceError):
            await client.submit_flag(CHALLENGE, FLAG)

    events = _read_events(log)
    assert [event["event_type"] for event in events] == [SIDECAR_FAILURE_EVENT]
    assert events[0]["payload"]["status"] == 200
    assert events[0]["payload"]["provider"] == "halctf"


# ---------------------------------------------------------------------------
# base URL discovery — env-first, deterministic, injectable
# ---------------------------------------------------------------------------


def test_discovery_explicit_env_wins() -> None:
    """OZZGRAPH_SIDECAR_BASE_URL wins over the MCP origin and the default."""
    assert (
        discover_halctf_sidecar_base_url({"OZZGRAPH_SIDECAR_BASE_URL": "http://sidecar:1234"})
        == "http://sidecar:1234"
    )


def test_discovery_falls_back_to_mcp_endpoint_origin() -> None:
    """Without an explicit var, the MCP endpoint's origin is used."""
    assert (
        discover_halctf_sidecar_base_url({"MCP_ENDPOINT": "http://127.0.0.1:9000/mcp"})
        == "http://127.0.0.1:9000"
    )


def test_discovery_defaults_to_localhost() -> None:
    """With nothing set, the localhost default is used (standalone use)."""
    assert discover_halctf_sidecar_base_url({}) == "http://127.0.0.1:9000"


def test_invalid_base_url_rejected() -> None:
    """A non-http(s) base URL fails loudly at construction."""
    with pytest.raises(ValueError, match="http"):
        SidecarSubmissionClient(base_url="sidecar:9000", environ={})


# ---------------------------------------------------------------------------
# coordinator integration — the SubmissionClient protocol end-to-end
# ---------------------------------------------------------------------------


async def _seed_verified_candidate(graph: StateGraph, *, flag: str = FLAG) -> str:
    """Seed observation + evidence + a verified flag candidate (PR22 pattern)."""
    await graph.create_entity("obs-1", ENTITY_OBSERVATION, {"summary": "observed"})
    await graph.create_entity("evidence-1", "evidence", {"note": "extracted"})
    await graph.create_edge(
        "evidence-1-extracted-obs-1",
        EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
        "evidence-1",
        "obs-1",
    )
    candidate_id = flag_candidate_id(flag)
    await graph.create_entity(
        candidate_id,
        ENTITY_FLAG_CANDIDATE,
        {
            FIELD_FLAG: flag,
            FIELD_VERIFIED: True,
            "source_observation_id": "obs-1",
            "evidence_ids": ["evidence-1"],
            FIELD_REJECTED: False,
            FIELD_ATTEMPTS: 0,
        },
    )
    await graph.create_edge(
        f"{candidate_id}-observed-in-evidence-1",
        EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
        candidate_id,
        "evidence-1",
    )
    return candidate_id


@pytest.mark.asyncio
async def test_coordinator_drives_sidecar_client_end_to_end() -> None:
    """SubmissionCoordinator + SidecarSubmissionClient: protocol satisfied.

    The sidecar adapter satisfies the SubmissionClient protocol
    (``privileged`` / ``submit_flag`` / ``aclose``), so the supervisor-only
    coordinator drives it unchanged, and the SubmissionResult it returns
    flows through unmodified (challenge_id / accepted / message / points /
    attempts_remaining — the internal schema is untouched).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/submit"
        assert json.loads(request.content) == {"challenge_id": CHALLENGE, "flag": FLAG}
        return httpx.Response(
            200,
            json={
                "status": "correct",
                "points_awarded": 50,
                "message": "Nice!",
                "attempts_remaining": 9,
            },
        )

    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        async with _client(handler) as client:
            coordinator = SubmissionCoordinator(
                client=client, run_id="run-1", challenge_id=CHALLENGE
            )
            result = await coordinator.submit_verified_candidate(graph)

        assert result == SubmissionResult(
            challenge_id=CHALLENGE,
            accepted=True,
            message="Nice!",
            points=50,
            attempts_remaining=9,
        )

        # The coordinator persisted the verdict from the sidecar transport.
        submissions = await graph.list_entities(ENTITY_SUBMISSION)
        assert len(submissions) == 1
        submission = submissions[0]
        assert submission.data["challenge_id"] == CHALLENGE
        assert submission.data["flag"] == FLAG
        assert submission.data["accepted"] is True
        assert submission.data["message"] == "Nice!"
        assert submission.data["points"] == 50


@pytest.mark.asyncio
async def test_coordinator_rejects_through_sidecar_client() -> None:
    """A platform rejection flows through the coordinator as a rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "wrong", "message": "Nope"})

    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph)
        async with _client(handler) as client:
            coordinator = SubmissionCoordinator(
                client=client, run_id="run-1", challenge_id=CHALLENGE
            )
            with pytest.raises(SubmissionRejectedError, match="Nope"):
                await coordinator.submit_verified_candidate(graph)

        # The rejection marked the candidate, so the flag is never re-submitted.
        record = await graph.get_entity(candidate_id)
        assert record is not None
        assert record.data[FIELD_REJECTED] is True
        assert record.data[FIELD_ATTEMPTS] == 1
