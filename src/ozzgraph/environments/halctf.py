"""HalCTF environment adapter — MINIMAL V01 slice (V09 is the full adapter).

:class:`HalCTFEnvironment` is the HalCTF runtime environment behind the
generic :class:`~ozzgraph.environments.base.EnvironmentAdapter` protocol
(docs/CHANGES_v2.md, milestone 9 "halctf-adapter"). V01 deliberately
keeps it MINIMAL: it reads the operator's configuration (the same
``OZZGRAPH_*`` / ``HAL_*`` environment the kernel already reads) and
expresses the challenge as ONE :class:`~ozzgraph.environments.models.Target`
and the objective of obtaining the flag as ONE
:class:`~ozzgraph.environments.models.Objective` — a plain completion
contract, NOT a kernel phase (the kernel phases no longer contain
FLAG_HUNT / VERIFY_AND_SUBMIT; docs/adr/0008).

Deliberately NOT ported in V01 (they arrive with V09's full HalCTF
adapter): scoreboard access, hint purchasing, flag submission, smoke
flags, and challenge-status retrieval. The environment performs NO I/O
in V01 — it derives everything from configuration, so discovery is
deterministic and testable without an MCP server.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments.models import Objective, Scope, Target
from ozzgraph.halctl import CHALLENGE_ID_ENV

#: Conservative capability vocabulary for a HalCTF challenge (V01);
#: V03 replaces these static sets with a real ToolInventory.
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


class HalCTFEnvironment:
    """Minimal HalCTF runtime environment (V01; full adapter is V09).

    Args:
        config: The validated runtime configuration; its
            ``target_allowlist`` is the authorized surface.
        environ: Environment mapping for ``OZZGRAPH_CHALLENGE_ID``.
            Defaults to ``os.environ``.
    """

    def __init__(
        self,
        config: OzzGraphConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._environ = os.environ if environ is None else environ

    @property
    def challenge_id(self) -> str:
        """The configured challenge id (``OZZGRAPH_CHALLENGE_ID``)."""
        return self._environ.get(CHALLENGE_ID_ENV, "").strip()

    async def discover_scope(self) -> Scope:
        """The authorized surface: the challenge's policy surface.

        V01 derives the surface from ``config.target_allowlist`` (the
        same allowlist the policy gate enforces); constraints carry the
        challenge id so downstream context can render it. No MCP I/O.
        """
        return Scope(
            name="halctf",
            hosts=tuple(sorted(self._config.target_allowlist)),
            constraints={"challenge_id": self.challenge_id},
        )

    async def discover_targets(self) -> list[Target]:
        """Exactly one target: the challenge itself.

        A configured ``OZZGRAPH_CHALLENGE_ID`` is required — without it
        there is nothing to assess and discovery fails loudly
        (:class:`~ozzgraph.config.ConfigError`), matching the kernel's
        fail-loud convention (AGENTS.md rule #9).

        Raises:
            ozzgraph.config.ConfigError: If ``OZZGRAPH_CHALLENGE_ID``
                is not configured.
        """
        challenge_id = self.challenge_id
        if not challenge_id:
            from ozzgraph.config import ConfigError

            raise ConfigError(
                f"HalCTF environment requires {CHALLENGE_ID_ENV} to discover the challenge target"
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
        anymore; docs/adr/0008)."""
        return [Objective(id=HALCTF_OBJECTIVE_ID, description=HALCTF_OBJECTIVE_DESCRIPTION)]

    async def discover_capabilities(self) -> set[str]:
        """The conservative capability set until V03's tool-runtime."""
        return set(DEFAULT_HALCTF_CAPABILITIES)

    async def aclose(self) -> None:
        """No owned resources; idempotent no-op."""
