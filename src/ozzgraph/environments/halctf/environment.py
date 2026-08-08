"""HalCTF environment adapter — FULL V09 slice (v2/halctf-adapter).

:class:`HalCTFEnvironment` is the HalCTF runtime environment behind the
generic :class:`~ozzgraph.environments.base.EnvironmentAdapter` protocol
(docs/CHANGES_v2.md, milestone 9; docs/adr/0011). The V01 minimal slice
(docs/adr/0008) read the operator's configuration and expressed the
challenge as ONE :class:`~ozzgraph.environments.models.Target` and the
flag objective as ONE
:class:`~ozzgraph.environments.models.Objective` — a plain completion
contract, NOT a kernel phase. V09 completes the adapter:

- **Deterministic runtime discovery** (docs/adr/0011): HalCTF mode is
  selected by the presence of any HalCTF runtime variable (``HAL_CTF_ID``,
  ``HAL_CHALLENGE_ID``, ``HAL_ENDPOINT``, ``HAL_MCP_ENDPOINT``,
  ``MCP_ENDPOINT``, or the legacy ``OZZGRAPH_CHALLENGE_ID``), and the
  MCP endpoint is resolved from the first non-blank of
  ``OZZGRAPH_MCP_BASE_URL`` / ``HAL_MCP_ENDPOINT`` / ``HAL_ENDPOINT`` /
  ``MCP_ENDPOINT`` / ``OPENAI_BASE_URL``
  (:func:`ozzgraph.config.discover_halctf_endpoint`). HalCTF mode
  WITHOUT a discoverable endpoint fails loudly at construction
  (:class:`~ozzgraph.config.ConfigError` — the run cannot reach the
  platform). The local default is unchanged: with no HalCTF runtime
  variable the supervisor selects :class:`~ozzgraph.environments.local.LocalEnvironment`
  and the V08 ``OZZGRAPH_TARGET`` classification stays authoritative.
- **Environment-provided services**: the adapter owns the HalCTF
  behavior — the flag candidate extractor, the supervisor-only
  submission coordinator, the paid-hint coordinator (with the
  deterministic gate), and the scoreboard coordinator are exposed as
  service factories wired to the environment's own challenge id and the
  config's budgets. The generic kernel (supervisor, runner, router)
  never imports ``ozzgraph.hints`` / ``ozzgraph.submissions`` /
  ``ozzgraph.flags`` (the modules moved HERE); it drives the adapter
  through the protocol and the ``ozzgraph.environments.halctf`` shim.
- **Graceful completion**: the objective's ``success_hint`` names the
  deterministic completion signal (a submission accepted through the
  privileged surface). The runner's generic DONE path (accepted
  submission routed DONE, or all objectives completed) terminates the
  run COMPLETED and renders the V08 report bundle — a HalCTF run can
  complete gracefully with zero kernel HalCTF knowledge.

Discovery performs NO I/O in this adapter (it derives everything from
configuration), so discovery is deterministic and testable without an
MCP server; the platform calls themselves are the injected clients'
job (bootstrap, halctl, and the environment's coordinators).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.config import (
    ConfigError,
    OzzGraphConfig,
    discover_halctf_challenge_id,
    require_halctf_endpoint,
)
from ozzgraph.environments.halctf.flags import FlagCandidateExtractor
from ozzgraph.environments.halctf.hints import HintClient, HintCoordinator, HintPolicy
from ozzgraph.environments.halctf.scoreboard import ScoreboardClient, ScoreboardCoordinator
from ozzgraph.environments.halctf.submissions import SubmissionClient, SubmissionCoordinator
from ozzgraph.environments.models import Objective, Scope, Target
from ozzgraph.events import EventLog
from ozzgraph.halctl import CHALLENGE_ID_ENV

#: Conservative capability vocabulary for a HalCTF challenge;
#: the tool-runtime milestone replaces these static sets with a real
#: ToolInventory.
DEFAULT_HALCTF_CAPABILITIES: frozenset[str] = frozenset(
    {"http.request", "network.probe", "filesystem.read"}
)

#: Objective id for the single HalCTF objective.
HALCTF_OBJECTIVE_ID = "objective-halctf-flag"

#: Bounded description of the HalCTF objective (an Objective, never a
#: kernel phase).
HALCTF_OBJECTIVE_DESCRIPTION = (
    "Obtain the challenge's flag and submit it through the privileged "
    "supervisor-only submission surface."
)

#: The objective's deterministic success signal (docs/adr/0011): the
#: generic runner completes the objective only through deterministic
#: paths — an accepted submission routes the graph DONE.
HALCTF_OBJECTIVE_SUCCESS_HINT = "submission accepted for the challenge"


class HalCTFEnvironment:
    """Full HalCTF runtime environment (V09, docs/adr/0011).

    Args:
        config: The validated runtime configuration; its
            ``target_allowlist`` is the authorized surface and its
            budgets bound the environment-provided services.
        environ: Environment mapping for the HalCTF runtime variables
            (challenge id + endpoint discovery). Defaults to
            ``os.environ``.
        endpoint: Explicit MCP endpoint override; when ``None`` the
            endpoint is discovered from ``environ``
            (:func:`ozzgraph.config.discover_halctf_endpoint`).

    Raises:
        ConfigError: If no MCP endpoint is discoverable — HalCTF mode
            without an endpoint is a configuration error (AGENTS.md
            rule #9, fail loudly).
    """

    def __init__(
        self,
        config: OzzGraphConfig,
        *,
        environ: Mapping[str, str] | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._config = config
        self._environ = os.environ if environ is None else environ
        # V09: the endpoint is REQUIRED — HalCTF mode without a
        # discoverable MCP endpoint cannot reach the platform, so the
        # adapter fails loudly at construction (never a silent
        # mid-run failure). An explicit endpoint wins (tests).
        self._endpoint = (
            endpoint if endpoint is not None else require_halctf_endpoint(self._environ)
        )

    @property
    def endpoint(self) -> str:
        """The resolved HalCTF MCP endpoint this environment drives."""
        return self._endpoint

    @property
    def challenge_id(self) -> str:
        """The configured challenge id (``HAL_CTF_ID`` / ``HAL_CHALLENGE_ID`` /
        legacy ``OZZGRAPH_CHALLENGE_ID``)."""
        return discover_halctf_challenge_id(self._environ)

    # -- environment-provided HalCTF services (V09, docs/adr/0011) ----

    def flag_extractor(
        self,
        *,
        run_id: str,
        event_log: EventLog | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> FlagCandidateExtractor:
        """The provenance-gated flag candidate extractor for this challenge.

        Wired to the environment's config (flag pattern and submission
        attempt budget), so a HalCTF driver extracts candidates with the
        operator's configured policy without importing the adapter's
        internals.
        """
        return FlagCandidateExtractor(
            run_id=run_id,
            event_log=event_log,
            pattern=self._config.flag_pattern,
            max_attempts=self._config.max_submissions,
            artifact_store=artifact_store,
        )

    def submission_coordinator(
        self,
        *,
        client: SubmissionClient,
        run_id: str,
        event_log: EventLog | None = None,
        challenge_id: str | None = None,
        max_submissions: int | None = None,
    ) -> SubmissionCoordinator:
        """The supervisor-only submission coordinator for this challenge.

        ``challenge_id`` defaults to the environment's discovered
        challenge id; ``max_submissions`` defaults to
        ``config.max_submissions`` — the environment wires its own
        scope, so callers never re-derive HalCTF configuration.
        """
        return SubmissionCoordinator(
            client=client,
            run_id=run_id,
            challenge_id=challenge_id or self.challenge_id,
            event_log=event_log,
            max_submissions=max_submissions or self._config.max_submissions,
        )

    def hint_coordinator(
        self,
        *,
        client: HintClient,
        run_id: str,
        event_log: EventLog | None = None,
        challenge_id: str | None = None,
        max_hints: int | None = None,
        policy: HintPolicy | None = None,
    ) -> HintCoordinator:
        """The supervisor-only paid-hint coordinator for this challenge.

        The paid-hint gate enforces the max-paid-hint-count invariant
        (the persisted ``hint_purchase`` count never exceeds
        ``config.max_hints``); the per-hint cost rides the platform's
        :class:`~ozzgraph.hal_client.HintResult`.
        """
        return HintCoordinator(
            client=client,
            run_id=run_id,
            challenge_id=challenge_id or self.challenge_id,
            event_log=event_log,
            max_hints=max_hints or self._config.max_hints,
            policy=policy,
        )

    def scoreboard_coordinator(
        self,
        *,
        client: ScoreboardClient,
        run_id: str,
        event_log: EventLog | None = None,
    ) -> ScoreboardCoordinator:
        """The scoreboard service for this run (read-only, not privileged)."""
        return ScoreboardCoordinator(client=client, run_id=run_id, event_log=event_log)

    # -- EnvironmentAdapter protocol -----------------------------------

    async def discover_scope(self) -> Scope:
        """The authorized surface: the challenge's policy surface.

        Derived from ``config.target_allowlist`` (the same allowlist the
        policy gate enforces); constraints carry the challenge id and
        the ``halctf`` mode so downstream context can render it. No MCP
        I/O.
        """
        return Scope(
            name="halctf",
            hosts=tuple(sorted(self._config.target_allowlist)),
            constraints={"challenge_id": self.challenge_id, "mode": "halctf"},
        )

    async def discover_targets(self) -> list[Target]:
        """Exactly one target: the challenge itself.

        A configured challenge id is required — without it there is
        nothing to assess and discovery fails loudly
        (:class:`~ozzgraph.config.ConfigError`), matching the kernel's
        fail-loud convention (AGENTS.md rule #9). The challenge id is
        discovered from ``HAL_CTF_ID`` / ``HAL_CHALLENGE_ID`` / the
        legacy ``OZZGRAPH_CHALLENGE_ID`` (V09).

        Raises:
            ozzgraph.config.ConfigError: If no challenge id is
                configured.
        """
        challenge_id = self.challenge_id
        if not challenge_id:
            raise ConfigError(
                f"HalCTF environment requires a challenge id to discover the challenge "
                f"target: set {CHALLENGE_ID_ENV} or a HAL_* challenge variable"
            )
        return [
            Target(
                id=f"halctf-challenge-{challenge_id}",
                type="url",
                address=challenge_id,
                metadata={"challenge_id": challenge_id},
            )
        ]

    async def discover_objectives(self) -> list[Objective]:
        """One objective: obtain and submit the flag (an Objective, not a
        kernel phase — the kernel owns no FLAG_HUNT/VERIFY_AND_SUBMIT
        anymore; docs/adr/0008). Its ``success_hint`` names the
        deterministic completion signal (V09): the generic runner
        completes the objective only through deterministic paths (an
        accepted submission routed DONE), so a HalCTF run terminates
        COMPLETED with the V08 report bundle when the flag is accepted.
        """
        return [
            Objective(
                id=HALCTF_OBJECTIVE_ID,
                description=HALCTF_OBJECTIVE_DESCRIPTION,
                success_hint=HALCTF_OBJECTIVE_SUCCESS_HINT,
            )
        ]

    async def discover_capabilities(self) -> set[str]:
        """The conservative capability set until the tool-runtime milestone."""
        return set(DEFAULT_HALCTF_CAPABILITIES)

    async def aclose(self) -> None:
        """No owned resources; idempotent no-op."""
