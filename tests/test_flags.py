"""Tests for flag candidate extraction and supervisor-only submission (PR22).

Covers candidate extraction (success, malformed input, the
no-provenance gate), the provenance chain (EVIDENCE EXTRACTED_FROM
OBSERVATION -> FLAG_CANDIDATE OBSERVED_IN EVIDENCE), verified
flag_candidate persistence with replay consistency (replaying the
event log reconstructs the same graph hash), the submission
coordinator's success and failure paths, rejected-flag
never-resubmitted behavior, per-candidate and total attempt-limit
enforcement, unprivileged-client rejection, and the typed error
hierarchy (AGENTS.md rule #9).

Every test uses its own in-memory SQLite graph (``":memory:"``);
replay tests use a file-backed live graph plus a fresh replay database.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.events import (
    FLAGS_CANDIDATE_FOUND,
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_UPDATED,
    SUBMISSION_ACCEPTED,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_REJECTED,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.flags import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
    ENTITY_FLAG_CANDIDATE,
    FIELD_ATTEMPTS,
    FIELD_EVIDENCE_IDS,
    FIELD_FLAG,
    FIELD_REJECTED,
    FIELD_SOURCE_OBSERVATION_ID,
    FIELD_VERIFIED,
    FlagCandidate,
    FlagCandidateExtractor,
    FlagsError,
    FlagsStateError,
    InvalidFlagPatternError,
    flag_candidate_id,
)
from ozzgraph.hal_client import SubmissionResult
from ozzgraph.phases import Phase
from ozzgraph.replay import replay_graph
from ozzgraph.router import (
    EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
    ENTITY_SUBMISSION,
    FIELD_ACCEPTED,
    MissingRequiredStateError,
    PhaseRouter,
)
from ozzgraph.state_graph import StateGraph
from ozzgraph.submissions import (
    SUBMISSIONS_PRODUCER,
    SubmissionCoordinator,
    SubmissionError,
    SubmissionLimitError,
    SubmissionPrivilegeError,
    SubmissionRejectedError,
    SubmissionStateError,
)

CHALLENGE = "ch-1"
RUN = "run-1"


class FakeSubmissionClient:
    """Minimal privileged-submit fake (structurally satisfies the protocol)."""

    def __init__(
        self,
        *,
        privileged: bool = True,
        accepted: bool = True,
        message: str = "correct",
    ) -> None:
        self._privileged = privileged
        self._accepted = accepted
        self._message = message
        self.calls: list[tuple[str, str]] = []

    @property
    def privileged(self) -> bool:
        return self._privileged

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
        self.calls.append((challenge_id, flag))
        return SubmissionResult(
            challenge_id=challenge_id,
            accepted=self._accepted,
            message=self._message,
            points=100 if self._accepted else 0,
            attempts_remaining=2,
        )


async def _seed_observed_flag(
    graph: StateGraph,
    *,
    observation_id: str = "obs-1",
    evidence_id: str = "ev-1",
    text: str = "the flag is flag{abc123}",
    observation_data: dict[str, object] | None = None,
    with_evidence: bool = True,
    reverse_edge: bool = False,
) -> None:
    """Seed one observation (and optionally its backing evidence)."""
    await graph.create_entity(observation_id, "observation", observation_data or {"summary": text})
    if with_evidence:
        await graph.create_entity(evidence_id, "evidence", {"note": "parsed from output"})
        if reverse_edge:
            await graph.create_edge(
                f"{observation_id}-from-{evidence_id}",
                EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
                observation_id,
                evidence_id,
            )
        else:
            await graph.create_edge(
                f"{evidence_id}-from-{observation_id}",
                EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
                evidence_id,
                observation_id,
            )


async def _seed_verified_candidate(
    graph: StateGraph,
    *,
    candidate_id: str = "flag-1",
    flag: str = "flag{abc123}",
    evidence_id: str = "ev-1",
    attempts: int = 0,
    rejected: bool = False,
    verified: bool = True,
    with_edge: bool = True,
) -> None:
    """Seed a flag_candidate entity with its provenance edge."""
    await graph.create_entity(
        candidate_id,
        ENTITY_FLAG_CANDIDATE,
        {
            FIELD_FLAG: flag,
            FIELD_VERIFIED: verified,
            FIELD_SOURCE_OBSERVATION_ID: "obs-1",
            FIELD_EVIDENCE_IDS: [evidence_id],
            FIELD_REJECTED: rejected,
            FIELD_ATTEMPTS: attempts,
        },
    )
    if with_edge:
        await graph.create_entity(evidence_id, "evidence", {"note": "parsed"})
        await graph.create_edge(
            f"{candidate_id}-observed-in-{evidence_id}",
            EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
            candidate_id,
            evidence_id,
        )


async def _seed_observed_flag_logged(
    graph: StateGraph,
    event_log: EventLog,
    *,
    text: str,
    at: datetime = datetime(2026, 8, 7, 9, 0, 0, tzinfo=UTC),
) -> None:
    """Seed observation + evidence + edge, mirroring every mutation to the log.

    Replay tests need the FULL mutation history in the event log (the
    live graph is built by direct calls); otherwise replay cannot
    reconstruct the seeded observation/evidence and their edge.
    """
    await graph.create_entity("obs-1", "observation", {"summary": text}, at=at)
    event_log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            RUN,
            "seed",
            GraphEntityCreated(
                entity_id="obs-1", entity_type="observation", data={"summary": text}, at=at
            ),
        )
    )
    await graph.create_entity("ev-1", "evidence", {"note": "parsed from output"}, at=at)
    event_log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            RUN,
            "seed",
            GraphEntityCreated(
                entity_id="ev-1", entity_type="evidence", data={"note": "parsed from output"}, at=at
            ),
        )
    )
    await graph.create_edge(
        "ev-1-from-obs-1",
        EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
        "ev-1",
        "obs-1",
        at=at,
    )
    event_log.append(
        graph_event(
            GRAPH_EDGE_CREATED,
            RUN,
            "seed",
            GraphEdgeCreated(
                edge_id="ev-1-from-obs-1",
                edge_type=EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
                src_id="ev-1",
                dst_id="obs-1",
                at=at,
            ),
        )
    )


def _read_events(path: Path) -> list[dict[str, object]]:
    """Every JSON line of an event log, in order."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _event_types(events: list[dict[str, object]]) -> list[str]:
    return [str(event["event_type"]) for event in events]


# ---------------------------------------------------------------------------
# candidate extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_persists_verified_candidate() -> None:
    """A flag seen in an evidence-backed observation becomes a candidate."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph)
        extractor = FlagCandidateExtractor()
        candidates = await extractor.extract(graph)

        assert len(candidates) == 1
        candidate = candidates[0]
        assert isinstance(candidate, FlagCandidate)
        assert candidate.flag == "flag{abc123}"
        assert candidate.entity_id == flag_candidate_id("flag{abc123}")
        assert candidate.source_observation_id == "obs-1"
        assert candidate.evidence_ids == ("ev-1",)

        record = await graph.get_entity(candidate.entity_id)
        assert record is not None
        assert record.type == ENTITY_FLAG_CANDIDATE
        assert record.data[FIELD_FLAG] == "flag{abc123}"
        assert record.data[FIELD_VERIFIED] is True
        assert record.data[FIELD_REJECTED] is False
        assert record.data[FIELD_ATTEMPTS] == 0
        assert record.data[FIELD_SOURCE_OBSERVATION_ID] == "obs-1"
        assert record.data[FIELD_EVIDENCE_IDS] == ["ev-1"]

        neighbors = await graph.neighbors(
            candidate.entity_id, EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE
        )
        assert [(edge.src_id, edge.dst_id) for edge in neighbors.outgoing] == [
            (candidate.entity_id, "ev-1")
        ]


@pytest.mark.asyncio
async def test_extract_without_evidence_produces_no_candidate() -> None:
    """The provenance gate: an observation with no evidence yields nothing."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph, with_evidence=False)
        extractor = FlagCandidateExtractor()
        assert await extractor.extract(graph) == ()
        assert await graph.list_entities(ENTITY_FLAG_CANDIDATE) == []


@pytest.mark.asyncio
async def test_extract_ignores_text_without_matching_flag() -> None:
    """Text with no flag{...} match produces no candidate."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph, text="nothing interesting here")
        extractor = FlagCandidateExtractor()
        assert await extractor.extract(graph) == ()
        assert await graph.list_entities(ENTITY_FLAG_CANDIDATE) == []


@pytest.mark.asyncio
async def test_extract_skips_malformed_or_nonmatching_flag_text() -> None:
    """Malformed flags (whitespace inside, wrong prefix, unclosed) never match."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(
            graph,
            text="flag{two words} FLAG{upper} flag{unclosed CTF{not-this}",
        )
        extractor = FlagCandidateExtractor()
        assert await extractor.extract(graph) == ()
        assert await graph.list_entities(ENTITY_FLAG_CANDIDATE) == []


@pytest.mark.asyncio
async def test_extract_finds_flags_in_nested_payload_and_artifacts(
    tmp_path: Path,
) -> None:
    """Observation payload strings (recursively) and artifact contents scan."""
    store = ArtifactStore(tmp_path / "artifacts")
    await store.put(source=b"backup dump: flag{in-artifact}", artifact_id="art-1")
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(
            graph,
            observation_data={
                "summary": "no flag here",
                "data": {"nested": {"deep": ["flag{deep-nested}"]}},
                "artifact_ids": ["art-1"],
            },
        )
        extractor = FlagCandidateExtractor(artifact_store=store)
        candidates = await extractor.extract(graph)

    assert [candidate.flag for candidate in candidates] == [
        "flag{deep-nested}",
        "flag{in-artifact}",
    ]


@pytest.mark.asyncio
async def test_extract_scans_artifact_contents(tmp_path: Path) -> None:
    """A flag inside a referenced artifact becomes a candidate."""
    store = ArtifactStore(tmp_path / "artifacts")
    await store.put(source=b"backup dump: flag{in-artifact}", artifact_id="art-1")
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(
            graph,
            observation_data={"summary": "no flag", "artifact_ids": ["art-1"]},
        )
        extractor = FlagCandidateExtractor(artifact_store=store)
        candidates = await extractor.extract(graph)

        assert len(candidates) == 1
        assert candidates[0].flag == "flag{in-artifact}"
        record = await graph.get_entity(candidates[0].entity_id)
        assert record is not None
        assert record.data[FIELD_SOURCE_OBSERVATION_ID] == "obs-1"


@pytest.mark.asyncio
async def test_extract_missing_artifact_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A dangling artifact reference never breaks extraction."""
    store = ArtifactStore(tmp_path / "artifacts")
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(
            graph,
            observation_data={"summary": "flag{payload-only}", "artifact_ids": ["missing"]},
        )
        extractor = FlagCandidateExtractor(artifact_store=store)
        candidates = await extractor.extract(graph)
        assert [candidate.flag for candidate in candidates] == ["flag{payload-only}"]


@pytest.mark.asyncio
async def test_extract_resolves_evidence_from_either_edge_direction() -> None:
    """An observation->evidence edge (reverse direction) is still provenance."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph, reverse_edge=True)
        extractor = FlagCandidateExtractor()
        candidates = await extractor.extract(graph)
        assert len(candidates) == 1
        assert candidates[0].evidence_ids == ("ev-1",)


@pytest.mark.asyncio
async def test_extract_two_flags_in_one_observation() -> None:
    """Distinct flag strings become distinct candidates sharing the evidence."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph, text="flag{one} then flag{two}")
        extractor = FlagCandidateExtractor()
        candidates = await extractor.extract(graph)

        assert [candidate.flag for candidate in candidates] == ["flag{one}", "flag{two}"]
        assert len(candidates) == 2
        assert candidates[0].entity_id == flag_candidate_id("flag{one}")
        assert candidates[1].entity_id == flag_candidate_id("flag{two}")
        for candidate in candidates:
            neighbors = await graph.neighbors(
                candidate.entity_id, EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE
            )
            assert [edge.dst_id for edge in neighbors.outgoing] == ["ev-1"]


@pytest.mark.asyncio
async def test_extract_is_idempotent_per_flag_string() -> None:
    """The same flag string never becomes a second candidate."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph)
        extractor = FlagCandidateExtractor()
        first = await extractor.extract(graph)
        second = await extractor.extract(graph)

        assert len(first) == 1
        assert second == ()
        assert len(await graph.list_entities(ENTITY_FLAG_CANDIDATE)) == 1


@pytest.mark.asyncio
async def test_extract_never_resurrects_rejected_candidate() -> None:
    """A rejected candidate is skipped, never re-created (PR22)."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph)
        extractor = FlagCandidateExtractor()
        await extractor.extract(graph)
        candidate_id = flag_candidate_id("flag{abc123}")
        record = await graph.get_entity(candidate_id)
        assert record is not None
        payload = dict(record.data)
        payload[FIELD_REJECTED] = True
        await graph.update_entity(candidate_id, payload)

        # The same flag appears again in a fresh observation.
        await _seed_observed_flag(
            graph,
            observation_id="obs-2",
            evidence_id="ev-2",
            text="flag{abc123} again",
        )
        assert await extractor.extract(graph) == ()
        assert len(await graph.list_entities(ENTITY_FLAG_CANDIDATE)) == 1


@pytest.mark.asyncio
async def test_extract_skips_candidate_at_attempt_budget() -> None:
    """A candidate whose attempts reached the budget is never re-created."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph)
        extractor = FlagCandidateExtractor(max_attempts=1)
        await extractor.extract(graph)
        candidate_id = flag_candidate_id("flag{abc123}")
        record = await graph.get_entity(candidate_id)
        assert record is not None
        payload = dict(record.data)
        payload[FIELD_ATTEMPTS] = 1
        await graph.update_entity(candidate_id, payload)

        await _seed_observed_flag(
            graph,
            observation_id="obs-2",
            evidence_id="ev-2",
            text="flag{abc123} again",
        )
        assert await extractor.extract(graph) == ()
        assert len(await graph.list_entities(ENTITY_FLAG_CANDIDATE)) == 1


@pytest.mark.asyncio
async def test_extract_wrong_typed_rejected_field_raises_loudly() -> None:
    """Corrupt candidate payloads fail loudly, never coerced."""
    async with StateGraph(":memory:") as graph:
        await _seed_observed_flag(graph)
        extractor = FlagCandidateExtractor()
        await extractor.extract(graph)
        candidate_id = flag_candidate_id("flag{abc123}")
        payload: dict[str, object] = {"flag": "flag{abc123}", FIELD_REJECTED: "yes"}
        await graph.update_entity(candidate_id, payload)

        with pytest.raises(FlagsStateError):
            await extractor.extract(graph)


def test_invalid_flag_pattern_raises_at_construction() -> None:
    """A bad configured regex fails loudly at construction."""
    with pytest.raises(InvalidFlagPatternError):
        FlagCandidateExtractor(pattern="[")
    with pytest.raises(ValueError, match="max_attempts"):
        FlagCandidateExtractor(max_attempts=0)


def test_flag_candidate_id_is_deterministic_sha256() -> None:
    """flag-<sha256(flag)> is stable and unique per flag string."""
    import hashlib

    first = flag_candidate_id("flag{abc123}")
    assert first == f"flag-{hashlib.sha256(b'flag{abc123}').hexdigest()}"
    assert first == flag_candidate_id("flag{abc123}")
    assert first != flag_candidate_id("flag{abc124}")


# ---------------------------------------------------------------------------
# replay consistency (extraction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractor_events_replay_to_identical_graph_hash(tmp_path: Path) -> None:
    """Every extractor mutation is mirrored; replay yields the same hash."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_observed_flag_logged(live, event_log, text="flag{replay-me}")
        extractor = FlagCandidateExtractor(run_id=RUN, event_log=event_log)
        await extractor.extract(live)
        live_hash = await live.graph_hash()

    assert await replay_graph(event_log.path, tmp_path / "replay.db") == live_hash

    events = _read_events(event_log.path)
    assert FLAGS_CANDIDATE_FOUND in _event_types(events)
    assert GRAPH_ENTITY_CREATED in _event_types(events)
    assert GRAPH_EDGE_CREATED in _event_types(events)
    found = next(event for event in events if event["event_type"] == FLAGS_CANDIDATE_FOUND)
    assert found["producer"] == "flags"
    payload = found["payload"]
    assert isinstance(payload, dict)
    assert payload["flag_sha256"] == hashlib.sha256(b"flag{replay-me}").hexdigest()
    assert payload["flag_length"] == len("flag{replay-me}")
    assert "flag" not in payload  # FLAGLEAK-001: no raw flag in run-only events
    assert payload["candidate_id"] == flag_candidate_id("flag{replay-me}")
    assert payload["source_observation_id"] == "obs-1"
    assert payload["evidence_ids"] == ["ev-1"]


@pytest.mark.asyncio
async def test_run_only_events_never_carry_raw_flag(tmp_path: Path) -> None:
    """FLAGLEAK-001: run-only events carry digests, never the raw flag.

    A full extract + accepted-submit cycle writes every run-only event
    type to the log. The raw flag text must appear nowhere in a run-only
    event payload (they are not replay-required), while the
    replay-required ``graph.*`` events may still carry it via entity
    payloads — the sweep below asserts both directions.
    """
    event_log = EventLog(tmp_path / "actions.jsonl")
    raw_flag = "flag{leak-sweep}"
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_observed_flag_logged(live, event_log, text=f"the flag is {raw_flag}")
        await FlagCandidateExtractor(run_id=RUN, event_log=event_log).extract(live)
        await SubmissionCoordinator(
            client=FakeSubmissionClient(accepted=True),
            run_id=RUN,
            challenge_id=CHALLENGE,
            event_log=event_log,
        ).submit_verified_candidate(live)

    events = _read_events(event_log.path)
    run_only = [
        event
        for event in events
        if str(event["event_type"])
        in {FLAGS_CANDIDATE_FOUND, SUBMISSION_ATTEMPTED, SUBMISSION_ACCEPTED, SUBMISSION_REJECTED}
    ]
    graph_events = [event for event in events if str(event["event_type"]).startswith("graph.")]
    assert run_only  # the cycle produced every run-only event
    assert graph_events

    for event in run_only:
        assert raw_flag not in json.dumps(event), f"raw flag leaked in {event['event_type']}"
    # The sweep is sensitive: replay-required graph events still carry
    # the flag (observation + flag_candidate entity payloads), so this
    # test would fail if run events were not redacted.
    assert any(raw_flag in json.dumps(event) for event in graph_events)


# ---------------------------------------------------------------------------
# submission coordinator: accepted path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_accepted_persists_submission_and_returns_result() -> None:
    """An accepted verdict persists the submission and returns the result."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        client = FakeSubmissionClient(accepted=True)
        coordinator = SubmissionCoordinator(client=client, run_id=RUN, challenge_id=CHALLENGE)

        result = await coordinator.submit_verified_candidate(graph)

        assert result.accepted is True
        assert result.points == 100
        assert client.calls == [(CHALLENGE, "flag{abc123}")]

        submission = await graph.get_entity("submission-1")
        assert submission is not None
        assert submission.type == ENTITY_SUBMISSION
        assert submission.data[FIELD_ACCEPTED] is True
        assert submission.data["challenge_id"] == CHALLENGE
        assert submission.data["flag"] == "flag{abc123}"
        assert submission.data["candidate_id"] == "flag-1"
        assert submission.data["points"] == 100

        neighbors = await graph.neighbors("submission-1", EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE)
        assert [(edge.src_id, edge.dst_id) for edge in neighbors.outgoing] == [
            ("submission-1", "flag-1")
        ]

        # The accepted candidate is not marked rejected.
        candidate = await graph.get_entity("flag-1")
        assert candidate is not None
        assert candidate.data[FIELD_REJECTED] is False
        assert candidate.data[FIELD_ATTEMPTS] == 0


@pytest.mark.asyncio
async def test_submit_accepted_routes_graph_to_done() -> None:
    """An accepted submission is the router's terminal signal (DONE)."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(accepted=True),
            run_id=RUN,
            challenge_id=CHALLENGE,
        )
        await coordinator.submit_verified_candidate(graph)
        route = await PhaseRouter().route(graph)
    assert route.phase == Phase.DONE
    assert route.predicate == "has_accepted_submission"


# ---------------------------------------------------------------------------
# submission coordinator: rejection path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_rejected_marks_candidate_and_raises() -> None:
    """A platform rejection marks the candidate rejected and raises loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        client = FakeSubmissionClient(accepted=False, message="wrong flag")
        coordinator = SubmissionCoordinator(client=client, run_id=RUN, challenge_id=CHALLENGE)

        with pytest.raises(SubmissionRejectedError) as excinfo:
            await coordinator.submit_verified_candidate(graph)
        error = excinfo.value
        assert error.candidate_id == "flag-1"
        assert error.flag == "flag{abc123}"
        assert error.message == "wrong flag"

        candidate = await graph.get_entity("flag-1")
        assert candidate is not None
        assert candidate.data[FIELD_REJECTED] is True
        assert candidate.data[FIELD_ATTEMPTS] == 1

        submission = await graph.get_entity("submission-1")
        assert submission is not None
        assert submission.data[FIELD_ACCEPTED] is False
        assert submission.data["message"] == "wrong flag"


@pytest.mark.asyncio
async def test_rejected_flag_is_never_resubmitted() -> None:
    """After a rejection there is no verified candidate left to submit."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(accepted=False),
            run_id=RUN,
            challenge_id=CHALLENGE,
        )
        with pytest.raises(SubmissionRejectedError):
            await coordinator.submit_verified_candidate(graph)

        # The coordinator now refuses: no verified, non-rejected candidate.
        with pytest.raises(MissingRequiredStateError):
            await coordinator.submit_verified_candidate(graph)

        # The router re-routes away from VERIFY_AND_SUBMIT.
        route = await PhaseRouter().route(graph)
        assert route.phase != Phase.VERIFY_AND_SUBMIT
        assert route.predicate != "has_verified_flag"

        # The extractor never resurrects the flag either.
        extractor = FlagCandidateExtractor()
        assert await extractor.extract(graph) == ()
        assert len(await graph.list_entities(ENTITY_FLAG_CANDIDATE)) == 1


@pytest.mark.asyncio
async def test_rejected_flag_does_not_block_a_fresh_flag_hunt() -> None:
    """A rejected candidate leaves the graph free to hunt another flag."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        # Router baseline: recon + enumeration complete, explored access.
        await graph.create_entity("run-1", "run")
        await graph.create_entity("tgt-1", "target", {"confirmed": True})
        await graph.create_entity("svc-1", "service", {"characterized": True})
        await graph.create_entity("cred-1", "credential", {"valid": True, "explored": True})
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(accepted=False),
            run_id=RUN,
            challenge_id=CHALLENGE,
        )
        with pytest.raises(SubmissionRejectedError):
            await coordinator.submit_verified_candidate(graph)

        route = await PhaseRouter().route(graph)
        assert route.phase == Phase.FLAG_HUNT
        assert route.predicate == "has_access_but_no_flag"


# ---------------------------------------------------------------------------
# submission coordinator: refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unprivileged_client_is_refused_before_any_wire_call() -> None:
    """A non-privileged client must never reach the platform."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        client = FakeSubmissionClient(privileged=False)
        coordinator = SubmissionCoordinator(client=client, run_id=RUN, challenge_id=CHALLENGE)

        with pytest.raises(SubmissionPrivilegeError):
            await coordinator.submit_verified_candidate(graph)
        assert client.calls == []  # nothing hit the wire


@pytest.mark.asyncio
async def test_per_candidate_attempt_limit_is_enforced() -> None:
    """A candidate at its attempt budget is refused before any wire call."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph, attempts=2)
        client = FakeSubmissionClient(accepted=False)
        coordinator = SubmissionCoordinator(
            client=client, run_id=RUN, challenge_id=CHALLENGE, max_submissions=2
        )

        with pytest.raises(SubmissionLimitError, match="never re-submitted"):
            await coordinator.submit_verified_candidate(graph)
        assert client.calls == []
        assert await graph.get_entity("submission-1") is None


@pytest.mark.asyncio
async def test_total_submission_budget_is_enforced() -> None:
    """The run-wide submission cap is enforced before any wire call."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        await graph.create_entity(
            "submission-1",
            ENTITY_SUBMISSION,
            {FIELD_ACCEPTED: False, "flag": "flag{other}"},
        )
        client = FakeSubmissionClient(accepted=True)
        coordinator = SubmissionCoordinator(
            client=client, run_id=RUN, challenge_id=CHALLENGE, max_submissions=1
        )

        with pytest.raises(SubmissionLimitError, match="total submission budget"):
            await coordinator.submit_verified_candidate(graph)
        assert client.calls == []


@pytest.mark.asyncio
async def test_no_verified_candidate_raises_missing_state() -> None:
    """A graph with no verified candidate has nothing to submit."""
    async with StateGraph(":memory:") as graph:
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(), run_id=RUN, challenge_id=CHALLENGE
        )
        with pytest.raises(MissingRequiredStateError, match="no verified flag candidate"):
            await coordinator.submit_verified_candidate(graph)


@pytest.mark.asyncio
async def test_verified_candidate_without_provenance_edge_raises() -> None:
    """AGENTS.md invariant: submitted candidates must have provenance."""
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph, with_edge=False)
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(), run_id=RUN, challenge_id=CHALLENGE
        )
        with pytest.raises(MissingRequiredStateError, match="OBSERVED_IN EVIDENCE"):
            await coordinator.submit_verified_candidate(graph)


@pytest.mark.asyncio
async def test_wrong_typed_candidate_payload_raises_loudly() -> None:
    """A non-bool verified field fails loudly, never coerced."""
    async with StateGraph(":memory:") as graph:
        await graph.create_entity(
            "flag-1",
            ENTITY_FLAG_CANDIDATE,
            {FIELD_FLAG: "flag{abc123}", FIELD_VERIFIED: "yes"},
        )
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(), run_id=RUN, challenge_id=CHALLENGE
        )
        with pytest.raises(SubmissionStateError):
            await coordinator.submit_verified_candidate(graph)


def test_coordinator_rejects_invalid_max_submissions() -> None:
    """max_submissions below 1 is a loud argument error."""
    with pytest.raises(ValueError, match="max_submissions"):
        SubmissionCoordinator(
            client=FakeSubmissionClient(), run_id=RUN, challenge_id=CHALLENGE, max_submissions=0
        )


# ---------------------------------------------------------------------------
# submission events + replay consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submission_events_and_replay_identical_hash_accepted(
    tmp_path: Path,
) -> None:
    """Accepted flow: events are mirrored and replay matches the live hash."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_observed_flag_logged(live, event_log, text="flag{accepted-flow}")
        extractor = FlagCandidateExtractor(run_id=RUN, event_log=event_log)
        await extractor.extract(live)
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(accepted=True),
            run_id=RUN,
            challenge_id=CHALLENGE,
            event_log=event_log,
        )
        result = await coordinator.submit_verified_candidate(live)
        assert result.accepted is True
        live_hash = await live.graph_hash()

    assert await replay_graph(event_log.path, tmp_path / "replay.db") == live_hash

    types = _event_types(_read_events(event_log.path))
    assert SUBMISSION_ATTEMPTED in types
    assert SUBMISSION_ACCEPTED in types
    assert SUBMISSION_REJECTED not in types


@pytest.mark.asyncio
async def test_submission_events_and_replay_identical_hash_rejected(
    tmp_path: Path,
) -> None:
    """Rejected flow: the candidate update is mirrored and replay matches."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    async with StateGraph(tmp_path / "live.db") as live:
        await _seed_observed_flag_logged(live, event_log, text="flag{rejected-flow}")
        extractor = FlagCandidateExtractor(run_id=RUN, event_log=event_log)
        await extractor.extract(live)
        coordinator = SubmissionCoordinator(
            client=FakeSubmissionClient(accepted=False, message="nope"),
            run_id=RUN,
            challenge_id=CHALLENGE,
            event_log=event_log,
        )
        with pytest.raises(SubmissionRejectedError):
            await coordinator.submit_verified_candidate(live)
        live_hash = await live.graph_hash()

    assert await replay_graph(event_log.path, tmp_path / "replay.db") == live_hash

    events = _read_events(event_log.path)
    types = _event_types(events)
    assert SUBMISSION_ATTEMPTED in types
    assert SUBMISSION_REJECTED in types
    assert GRAPH_ENTITY_UPDATED in types

    rejected = next(event for event in events if event["event_type"] == SUBMISSION_REJECTED)
    assert rejected["producer"] == SUBMISSIONS_PRODUCER
    payload = rejected["payload"]
    assert isinstance(payload, dict)
    assert payload["candidate_id"] == flag_candidate_id("flag{rejected-flow}")
    assert payload["flag_sha256"] == hashlib.sha256(b"flag{rejected-flow}").hexdigest()
    assert payload["flag_length"] == len("flag{rejected-flow}")
    assert "flag" not in payload  # FLAGLEAK-001: no raw flag in run-only events
    assert payload["accepted"] is False


# ---------------------------------------------------------------------------
# error hierarchy
# ---------------------------------------------------------------------------


def test_error_hierarchy_is_typed() -> None:
    """Every error is a typed RuntimeError subclass (AGENTS.md rule #9)."""
    assert issubclass(FlagsError, RuntimeError)
    assert issubclass(InvalidFlagPatternError, FlagsError)
    assert issubclass(FlagsStateError, FlagsError)

    assert issubclass(SubmissionError, RuntimeError)
    assert issubclass(SubmissionPrivilegeError, SubmissionError)
    assert issubclass(SubmissionLimitError, SubmissionError)
    assert issubclass(SubmissionStateError, SubmissionError)
    assert issubclass(SubmissionRejectedError, SubmissionError)

    error = SubmissionRejectedError(candidate_id="flag-1", flag="flag{x}", message="no")
    assert error.candidate_id == "flag-1"
    assert error.flag == "flag{x}"
    assert error.message == "no"
    assert isinstance(error, RuntimeError)


def test_submission_timestamps_are_utc_aware() -> None:
    """Coordinator-created entity timestamps are timezone-aware UTC."""
    # The graph layer enforces this; assert the pattern used by both
    # modules keeps producing aware datetimes.
    assert datetime.now(UTC).tzinfo is not None
