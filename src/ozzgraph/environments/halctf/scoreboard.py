"""Deterministic scoreboard access for the HalCTF environment (V09).

The ``scoreboard`` tool of the official HalCTF MCP tool set
(docs/CHANGES_v2.md milestone 9) is read-only — unlike flag submission
and paid hints it is NOT privileged — but it is still environment
territory: models never call MCP directly (AGENTS.md invariant 5); the
model-facing surface is ``halctl scoreboard``, and the supervisor-side
service is this coordinator, owned by the HalCTF environment
(docs/adr/0011).

Design rules:

- Deterministic and bounded: :meth:`ScoreboardCoordinator.refresh`
  fetches the normalized :class:`~ozzgraph.hal_client.Scoreboard`
  through the injected client and records exactly one
  ``scoreboard.retrieved`` run event (producer ``scoreboard``) carrying
  only bounded aggregate data (entry count, top rank/points) — never a
  raw leaderboard dump.
- Replay compatibility: the scoreboard is live external state, so it is
  NEVER persisted to the graph; the run event is run-only (not
  replay-required), so replaying the log reconstructs the identical
  graph hash.
- Fail loudly (AGENTS.md rule #9): a client failure propagates as the
  typed :class:`~ozzgraph.hal_client.HalServiceError`; the caller
  decides whether the failure is fatal.

Payload contract (docs/DATA_STRATEGY.md, run-only events):
``scoreboard.retrieved`` -> ``entries`` (count), ``top_rank``,
``top_user``, ``top_points``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from ozzgraph.events import SCOREBOARD_RETRIEVED, Event, EventLog
from ozzgraph.hal_client import Scoreboard

#: Producer name on every scoreboard coordinator event.
SCOREBOARD_PRODUCER = "scoreboard"


class ScoreboardError(RuntimeError):
    """Base error for the scoreboard layer (AGENTS.md rule #9)."""


class ScoreboardClient(Protocol):
    """The scoreboard surface the coordinator needs.

    :class:`~ozzgraph.hal_client.HalClient` satisfies this protocol
    structurally; tests inject lightweight fakes. Scoreboard reads are
    read-only and open to any client (the privileged boundary covers
    submit / paid hints / exit only).
    """

    async def get_scoreboard(self) -> Scoreboard: ...

    async def aclose(self) -> None: ...


class ScoreboardCoordinator:
    """Supervisor-side scoreboard retrieval, recorded as a run event.

    Args:
        client: The HalCTF client used for ``scoreboard.get``.
        run_id: Run identifier recorded on every event.
        event_log: Optional append-only log for the
            ``scoreboard.retrieved`` run event; when ``None`` no event
            is emitted.
    """

    def __init__(
        self,
        *,
        client: ScoreboardClient,
        run_id: str,
        event_log: EventLog | None = None,
    ) -> None:
        self._client = client
        self._run_id = run_id
        self._event_log = event_log

    async def refresh(self) -> Scoreboard:
        """Fetch the competition scoreboard and record the retrieval.

        The scoreboard is live external state: it is returned to the
        caller and recorded as a bounded run event, never persisted to
        the graph (replay compatibility).

        Raises:
            ozzgraph.hal_client.HalServiceError: If the platform call
                fails after bounded retries.

        Returns:
            The platform's typed :class:`~ozzgraph.hal_client.Scoreboard`.
        """
        board = await self._client.get_scoreboard()
        if self._event_log is not None:
            payload: dict[str, object] = {"entries": len(board.entries)}
            if board.entries:
                top = board.entries[0]
                payload["top_rank"] = top.rank
                payload["top_user"] = top.user_id
                payload["top_points"] = top.points
            self._event_log.append(
                Event(
                    run_id=self._run_id,
                    timestamp=datetime.now(UTC),
                    event_type=SCOREBOARD_RETRIEVED,
                    producer=SCOREBOARD_PRODUCER,
                    payload=payload,
                )
            )
        return board
