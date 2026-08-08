"""Tests for the supervisor-only submission coordinator (PR22).

Covers the docs/TECHNICAL_REQUIREMENTS.md "Flag Submission" contract:
only the supervisor may submit (a non-privileged client is refused
before the wire), the verified candidate's observed provenance is
validated (MissingRequiredStateError mirrors the phase router), attempt
budgets are enforced per candidate and in total (budget-style), an
accepted submission persists the entity + SUBMISSION SUBMITS
FLAG_CANDIDATE edge and routes DONE, a rejection marks the candidate
rejected (the flag is never re-submitted; V01, docs/adr/0008: the
kernel no longer routes on flag candidates — no VERIFY_AND_SUBMIT /
FLAG_HUNT — so the graph replans) and raises a typed
SubmissionRejectedError, and every mutation is replay-consistent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ozzgraph.config import DEFAULT_MAX_SUBMISSIONS
from ozzgraph.environments.halctf import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
    ENTITY_FLAG_CANDIDATE,
    ENTITY_OBSERVATION,
    FIELD_ATTEMPTS,
    FIELD_FLAG,
    FIELD_REJECTED,
    FIELD_VERIFIED,
    SubmissionCoordinator,
    SubmissionLimitError,
    SubmissionPrivilegeError,
    SubmissionRejectedError,
    SubmissionStateError,
    flag_candidate_id,
)
from ozzgraph.events import (
    SUBMISSION_ACCEPTED,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_REJECTED,
    EventLog,
)
from ozzgraph.hal_client import HalClient, SubmissionResult
from ozzgraph.phases import Phase
from ozzgraph.router import (
    EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
    ENTITY_SUBMISSION,
    MissingRequiredStateError,
    PhaseRouter,
)
from ozzgraph.state_graph import StateGraph

FLAG = "flag{submit-me-4242}"
CHALLENGE = "web-01"

ACCEPTED = SubmissionResult(challenge_id=CHALLENGE, accepted=True, message="Correct!", points=100)
REJECTED = SubmissionResult(challenge_id=CHALLENGE, accepted=False, message="Wrong flag", points=0)


class FakeSubmissionClient:
    """A scripted privileged submit surface (records every call)."""

    def __init__(self, *, privileged: bool = True, result: SubmissionResult | None = None) -> None:
        self._privileged = privileged
        self.result = result
        self.calls: list[tuple[str, str]] = []

    @property
    def privileged(self) -> bool:
        return self._privileged

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
        self.calls.append((challenge_id, flag))
        if self.result is None:
            raise AssertionError("fake client scripted without a result")
        return self.result


def _coordinator(
    client: FakeSubmissionClient | HalClient,
    *,
    event_log: EventLog | None = None,
    max_submissions: int = DEFAULT_MAX_SUBMISSIONS,
) -> SubmissionCoordinator:
    return SubmissionCoordinator(
        client=client,
        run_id="run-1",
        challenge_id=CHALLENGE,
        event_log=event_log,
        max_submissions=max_submissions,
    )


async def _seed_verified_candidate(
    graph: StateGraph,
    *,
    flag: str = FLAG,
    with_edge: bool = True,
    rejected: bool = False,
    attempts: int = 0,
) -> str:
    """Seed observation + evidence + a verified flag candidate."""
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
            FIELD_REJECTED: rejected,
            FIELD_ATTEMPTS: attempts,
        },
    )
    if with_edge:
        await graph.create_edge(
            f"{candidate_id}-observed-in-evidence-1",
            EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
            candidate_id,
            "evidence-1",
        )
    return candidate_id


# ---------------------------------------------------------------------------
# accepted submissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_submission_persists_entity_edge_and_routes_done() -> None:
    """An accepted submission persists entity + edge; the router routes DONE."""
    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph)
        client = FakeSubmissionClient(result=ACCEPTED)
        result = await _coordinator(client).submit_verified_candidate(graph)

        assert result.accepted is True
        assert client.calls == [(CHALLENGE, FLAG)]

        submissions = await graph.list_entities(ENTITY_SUBMISSION)
        assert len(submissions) == 1
        submission = submissions[0]
        assert submission.id == "submission-1"
        assert submission.data["challenge_id"] == CHALLENGE
        assert submission.data["flag"] == FLAG
        assert submission.data["accepted"] is True
        assert submission.data["message"] == "Correct!"
        assert submission.data["points"] == 100
        assert submission.data["candidate_id"] == candidate_id

        neighbors = await graph.neighbors(submission.id, EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE)
        assert any(
            edge.src_id == submission.id and edge.dst_id == candidate_id
            for edge in neighbors.outgoing
        )

        route = await PhaseRouter().route(graph)
        assert route.phase == Phase.DONE
        assert route.predicate == "has_accepted_submission"


@pytest.mark.asyncio
async def test_accepted_submission_keeps_candidate_verified() -> None:
    """Acceptance never marks the candidate rejected (it was correct)."""
    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph)
        await _coordinator(FakeSubmissionClient(result=ACCEPTED)).submit_verified_candidate(graph)

        record = await graph.get_entity(candidate_id)
        assert record is not None
        assert record.data[FIELD_REJECTED] is False
        assert record.data[FIELD_ATTEMPTS] == 0


# ---------------------------------------------------------------------------
# rejected submissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_submission_marks_candidate_and_reroutes() -> None:
    """A rejection marks the candidate rejected; the generic kernel replans."""
    async with StateGraph(":memory:") as graph:
        # Baseline: recon + enumeration complete, explored access.
        await graph.create_entity("tgt-1", "target", {"confirmed": True})
        await graph.create_entity("svc-1", "service", {"characterized": True})
        await graph.create_entity("cred-1", "credential", {"valid": True, "explored": True})
        candidate_id = await _seed_verified_candidate(graph)
        client = FakeSubmissionClient(result=REJECTED)

        with pytest.raises(SubmissionRejectedError) as exc_info:
            await _coordinator(client).submit_verified_candidate(graph)

        assert exc_info.value.message == "Wrong flag"
        assert exc_info.value.flag == FLAG
        assert exc_info.value.candidate_id == candidate_id
        assert client.calls == [(CHALLENGE, FLAG)]

        record = await graph.get_entity(candidate_id)
        assert record is not None
        assert record.data[FIELD_REJECTED] is True
        assert record.data[FIELD_ATTEMPTS] == 1

        # V01 (docs/adr/0008): the kernel no longer routes on flag
        # candidates — no VERIFY_AND_SUBMIT, no FLAG_HUNT; the graph
        # replans, and the flag is never re-submitted.
        route = await PhaseRouter().route(graph)
        assert route.phase == Phase.REPLAN
        assert route.predicate == "default_replan"
        assert route.phase.value not in ("FLAG_HUNT", "VERIFY_AND_SUBMIT")


@pytest.mark.asyncio
async def test_rejected_flag_is_never_resubmitted() -> None:
    """A rejected candidate is not even found as a submission target."""
    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph, rejected=True, attempts=1)
        client = FakeSubmissionClient(result=ACCEPTED)

        with pytest.raises(MissingRequiredStateError, match="no verified flag candidate"):
            await _coordinator(client).submit_verified_candidate(graph)

        assert client.calls == []
        record = await graph.get_entity(candidate_id)
        assert record is not None
        assert record.data[FIELD_REJECTED] is True


# ---------------------------------------------------------------------------
# supervisor-only and budgets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_privileged_client_raises_before_wire() -> None:
    """Only a privileged client may submit; the wire is never reached."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        client = FakeSubmissionClient(privileged=False, result=ACCEPTED)

        with pytest.raises(SubmissionPrivilegeError, match="supervisor-only"):
            await _coordinator(client).submit_verified_candidate(graph)

        assert client.calls == []


@pytest.mark.asyncio
async def test_per_candidate_attempt_limit_raises() -> None:
    """A candidate at its attempt budget is refused (budget-style)."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph, attempts=3)
        client = FakeSubmissionClient(result=ACCEPTED)

        with pytest.raises(SubmissionLimitError, match="candidate"):
            await _coordinator(client, max_submissions=3).submit_verified_candidate(graph)

        assert client.calls == []


@pytest.mark.asyncio
async def test_total_submission_budget_raises() -> None:
    """The run-wide submission cap is enforced before the wire call."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        for index in range(3):
            await graph.create_entity(
                f"submission-{index + 1}", ENTITY_SUBMISSION, {"accepted": False}
            )
        client = FakeSubmissionClient(result=ACCEPTED)

        with pytest.raises(SubmissionLimitError, match="total"):
            await _coordinator(client, max_submissions=3).submit_verified_candidate(graph)

        assert client.calls == []


@pytest.mark.asyncio
async def test_wrong_typed_attempts_field_fails_loudly() -> None:
    """A corrupt attempts field is a SubmissionStateError, never coerced."""
    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph)
        await graph.update_entity(
            candidate_id, {FIELD_FLAG: FLAG, FIELD_VERIFIED: True, FIELD_ATTEMPTS: "many"}
        )
        client = FakeSubmissionClient(result=ACCEPTED)

        with pytest.raises(SubmissionStateError, match="attempts"):
            await _coordinator(client).submit_verified_candidate(graph)

        assert client.calls == []


# ---------------------------------------------------------------------------
# provenance validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verified_candidate_without_provenance_raises() -> None:
    """A verified candidate lacking its evidence edge is refused loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph, with_edge=False)
        client = FakeSubmissionClient(result=ACCEPTED)

        with pytest.raises(MissingRequiredStateError, match="OBSERVED_IN EVIDENCE"):
            await _coordinator(client).submit_verified_candidate(graph)

        assert client.calls == []


@pytest.mark.asyncio
async def test_no_verified_candidate_raises() -> None:
    """A graph with nothing verified to submit is refused loudly."""
    async with StateGraph(":memory:") as graph:
        client = FakeSubmissionClient(result=ACCEPTED)
        with pytest.raises(MissingRequiredStateError, match="no verified flag candidate"):
            await _coordinator(client).submit_verified_candidate(graph)
        assert client.calls == []


@pytest.mark.asyncio
async def test_rejected_candidate_is_not_a_submission_target() -> None:
    """A rejected candidate is never picked up as the verified candidate."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph, rejected=True, attempts=1)
        client = FakeSubmissionClient(result=ACCEPTED)
        with pytest.raises(MissingRequiredStateError, match="no verified flag candidate"):
            await _coordinator(client).submit_verified_candidate(graph)
        assert client.calls == []


# ---------------------------------------------------------------------------
# events and replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submission_events_attempted_then_verdict(tmp_path: Path) -> None:
    """The attempt is recorded before the verdict events (step-10 pattern)."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph)
        await _coordinator(
            FakeSubmissionClient(result=ACCEPTED), event_log=log
        ).submit_verified_candidate(graph)

    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types.index(SUBMISSION_ATTEMPTED) < event_types.index(SUBMISSION_ACCEPTED)
    assert SUBMISSION_REJECTED not in event_types
    attempted = next(event for event in events if event["event_type"] == SUBMISSION_ATTEMPTED)
    assert attempted["payload"]["candidate_id"] == candidate_id
    assert attempted["payload"]["flag_sha256"] == hashlib.sha256(FLAG.encode()).hexdigest()
    assert attempted["payload"]["flag_length"] == len(FLAG)
    assert "flag" not in attempted["payload"]  # FLAGLEAK-001: no raw flag in run-only events
    accepted = next(event for event in events if event["event_type"] == SUBMISSION_ACCEPTED)
    assert accepted["payload"]["accepted"] is True


@pytest.mark.asyncio
async def test_rejection_events_and_updated_marker(tmp_path: Path) -> None:
    """A rejection emits submission.rejected after submission.attempted."""
    log = EventLog.for_run(tmp_path)
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        with pytest.raises(SubmissionRejectedError):
            await _coordinator(
                FakeSubmissionClient(result=REJECTED), event_log=log
            ).submit_verified_candidate(graph)

    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types.index(SUBMISSION_ATTEMPTED) < event_types.index(SUBMISSION_REJECTED)
    assert "graph.entity_updated" in event_types  # the rejected marker
    rejected = next(event for event in events if event["event_type"] == SUBMISSION_REJECTED)
    assert rejected["payload"]["accepted"] is False
    assert rejected["payload"]["message"] == "Wrong flag"


def test_invalid_max_submissions_rejected() -> None:
    """max_submissions must be >= 1 (budget-style, never silently unbounded)."""
    with pytest.raises(ValueError):
        SubmissionCoordinator(
            client=FakeSubmissionClient(result=ACCEPTED),
            run_id="run-1",
            challenge_id=CHALLENGE,
            max_submissions=0,
        )
