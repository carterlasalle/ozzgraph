"""Supervisor-only flag submission coordinator for OzzGraph (PR22).

Implements the submission slice of Phase 8 (docs/IMPLEMENTATION_PLAN.md,
PR step 22; docs/TECHNICAL_REQUIREMENTS.md, "Flag Submission": only the
supervisor may submit): :class:`SubmissionCoordinator` finds the graph's
verified flag candidate, validates its observed provenance, enforces the
attempt budgets, and drives the privileged
:class:`~ozzgraph.hal_client.HalClient` — the ONLY caller of
``submit_flag`` in the kernel (AGENTS.md invariant 5).

Design rules:

- Supervisor-only (AGENTS.md rule #5): the coordinator refuses to call
  the wire unless the injected client is privileged
  (:class:`SubmissionPrivilegeError`). HalClient itself double-guards
  ``submit_flag``, so a non-privileged client can never reach the
  platform from any path.

- Provenance is validated, not assumed (AGENTS.md rule #3): the
  candidate must carry ``verified: true`` AND an outgoing
  ``FLAG_CANDIDATE OBSERVED_IN EVIDENCE`` edge. A verified candidate
  without that edge raises :class:`~ozzgraph.router.MissingRequiredStateError`
  — the same typed error the phase router raises for the same
  invariant — and a graph with no verified candidate raises it too.

- Attempt budgets (docs/TECHNICAL_REQUIREMENTS.md: attempt limits): a
  candidate whose ``attempts`` payload reached ``max_submissions``, or a
  run whose submission entities reached ``max_submissions`` in total, is
  refused loudly (:class:`SubmissionLimitError`) before any wire call —
  budget-style, mirroring :mod:`ozzgraph.budgets`.

- Replay compatibility (AGENTS.md data invariants): the submission
  entity and its ``SUBMISSION SUBMITS FLAG_CANDIDATE`` edge share one
  timestamp with their ``graph.*`` events (the PR20 executor pattern),
  so replay reconstructs the identical graph hash. Run events
  (producer ``submissions``): ``submission.attempted`` before the wire
  call, then ``submission.accepted`` or ``submission.rejected`` after
  the verdict — mirroring the executor's "record the attempt before
  execution" boundary. These run-only events carry ``flag_sha256`` +
  ``flag_length`` digests, never the raw flag text (FLAGLEAK-001: they
  are not replay-required, so the plaintext flag is not persisted at
  rest in the event log).

- Rejected candidates are terminal (docs/TECHNICAL_REQUIREMENTS.md:
  not previously rejected): a platform rejection marks the candidate
  ``rejected: true`` (mirrored as a ``graph.entity_updated`` event) and
  increments its ``attempts``, so the phase router re-routes away from
  VERIFY_AND_SUBMIT and the flag is never re-submitted. The typed
  :class:`SubmissionRejectedError` carries the platform message so the
  caller (the supervisor) decides what happens next.

Payload contracts (docs/DATA_STRATEGY.md):

- ``submission`` entity (``submission-<seq>``, deterministic): payload
  ``challenge_id``, ``flag``, ``accepted`` (strict bool — the router's
  terminal signal), ``message``, ``points``, ``candidate_id``.
- ``SUBMISSION SUBMITS FLAG_CANDIDATE`` edge: submission -> candidate.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol

from ozzgraph.config import DEFAULT_MAX_SUBMISSIONS
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_UPDATED,
    SUBMISSION_ACCEPTED,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_REJECTED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    GraphEntityUpdated,
    graph_event,
)
from ozzgraph.flags import (
    EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
    ENTITY_FLAG_CANDIDATE,
    FIELD_ATTEMPTS,
    FIELD_FLAG,
    FIELD_REJECTED,
    FIELD_VERIFIED,
)
from ozzgraph.hal_client import SubmissionResult
from ozzgraph.router import (
    EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
    ENTITY_SUBMISSION,
    FIELD_ACCEPTED,
    MissingRequiredStateError,
)
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Producer name on every submission coordinator event.
SUBMISSIONS_PRODUCER = "submissions"


class SubmissionError(RuntimeError):
    """Base error for the submission layer (AGENTS.md rule #9)."""


class SubmissionPrivilegeError(SubmissionError):
    """The injected client is not privileged, so submission is refused.

    Only the supervisor may submit (AGENTS.md invariant 5,
    docs/TECHNICAL_REQUIREMENTS.md); a non-privileged client must never
    reach the wire.
    """


class SubmissionLimitError(SubmissionError):
    """The per-candidate or total submission attempt budget is exhausted.

    Budget-style (mirroring :mod:`ozzgraph.budgets`): the attempt is
    refused loudly before any wire call rather than silently over-spending.
    """


class SubmissionStateError(SubmissionError):
    """A payload field the coordinator reads on the candidate is invalid.

    The coordinator never coerces a wrong-typed ``verified``,
    ``rejected``, ``attempts``, or ``flag`` field — corrupt state fails
    loudly.
    """


class SubmissionRejectedError(SubmissionError):
    """The platform rejected the submitted flag.

    The candidate has been marked ``rejected: true`` and its ``attempts``
    incremented (mirrored as a ``graph.entity_updated`` event), so the
    router re-routes away from VERIFY_AND_SUBMIT and the flag is never
    re-submitted. The platform ``message`` rides on the error so the
    caller decides the next move (hunt another flag, terminate, ...).

    Attributes:
        candidate_id: The rejected ``flag_candidate`` entity id.
        flag: The flag text that was rejected.
        message: The platform's rejection message.
    """

    def __init__(self, *, candidate_id: str, flag: str, message: str) -> None:
        super().__init__(message)
        self.candidate_id = candidate_id
        self.flag = flag
        self.message = message


class SubmissionClient(Protocol):
    """The privileged submit surface the coordinator needs.

    :class:`~ozzgraph.hal_client.HalClient` satisfies this protocol
    structurally; tests inject lightweight fakes. The coordinator checks
    ``privileged`` before calling ``submit_flag``, so the supervisor-only
    boundary holds for every implementer.
    """

    @property
    def privileged(self) -> bool: ...

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult: ...

    async def aclose(self) -> None: ...


class SubmissionCoordinator:
    """Supervisor-only flag submission coordinator.

    Args:
        client: The privileged HalCTF client used for ``flag.submit``.
            Must be ``privileged`` — anything else raises
            :class:`SubmissionPrivilegeError`.
        run_id: Run identifier recorded on every event.
        challenge_id: The challenge the candidate is submitted to.
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``submission.*`` run events; when ``None`` no
            events are emitted.
        max_submissions: Attempt cap — per candidate and in total
            (default :data:`~ozzgraph.config.DEFAULT_MAX_SUBMISSIONS`).

    Raises:
        ValueError: If ``max_submissions`` is less than 1.
    """

    def __init__(
        self,
        *,
        client: SubmissionClient,
        run_id: str,
        challenge_id: str,
        event_log: EventLog | None = None,
        max_submissions: int = DEFAULT_MAX_SUBMISSIONS,
    ) -> None:
        if max_submissions < 1:
            raise ValueError(f"max_submissions must be >= 1, got {max_submissions}")
        self._client = client
        self._run_id = run_id
        self._challenge_id = challenge_id
        self._event_log = event_log
        self._max_submissions = max_submissions

    async def submit_verified_candidate(self, graph: StateGraph) -> SubmissionResult:
        """Submit the graph's verified flag candidate and persist the outcome.

        Flow: find the verified candidate (id order; provenance edge
        validated — :class:`~ozzgraph.router.MissingRequiredStateError`
        when absent) -> enforce the supervisor-only and attempt-budget
        invariants -> record ``submission.attempted`` -> call
        ``client.submit_flag`` -> persist the submission entity + its
        ``SUBMISSION SUBMITS FLAG_CANDIDATE`` edge (replay-consistent)
        -> on acceptance return the typed :class:`SubmissionResult` (the
        router then routes DONE); on rejection mark the candidate
        ``rejected: true`` and raise :class:`SubmissionRejectedError`.

        Args:
            graph: The authoritative SQLite state graph holding the
                verified flag candidate.

        Raises:
            MissingRequiredStateError: If no verified, non-rejected flag
                candidate exists, or a verified candidate lacks its
                ``FLAG_CANDIDATE OBSERVED_IN EVIDENCE`` edge.
            SubmissionPrivilegeError: If the client is not privileged.
            SubmissionLimitError: If the candidate's attempt budget or
                the run's total submission budget is exhausted.
            SubmissionStateError: If a candidate payload field the
                coordinator reads is wrong-typed.
            SubmissionRejectedError: If the platform rejected the flag
                (the candidate is marked rejected and never re-submitted).
            HalServiceError: If the platform call fails after bounded
                retries (the candidate stays verified; the caller
                decides whether to retry later).
        """
        candidate = await self._find_verified_candidate(graph)
        if not self._client.privileged:
            raise SubmissionPrivilegeError(
                "flag submission is supervisor-only; the client must be constructed "
                "with privileged=True (AGENTS.md invariant 5)"
            )
        attempts = self._candidate_attempts(candidate)
        if attempts >= self._max_submissions:
            raise SubmissionLimitError(
                f"candidate {candidate.id!r} has {attempts} rejected attempt(s) "
                f">= limit {self._max_submissions}; the flag is never re-submitted"
            )
        await self._check_total_budget(graph)

        flag = self._candidate_flag(candidate)
        submission_id = await self._next_submission_id(graph)
        self._append(
            SUBMISSION_ATTEMPTED,
            {
                "submission_id": submission_id,
                "candidate_id": candidate.id,
                "challenge_id": self._challenge_id,
                "flag_sha256": hashlib.sha256(flag.encode("utf-8")).hexdigest(),
                "flag_length": len(flag),
            },
        )
        result = await self._client.submit_flag(self._challenge_id, flag)
        await self._persist_submission(graph, submission_id, candidate.id, flag, result)
        if result.accepted:
            self._append(
                SUBMISSION_ACCEPTED,
                {
                    "submission_id": submission_id,
                    "candidate_id": candidate.id,
                    "flag_sha256": hashlib.sha256(flag.encode("utf-8")).hexdigest(),
                    "flag_length": len(flag),
                    "accepted": True,
                    "points": result.points,
                    "message": result.message,
                },
            )
            return result

        await self._mark_rejected(graph, candidate, attempts)
        self._append(
            SUBMISSION_REJECTED,
            {
                "submission_id": submission_id,
                "candidate_id": candidate.id,
                "flag_sha256": hashlib.sha256(flag.encode("utf-8")).hexdigest(),
                "flag_length": len(flag),
                "accepted": False,
                "message": result.message,
            },
        )
        raise SubmissionRejectedError(candidate_id=candidate.id, flag=flag, message=result.message)

    async def _find_verified_candidate(self, graph: StateGraph) -> EntityRecord:
        """The first verified, non-rejected candidate, in id order.

        Raises:
            MissingRequiredStateError: If no verified candidate exists
                (nothing to submit), or a verified candidate lacks its
                ``FLAG_CANDIDATE OBSERVED_IN EVIDENCE`` edge (AGENTS.md
                data invariant: every submitted flag candidate has
                observed provenance).
            SubmissionStateError: If a ``verified``/``rejected`` payload
                field is wrong-typed.
        """
        for record in await graph.list_entities(ENTITY_FLAG_CANDIDATE):
            if not self._payload_bool(record, FIELD_VERIFIED):
                continue
            if self._payload_bool(record, FIELD_REJECTED):
                continue
            if not await self._has_outgoing_edge(
                graph, record.id, EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE
            ):
                raise MissingRequiredStateError(
                    f"flag candidate {record.id!r} is verified but has no "
                    f"{EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE!r} edge"
                )
            return record
        raise MissingRequiredStateError(
            "no verified flag candidate in the graph; nothing to submit"
        )

    def _candidate_attempts(self, record: EntityRecord) -> int:
        """The candidate's rejection count, defaulting to 0.

        Raises:
            SubmissionStateError: If ``attempts`` is present and not an
                int (fail loudly, never coerced).
        """
        raw = record.data.get(FIELD_ATTEMPTS)
        if raw is None:
            return 0
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SubmissionStateError(
                f"candidate {record.id!r} payload field {FIELD_ATTEMPTS!r} must be an "
                f"int, got {type(raw).__name__} ({raw!r})"
            )
        return raw

    def _candidate_flag(self, record: EntityRecord) -> str:
        """The candidate's flag text.

        Raises:
            SubmissionStateError: If ``flag`` is missing or not a
                non-empty string.
        """
        raw = record.data.get(FIELD_FLAG)
        if not isinstance(raw, str) or raw == "":
            raise SubmissionStateError(
                f"candidate {record.id!r} payload field {FIELD_FLAG!r} must be a "
                f"non-empty string, got {type(raw).__name__} ({raw!r})"
            )
        return raw

    async def _check_total_budget(self, graph: StateGraph) -> None:
        """Refuse when the run's total submission budget is exhausted.

        Raises:
            SubmissionLimitError: If submission entities already reach
                ``max_submissions`` (budget-style: refused before any
                wire call).
        """
        total = len(await graph.list_entities(ENTITY_SUBMISSION))
        if total >= self._max_submissions:
            raise SubmissionLimitError(
                f"total submission budget exhausted: {total} submissions "
                f">= limit {self._max_submissions}"
            )

    async def _persist_submission(
        self,
        graph: StateGraph,
        submission_id: str,
        candidate_id: str,
        flag: str,
        result: SubmissionResult,
    ) -> None:
        """Persist one submission entity plus its SUBMITS edge (PR20 pattern).

        The entity and edge share one timestamp with their ``graph.*``
        events, so replay reconstructs the identical graph hash.
        """
        payload: dict[str, object] = {
            "challenge_id": result.challenge_id,
            "flag": flag,
            FIELD_ACCEPTED: result.accepted,
            "message": result.message,
            "points": result.points,
            "candidate_id": candidate_id,
        }
        await self._create_entity(graph, submission_id, ENTITY_SUBMISSION, payload)
        await self._create_edge(
            graph,
            f"{submission_id}-submits-{candidate_id}",
            EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
            submission_id,
            candidate_id,
        )

    async def _mark_rejected(self, graph: StateGraph, record: EntityRecord, attempts: int) -> None:
        """Mark the candidate rejected and increment its attempt count.

        The updated payload is mirrored as a ``graph.entity_updated``
        event with the same timestamp, so replay reconstructs the
        identical graph hash. The router (PR18) treats a rejected
        candidate as not-verified, so the graph re-routes away from
        VERIFY_AND_SUBMIT and the flag is never re-submitted.
        """
        payload = dict(record.data)
        payload[FIELD_REJECTED] = True
        payload[FIELD_ATTEMPTS] = attempts + 1
        at = datetime.now(UTC)
        await graph.update_entity(record.id, payload, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_UPDATED,
                    self._run_id,
                    SUBMISSIONS_PRODUCER,
                    GraphEntityUpdated(entity_id=record.id, data=payload, at=at),
                )
            )

    async def _create_entity(
        self,
        graph: StateGraph,
        entity_id: str,
        entity_type: str,
        data: dict[str, object],
    ) -> None:
        """Create one entity and mirror the mutation to the event log."""
        at = datetime.now(UTC)
        await graph.create_entity(entity_id, entity_type, data, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    self._run_id,
                    SUBMISSIONS_PRODUCER,
                    GraphEntityCreated(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        data=data,
                        at=at,
                    ),
                )
            )

    async def _create_edge(
        self,
        graph: StateGraph,
        edge_id: str,
        edge_type: str,
        src_id: str,
        dst_id: str,
    ) -> None:
        """Create one edge and mirror the mutation to the event log."""
        at = datetime.now(UTC)
        await graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
        if self._event_log is not None:
            self._event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    self._run_id,
                    SUBMISSIONS_PRODUCER,
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=edge_type,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=at,
                    ),
                )
            )

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one coordinator run event when an event log is configured."""
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=SUBMISSIONS_PRODUCER,
                    payload=payload,
                )
            )

    @staticmethod
    async def _has_outgoing_edge(graph: StateGraph, entity_id: str, edge_type: str) -> bool:
        """True when ``entity_id`` is the source of an edge of ``edge_type``."""
        neighbors = await graph.neighbors(entity_id, edge_type)
        return any(edge.src_id == entity_id for edge in neighbors.outgoing)

    @staticmethod
    async def _next_submission_id(graph: StateGraph) -> str:
        """The next deterministic submission entity id (``submission-<seq>``).

        Mirrors the evaluator's ``eval-<plan>-<seq>`` pattern: the
        sequence follows the existing submission entities, so identical
        graph states yield identical ids and replay reconstructs them.
        """
        existing = await graph.list_entities(ENTITY_SUBMISSION)
        return f"submission-{len(existing) + 1}"

    def _payload_bool(self, record: EntityRecord, key: str) -> bool:
        """Read a strict-boolean payload field, defaulting to False.

        Raises:
            SubmissionStateError: If ``key`` is present on the record's
                payload and is not a bool (fail loudly, never coerced).
        """
        value = record.data.get(key)
        if value is None:
            return False
        if not isinstance(value, bool):
            raise SubmissionStateError(
                f"entity {record.id!r} payload field {key!r} must be a bool, "
                f"got {type(value).__name__} ({value!r})"
            )
        return value
