"""Local environment adapter for the OzzGraph generic runtime (V01).

:class:`LocalEnvironment` is the v2 default runtime environment
(docs/CHANGES_v2.md, milestone 8 "local-assessment"): an authorized
assessment of targets the operator configured locally. It builds the
:class:`~ozzgraph.environments.models.Scope`,
:class:`~ozzgraph.environments.models.Target`, and
:class:`~ozzgraph.environments.models.Objective` sets DETERMINISTICALLY
from the validated :class:`~ozzgraph.config.OzzGraphConfig` and the
operator's target environment variables — no I/O, no model calls, no
hidden state (AGENTS.md: prefer deterministic code).

Derivation rules (all deterministic, documented in docs/adr/0008):

- Scope surface: ``config.target_allowlist`` is the single source of
  truth for the authorized surface (it is the SAME allowlist the policy
  gate enforces, so scope data and the gate can never disagree).
  Entries are classified by shape: a URL scheme means ``urls``, a
  parseable CIDR means ``networks``, everything else is a ``hosts``
  entry. An empty allowlist yields an empty scope — the environment
  fails closed, exactly like the policy gate.
- Targets: ``OZZGRAPH_TARGET`` / ``OZZGRAPH_TARGET_<NS>`` variables are
  parsed with the validated :func:`~ozzgraph.bootstrap.load_targets`
  parser (one source of truth for target syntax; a malformed variable
  raises :class:`~ozzgraph.config.ConfigError` loudly). When no target
  variables are set, targets fall back to the allowlist-derived scope
  entries. An environment with neither yields no targets.
- Objectives: exactly one generic objective — complete the authorized
  assessment and produce validated, evidence-backed findings. (V02
  turns this into per-target objectives; V01 keeps the kernel small.)
- Capabilities: the conservative generic set
  :data:`DEFAULT_LOCAL_CAPABILITIES` until V03's tool-runtime
  inventories real tools.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from collections.abc import Mapping

from ozzgraph.bootstrap import load_targets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments.models import Objective, Scope, Target

#: Conservative generic capability vocabulary (docs/CHANGES_v2.md,
#: "Key technical changes"): what the local environment can do until
#: V03 replaces these static sets with a real ToolInventory.
DEFAULT_LOCAL_CAPABILITIES: frozenset[str] = frozenset(
    {"http.request", "network.probe", "filesystem.read"}
)

#: Objective id for the single generic local objective.
LOCAL_OBJECTIVE_ID = "objective-local-1"

#: Bounded description of the generic local objective.
LOCAL_OBJECTIVE_DESCRIPTION = (
    "Complete the authorized assessment of the scoped targets and produce "
    "validated findings with evidence."
)

#: URL-scheme marker used to classify allowlist entries.
_URL_SCHEME_MARKERS = ("://",)


def _classify_surface(entry: str) -> str:
    """Classify one allowlist entry as ``url``, ``network``, or ``host``.

    Deterministic: a URL scheme marker means ``url``; a parseable CIDR
    means ``network``; anything else (hostname, bare IP) is a ``host``.
    """
    lowered = entry.casefold()
    if any(marker in lowered for marker in _URL_SCHEME_MARKERS):
        return "url"
    try:
        ipaddress.ip_network(lowered, strict=False)
    except ValueError:
        return "host"
    return "network"


def _target_id(category: str, address: str) -> str:
    """Deterministic target id: ``target-<category>-<sha256 prefix>``.

    The id is a pure function of the address, so repeated discovery
    yields identical ids and the graph seeding is idempotent.
    """
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()[:12]
    return f"target-{category}-{digest}"


def _target_for(address: str) -> Target:
    """One :class:`Target` derived from an allowlist/scoped address."""
    category = _classify_surface(address)
    if category == "url":
        return Target(id=_target_id("url", address), type="url", address=address)
    if category == "network":
        return Target(id=_target_id("network", address), type="network", address=address)
    return Target(id=_target_id("host", address), type="host", address=address)


class LocalEnvironment:
    """Deterministic local assessment environment (V01).

    Args:
        config: The validated runtime configuration; its
            ``target_allowlist`` is the authorized surface and the
            source of scope/target data.
        environ: Environment mapping for ``OZZGRAPH_TARGET*`` target
            variables. Defaults to ``os.environ``.
    """

    def __init__(
        self,
        config: OzzGraphConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._environ = os.environ if environ is None else environ

    async def discover_scope(self) -> Scope:
        """The authorized surface derived from ``config.target_allowlist``.

        An empty allowlist yields an empty scope (fail closed, matching
        the policy gate). Constraints carry the policy knobs and runtime
        directories so downstream context can render the scope fully.
        """
        hosts: list[str] = []
        urls: list[str] = []
        networks: list[str] = []
        for entry in sorted(self._config.target_allowlist):
            category = _classify_surface(entry)
            if category == "url":
                urls.append(entry)
            elif category == "network":
                networks.append(entry)
            else:
                hosts.append(entry)
        return Scope(
            name="local",
            hosts=tuple(hosts),
            urls=tuple(urls),
            networks=tuple(networks),
            constraints={
                "state_dir": str(self._config.state_dir),
                "artifact_dir": str(self._config.artifact_dir),
                "allowed_command_families": list(self._config.allowed_command_families),
                "max_command_length": self._config.max_command_length,
            },
        )

    async def discover_targets(self) -> list[Target]:
        """The concrete targets, in deterministic order.

        ``OZZGRAPH_TARGET`` / ``OZZGRAPH_TARGET_<NS>`` variables win
        (the operator's explicit pointer); a malformed variable raises
        :class:`~ozzgraph.config.ConfigError` loudly. Without target
        variables the allowlist-derived scope entries become the
        targets. With neither, no targets exist.
        """
        parsed = load_targets(self._environ)
        targets: list[Target] = []
        seen: set[str] = set()
        for spec in parsed.specs():
            address = spec.value
            if address in seen:
                continue
            seen.add(address)
            if spec.category in ("http", "https"):
                targets.append(
                    Target(
                        id=_target_id("url", address),
                        type="url",
                        address=address,
                        metadata={"source": "target_env"},
                    )
                )
            else:
                targets.append(
                    Target(
                        id=_target_id("host", address),
                        type="host",
                        address=address,
                        metadata={"source": "target_env"},
                    )
                )
        if not targets:
            for entry in sorted(self._config.target_allowlist):
                if entry in seen:
                    continue
                seen.add(entry)
                targets.append(_target_for(entry))
        return targets

    async def discover_objectives(self) -> list[Objective]:
        """The single generic assessment objective (V01)."""
        return [Objective(id=LOCAL_OBJECTIVE_ID, description=LOCAL_OBJECTIVE_DESCRIPTION)]

    async def discover_capabilities(self) -> set[str]:
        """The conservative generic capability set until V03."""
        return set(DEFAULT_LOCAL_CAPABILITIES)

    async def aclose(self) -> None:
        """No owned resources; idempotent no-op."""
