"""Deterministic flag candidate extraction with provenance (V09, HalCTF).

Implements the candidate-extraction slice of Phase 8 (docs/
IMPLEMENTATION_PLAN.md, PR step 22; docs/TECHNICAL_REQUIREMENTS.md,
"Flag Submission"): a flag candidate is created only when the flag text
appears VERBATIM in an observation (or an artifact that observation
references) AND that observation has at least one evidence entity linked
via ``EVIDENCE EXTRACTED_FROM OBSERVATION``. A bare model claim is never
a candidate — provenance is required (AGENTS.md rule #3).

V09 (v2/halctf-adapter, docs/adr/0011): this module is owned by the
HalCTF environment — it moved out of the generic kernel
(``ozzgraph.flags`` was deleted) into ``ozzgraph.environments.halctf``
so the kernel never imports HalCTF concepts directly. The generic
entity vocabulary it shares with the kernel (``observation``,
``evidence``, the extraction edge) lives in :mod:`ozzgraph.entities`.

Design rules:

- Deterministic extraction: :meth:`FlagCandidateExtractor.extract`
  scans observation entities in id order, resolves their evidence via
  the graph, and matches the configured flag pattern over the
  observation's string payload fields (recursively) plus the contents
  of referenced artifacts. The same graph and artifact store always
  yield the same candidates.

- Provenance gate: an observation with NO evidence entity linked via
  ``EVIDENCE EXTRACTED_FROM OBSERVATION`` produces no verified
  candidate — the flag text must be traceable to evidence, never to
  model prose.

- Idempotent by hash: a candidate's entity id is ``flag-<sha256(flag)>``,
  so the same flag string always maps to the same entity. A candidate
  that already exists is never re-created; a candidate that is
  ``rejected: true`` or has exhausted its attempt budget is skipped
  (the flag is never resurrected after the platform rejected it).

- Replay compatibility (AGENTS.md data invariants): every graph
  mutation shares one timestamp with its ``graph.*`` event (the PR20
  executor pattern), so replaying the log reconstructs the identical
  graph hash. Each found candidate also emits a ``flags.candidate_found``
  run event (producer ``flags``) whose payload carries only a
  ``flag_sha256`` digest and ``flag_length`` — never the raw flag text
  (FLAGLEAK-001: run-only events are not replay-required, so the
  plaintext flag is not persisted at rest in the event log).

- Fail loudly (AGENTS.md rule #9): an invalid configured flag pattern
  is rejected at construction (:class:`InvalidFlagPatternError`), and
  a wrong-typed payload field on an existing candidate is raised
  (:class:`FlagsStateError`) rather than coerced. Missing referenced
  artifacts are skipped defensively — the observation payload itself is
  always scanned, and artifact contents are a best-effort supplement,
  never the provenance gate.

Payload contract (docs/DATA_STRATEGY.md; the router reads ``verified``
and ``rejected`` as strict booleans):

- ``flag``: the exact flag text.
- ``verified``: ``true`` — the candidate has observed provenance.
- ``source_observation_id``: the observation the flag appeared in.
- ``evidence_ids``: the evidence entities backing that observation.
- ``rejected``: ``false`` at creation; the submission coordinator
  (PR22) flips it after a platform rejection.
- ``attempts``: ``0`` at creation; counts platform rejections.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.artifacts import ArtifactIndexError, ArtifactNotFoundError, ArtifactStore
from ozzgraph.config import DEFAULT_FLAG_PATTERN, DEFAULT_MAX_SUBMISSIONS
from ozzgraph.entities import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    ENTITY_OBSERVATION,
)
from ozzgraph.events import (
    FLAGS_CANDIDATE_FOUND,
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.state_graph import EntityRecord, StateGraph

#: Producer name on every flag extractor event.
FLAGS_PRODUCER = "flags"

#: Entity types the extractor reads and writes (docs/DATA_STRATEGY.md,
#: lowercase by convention). ``observation`` / ``evidence`` are the
#: generic kernel vocabulary (ozzgraph.entities, V09 docs/adr/0011);
#: ``flag_candidate`` is HalCTF-owned.
ENTITY_FLAG_CANDIDATE = "flag_candidate"

#: Edge types the extractor reads and writes (docs/DATA_STRATEGY.md,
#: uppercase by convention). The evidence edge direction is resolved
#: from either endpoint, mirroring the evaluator (PR21). The
#: ``EVIDENCE EXTRACTED_FROM OBSERVATION`` edge is the generic kernel
#: vocabulary (ozzgraph.entities); the candidate edge is HalCTF-owned.
EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE = "FLAG_CANDIDATE OBSERVED_IN EVIDENCE"

#: Payload fields of a flag_candidate entity.
FIELD_FLAG = "flag"
FIELD_VERIFIED = "verified"
FIELD_SOURCE_OBSERVATION_ID = "source_observation_id"
FIELD_EVIDENCE_IDS = "evidence_ids"
FIELD_REJECTED = "rejected"
FIELD_ATTEMPTS = "attempts"

#: Cap on a single artifact's scanned text, in characters. Artifact
#: content is bounded by the shell output limits, but a defensive cap
#: keeps the scan deterministic and cheap regardless of store contents.
_ARTIFACT_SCAN_LIMIT = 1 << 20

#: JSON ``artifact_ids`` list shape an observation payload may carry
#: (the ``OBSERVATION STORED_AS ARTIFACT`` handle list).
_ARTIFACT_IDS_FIELD = "artifact_ids"


class FlagsError(RuntimeError):
    """Base error for the flag extraction layer (AGENTS.md rule #9)."""


class InvalidFlagPatternError(FlagsError):
    """The configured flag pattern is not a valid regular expression.

    Raised at :class:`FlagCandidateExtractor` construction so a bad
    ``OZZGRAPH_FLAG_PATTERN`` fails loudly at configuration time, never
    mid-run.
    """


class FlagsStateError(FlagsError):
    """A payload field the extractor reads on an existing candidate is invalid.

    The extractor never coerces a wrong-typed ``rejected`` or
    ``attempts`` field on an already-persisted candidate — that state is
    corrupt and fails loudly instead.
    """


class FlagCandidate(BaseModel):
    """One extracted, provenance-backed flag candidate.

    Attributes:
        flag: The exact flag text found in the observation/artifact.
        entity_id: The persisted ``flag_candidate`` entity id
            (``flag-<sha256(flag)>``).
        source_observation_id: The observation the flag appeared in.
        evidence_ids: Evidence entities backing that observation
            (``EVIDENCE EXTRACTED_FROM OBSERVATION``), ordered by edge
            id.
    """

    model_config = ConfigDict(extra="forbid")

    flag: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()


def flag_candidate_id(flag: str) -> str:
    """The deterministic entity id for ``flag``: ``flag-<sha256 hex>``.

    Two runs that observe the same flag text persist the same entity
    id, so extraction is idempotent and a flag is never duplicated in
    the graph (docs/TECHNICAL_REQUIREMENTS.md: a candidate must not be
    previously rejected; never a second candidate for the same string).
    """
    digest = hashlib.sha256(flag.encode("utf-8")).hexdigest()
    return f"flag-{digest}"


class FlagCandidateExtractor:
    """Deterministic, provenance-gated flag candidate extractor.

    Args:
        run_id: Run identifier recorded on every event.
        event_log: Optional append-only log for ``graph.*`` mutation
            events and ``flags.*`` run events; when ``None`` no events
            are emitted.
        pattern: Regular expression matched against observation and
            artifact text. Defaults to :data:`~ozzgraph.config.DEFAULT_FLAG_PATTERN`
            (``flag{...}`` style).
        max_attempts: Attempt budget per candidate; a candidate whose
            ``attempts`` payload reaches this value is skipped. Defaults
            to :data:`~ozzgraph.config.DEFAULT_MAX_SUBMISSIONS`.
        artifact_store: Optional artifact store for scanning the
            contents of artifacts an observation references (its
            ``artifact_ids`` payload). Missing artifacts are skipped.

    Raises:
        InvalidFlagPatternError: If ``pattern`` is not a valid regular
            expression.
        ValueError: If ``max_attempts`` is less than 1.
    """

    def __init__(
        self,
        *,
        run_id: str = "flags",
        event_log: EventLog | None = None,
        pattern: str = DEFAULT_FLAG_PATTERN,
        max_attempts: int = DEFAULT_MAX_SUBMISSIONS,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        try:
            self._pattern = re.compile(pattern)
        except re.error as exc:
            raise InvalidFlagPatternError(
                f"flag pattern {pattern!r} is not a valid regular expression: {exc}"
            ) from exc
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._run_id = run_id
        self._event_log = event_log
        self._max_attempts = max_attempts
        self._artifact_store = artifact_store

    async def extract(self, graph: StateGraph) -> tuple[FlagCandidate, ...]:
        """Scan every evidence-backed observation and persist new candidates.

        Deterministic flow: list observations (ordered by id); for each,
        resolve the backing evidence entities (none means no candidate —
        the provenance gate); scan the observation payload and any
        referenced artifact contents for the flag pattern; and persist
        one ``flag_candidate`` entity per NEW flag string (``flag-<hash>``
        already present, rejected, or at its attempt budget is skipped),
        each with ``FLAG_CANDIDATE OBSERVED_IN EVIDENCE`` edges to its
        backing evidence and mirrored ``graph.*`` events.

        Args:
            graph: The authoritative SQLite state graph to scan and
                mutate.

        Raises:
            FlagsStateError: If an existing candidate's ``rejected`` or
                ``attempts`` payload field is wrong-typed.

        Returns:
            The newly persisted candidates, in deterministic order.
        """
        candidates: list[FlagCandidate] = []
        for record in await graph.list_entities(ENTITY_OBSERVATION):
            evidence_ids = await self._evidence_for_observation(graph, record.id)
            if not evidence_ids:
                continue  # provenance gate: no evidence, no verified candidate
            texts = self._observation_texts(record)
            for flag in self._matches(texts):
                candidate_id = flag_candidate_id(flag)
                existing = await graph.get_entity(candidate_id)
                if existing is not None:
                    if self._skippable(existing):
                        continue
                    continue  # already present — never a second candidate
                await self._persist_candidate(graph, candidate_id, flag, record.id, evidence_ids)
                candidates.append(
                    FlagCandidate(
                        flag=flag,
                        entity_id=candidate_id,
                        source_observation_id=record.id,
                        evidence_ids=evidence_ids,
                    )
                )
        return tuple(candidates)

    async def _evidence_for_observation(
        self, graph: StateGraph, observation_id: str
    ) -> tuple[str, ...]:
        """Evidence entity ids linked to ``observation_id``, ordered by edge id.

        The ``EVIDENCE EXTRACTED_FROM OBSERVATION`` direction is
        resolved from either endpoint (mirroring the evaluator's
        defensive read): an incoming edge contributes its source, an
        outgoing edge its destination.
        """
        neighbors = await graph.neighbors(observation_id, EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION)
        evidence_ids = [
            edge.src_id if edge.dst_id == observation_id else edge.dst_id
            for edge in (*neighbors.incoming, *neighbors.outgoing)
        ]
        return tuple(evidence_ids)

    def _observation_texts(self, record: EntityRecord) -> list[str]:
        """String texts to scan for one observation.

        Every string value in the observation payload (recursively) —
        the summary, structured data, and metadata — plus the contents
        of any artifacts the payload references via ``artifact_ids``
        when an artifact store is configured. Missing artifacts are
        skipped defensively: the payload itself was already scanned, and
        artifact contents are a best-effort supplement, never the
        provenance gate.
        """
        texts: list[str] = []
        self._collect_strings(record.data, texts)
        if self._artifact_store is None:
            return texts
        for artifact_id in self._referenced_artifact_ids(record.data):
            try:
                path = self._artifact_store.path_for(artifact_id)
            except (ArtifactNotFoundError, ArtifactIndexError):
                continue  # store drift must not break extraction (payload was scanned)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # pragma: no cover - depends on external store state
            texts.append(content[:_ARTIFACT_SCAN_LIMIT])
        return texts

    def _collect_strings(self, value: object, out: list[str]) -> None:
        """Append every string reachable through ``value`` (recursively)."""
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                self._collect_strings(nested, out)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                self._collect_strings(nested, out)

    def _referenced_artifact_ids(self, payload: Mapping[str, object]) -> tuple[str, ...]:
        """Artifact ids referenced by an observation payload."""
        raw = payload.get(_ARTIFACT_IDS_FIELD)
        if not isinstance(raw, list):
            return ()
        ids = [item for item in raw if isinstance(item, str) and item]
        return tuple(dict.fromkeys(ids))  # first-seen order, deduped

    def _matches(self, texts: Sequence[str]) -> tuple[str, ...]:
        """Distinct flag matches across ``texts``, in first-seen order.

        The flag text must appear EXACTLY as matched — extraction is
        verbatim, never assembled or repaired (docs/TECHNICAL_REQUIREMENTS.md:
        a candidate must appear exactly in an observation or artifact).
        """
        found: list[str] = []
        seen: set[str] = set()
        for text in texts:
            for match in self._pattern.finditer(text):
                flag = match.group(0)
                if flag not in seen:
                    seen.add(flag)
                    found.append(flag)
        return tuple(found)

    def _skippable(self, record: EntityRecord) -> bool:
        """True when an existing candidate must not be re-created.

        A candidate that is already present (idempotent by hash), was
        rejected by the platform, or has exhausted its attempt budget is
        skipped — the flag is never resurrected and never re-submitted
        (docs/TECHNICAL_REQUIREMENTS.md, "Flag Submission").

        Raises:
            FlagsStateError: If ``rejected`` or ``attempts`` is present
                and wrong-typed (fail loudly, never coerced).
        """
        rejected = record.data.get(FIELD_REJECTED)
        if rejected is not None and not isinstance(rejected, bool):
            raise FlagsStateError(
                f"candidate {record.id!r} payload field {FIELD_REJECTED!r} must be a "
                f"bool, got {type(rejected).__name__} ({rejected!r})"
            )
        attempts = record.data.get(FIELD_ATTEMPTS)
        if attempts is not None and (isinstance(attempts, bool) or not isinstance(attempts, int)):
            raise FlagsStateError(
                f"candidate {record.id!r} payload field {FIELD_ATTEMPTS!r} must be an "
                f"int, got {type(attempts).__name__} ({attempts!r})"
            )
        if rejected is True:
            return True
        return attempts is not None and attempts >= self._max_attempts

    async def _persist_candidate(
        self,
        graph: StateGraph,
        candidate_id: str,
        flag: str,
        observation_id: str,
        evidence_ids: Sequence[str],
    ) -> None:
        """Persist one candidate entity plus its evidence edges (PR20 pattern).

        The entity and every edge share one timestamp with their
        ``graph.*`` events, so replaying the log reconstructs the same
        graph hash. The found candidate is also recorded as a
        ``flags.candidate_found`` run event.
        """
        payload: dict[str, object] = {
            FIELD_FLAG: flag,
            FIELD_VERIFIED: True,
            FIELD_SOURCE_OBSERVATION_ID: observation_id,
            FIELD_EVIDENCE_IDS: list(evidence_ids),
            FIELD_REJECTED: False,
            FIELD_ATTEMPTS: 0,
        }
        await self._create_entity(graph, candidate_id, ENTITY_FLAG_CANDIDATE, payload)
        for evidence_id in evidence_ids:
            await self._create_edge(
                graph,
                f"{candidate_id}-observed-in-{evidence_id}",
                EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE,
                candidate_id,
                evidence_id,
            )
        self._append(
            FLAGS_CANDIDATE_FOUND,
            {
                "candidate_id": candidate_id,
                "flag_sha256": hashlib.sha256(flag.encode("utf-8")).hexdigest(),
                "flag_length": len(flag),
                "source_observation_id": observation_id,
                "evidence_ids": list(evidence_ids),
            },
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
                    FLAGS_PRODUCER,
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
                    FLAGS_PRODUCER,
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
        """Append one extractor run event when an event log is configured."""
        if self._event_log is not None:
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=event_type,
                    producer=FLAGS_PRODUCER,
                    payload=payload,
                )
            )
