"""Local environment adapter for the OzzGraph generic runtime (V01).

:class:`LocalEnvironment` is the v2 default runtime environment
(docs/CHANGES_v2.md, milestone 8 "local-assessment"): an authorized
assessment of targets the operator configured locally. It builds the
:class:`~ozzgraph.environments.models.Scope`,
:class:`~ozzgraph.environments.models.Target`, and
:class:`~ozzgraph.environments.models.Objective` sets DETERMINISTICALLY
from the validated :class:`~ozzgraph.config.OzzGraphConfig` and the
operator's target environment variables — no model calls, no hidden
state (AGENTS.md: prefer deterministic code).

Derivation rules (all deterministic, documented in docs/adr/0008 and
docs/adr/0010):

- Scope surface: ``config.target_allowlist`` is the single source of
  truth for the authorized surface (it is the SAME allowlist the policy
  gate enforces, so scope data and the gate can never disagree).
  Entries are classified by shape: a URL scheme means ``urls``, a
  parseable CIDR means ``networks``, everything else that is not a
  local path is a ``hosts`` entry. Repository / Docker-Compose paths
  are local surfaces, never network buckets. An empty allowlist yields
  an empty scope — the environment fails closed, exactly like the
  policy gate.
- Targets: ``OZZGRAPH_TARGET`` / ``OZZGRAPH_TARGET_<NS>`` variables are
  parsed with the validated :func:`~ozzgraph.bootstrap.load_targets`
  parser (one source of truth for target syntax; a malformed variable
  raises :class:`~ozzgraph.config.ConfigError` loudly), then each
  address is classified into a local-assessment mode
  (:func:`classify_local_target`): URL -> ``url``, CIDR -> ``network``,
  a path to a git repository (contains ``.git``) -> ``repo``, a path to
  a Docker Compose project (contains a compose file) -> ``compose``,
  everything else -> ``host``. Repository/compose paths are validated
  loudly (``ConfigError`` when the path does not exist or is not the
  expected kind). When no target variables are set, targets fall back
  to the allowlist-derived scope entries. An environment with neither
  yields no targets.
- Scope mode: the scope's ``constraints["mode"]`` is derived from the
  effective target set — one mode when every target shares a type,
  ``hybrid`` when targets span multiple types, ``none`` with no
  targets; ``constraints["target_modes"]`` lists the sorted unique
  modes. Each :class:`Target` carries its own mode in ``metadata``
  (``{"mode": ..., "source": ...}``).
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
from collections.abc import Mapping, Sequence
from pathlib import Path

from ozzgraph.bootstrap import load_targets
from ozzgraph.config import ConfigError, OzzGraphConfig
from ozzgraph.environments.models import Objective, Scope, Target, TargetType
from ozzgraph.state_graph import StateGraph

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

#: Display names for the local-assessment modes (docs/CHANGES_v2.md,
#: milestone 8): the ``Target.type`` literal stays ``repo``/``compose``
#: while the human-facing mode vocabulary is
#: ``url``/``network``/``host``/``repository``/``docker-compose``, with
#: ``hybrid`` for a mixed-type scope.
LOCAL_MODE_NAMES: dict[str, str] = {
    "url": "url",
    "network": "network",
    "host": "host",
    "repo": "repository",
    "compose": "docker-compose",
}

#: URL-scheme marker used to classify allowlist/target entries.
_URL_SCHEME_MARKERS = ("://",)

#: Compose-project markers probed in a path (first hit wins).
_COMPOSE_MARKERS = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yaml",
    "compose.yml",
)


def classify_local_target(address: str) -> TargetType:
    """Classify one target address into a local-assessment mode.

    Deterministic order: a URL scheme marker means ``url``; a parseable
    CIDR means ``network``; a path-like address is validated as a git
    repository (``repo``) or Docker Compose project (``compose``),
    failing loudly when the path does not exist or is not the expected
    kind; anything else (hostname, bare IP, host:port) is ``host``.

    Raises:
        ConfigError: For a path-like address whose path does not exist
            or is neither a git repository nor a compose project
            (AGENTS.md rule #9).
    """
    lowered = address.casefold()
    if any(marker in lowered for marker in _URL_SCHEME_MARKERS):
        return "url"
    try:
        ipaddress.ip_network(lowered, strict=False)
    except ValueError:
        pass
    else:
        # An explicit prefix is a CIDR network; a bare address is a
        # single host (milestone 8 vocabulary: host/IP -> host, CIDR
        # -> network).
        if "/" in lowered:
            return "network"
        return "host"
    if _is_path_like(address):
        return _classify_local_path(address)
    return "host"


def _is_path_like(address: str) -> bool:
    """True when ``address`` is a filesystem path rather than a host.

    Absolute/relative markers (``/``, ``./``, ``../``, ``~``) or an
    embedded path separator count; a bare existing directory also counts
    (so ``ozzgraph run myrepo`` resolves a repository next to the
    process). CIDRs never reach this check (classified earlier).
    """
    if address.startswith(("/", "./", "../", "~")):
        return True
    if "/" in address:
        return True
    return Path(address).expanduser().is_dir()


def _classify_local_path(address: str) -> TargetType:
    """Validate a path-like target as ``repo`` or ``compose`` (loudly).

    Raises:
        ConfigError: If the path does not exist, is not a directory, or
            is neither a git repository nor a Docker Compose project.
    """
    path = Path(address).expanduser()
    if not path.exists():
        raise ConfigError(f"target path {address!r} does not exist")
    if not path.is_dir():
        raise ConfigError(
            f"target path {address!r} is not a directory; expected a git "
            "repository or a Docker Compose project"
        )
    if (path / ".git").exists():
        return "repo"
    if any((path / marker).is_file() for marker in _COMPOSE_MARKERS):
        return "compose"
    markers = " or ".join(_COMPOSE_MARKERS)
    raise ConfigError(
        f"target path {address!r} is neither a git repository (missing .git) "
        f"nor a Docker Compose project (missing {markers})"
    )


def scope_mode(targets: Sequence[Target]) -> str:
    """The scope mode of a target set: one mode, ``hybrid``, or ``none``.

    Deterministic: an empty set is ``none``; a single-type set is that
    type's display name; a multi-type set is ``hybrid``
    (docs/CHANGES_v2.md, milestone 8).
    """
    if not targets:
        return "none"
    kinds = sorted({target.type for target in targets})
    if len(kinds) == 1:
        return LOCAL_MODE_NAMES[kinds[0]]
    return "hybrid"


def _target_id(category: str, address: str) -> str:
    """Deterministic target id: ``target-<category>-<sha256 prefix>``.

    The id is a pure function of the address, so repeated discovery
    yields identical ids and the graph seeding is idempotent.
    """
    digest = hashlib.sha256(address.encode("utf-8")).hexdigest()[:12]
    return f"target-{category}-{digest}"


def _target_for(address: str) -> Target:
    """One :class:`Target` derived from an allowlist/scoped address."""
    category = classify_local_target(address)
    return Target(
        id=_target_id(category, address),
        type=category,
        address=address,
        metadata={"source": "allowlist", "mode": LOCAL_MODE_NAMES[category]},
    )


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
        # Lazily computed effective target set (deterministic; shared by
        # discover_scope's mode derivation and discover_targets).
        self._targets: list[Target] | None = None

    async def discover_scope(self) -> Scope:
        """The authorized surface derived from ``config.target_allowlist``.

        An empty allowlist yields an empty scope (fail closed, matching
        the policy gate). Constraints carry the policy knobs, runtime
        directories, and the scope mode (from the effective target set)
        so downstream context can render the scope fully.
        """
        hosts: list[str] = []
        urls: list[str] = []
        networks: list[str] = []
        for entry in sorted(self._config.target_allowlist):
            category = classify_local_target(entry)
            if category == "url":
                urls.append(entry)
            elif category == "network":
                networks.append(entry)
            elif category == "host":
                hosts.append(entry)
            # repo/compose entries are local paths — never network
            # surface buckets — but they still shape the target set.
        targets = self._derive_targets()
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
                "mode": scope_mode(targets),
                "target_modes": sorted({LOCAL_MODE_NAMES[target.type] for target in targets}),
            },
        )

    async def discover_targets(self) -> list[Target]:
        """The concrete targets, in deterministic order.

        ``OZZGRAPH_TARGET`` / ``OZZGRAPH_TARGET_<NS>`` variables win
        (the operator's explicit pointer); a malformed variable or an
        invalid repository/compose path raises
        :class:`~ozzgraph.config.ConfigError` loudly. Without target
        variables the allowlist-derived scope entries become the
        targets. With neither, no targets exist.
        """
        return list(self._derive_targets())

    async def discover_objectives(self) -> list[Objective]:
        """The single generic assessment objective (V01)."""
        return [Objective(id=LOCAL_OBJECTIVE_ID, description=LOCAL_OBJECTIVE_DESCRIPTION)]

    async def discover_capabilities(self) -> set[str]:
        """The conservative generic capability set until V03."""
        return set(DEFAULT_LOCAL_CAPABILITIES)

    async def verdict_satisfies_objectives(self, graph: StateGraph) -> bool:
        """An evaluator COMPLETE verdict satisfies the local objective.

        Default (``exhaustive`` off): the deterministic COMPLETE
        verdict IS the completion signal, so it satisfies the objective
        unconditionally — the pre-HAL-006 behavior, byte-for-byte
        unchanged. Every finding the run validated before that verdict
        (including specialist-fleet findings from the same batch) is
        rendered by the append-safe store.

        Exhaustive mode (``OZZGRAPH_EXHAUSTIVE=true``): the objective
        is NEVER auto-completed by a verdict — the run keeps probing,
        new observations form new hypotheses, the fleet validates
        them, and findings accumulate until the budget is spent, so
        the whole box gets assessed instead of stopping at the first
        (or few) validated findings. The run terminates with
        ``budget_exhausted`` having rendered every finding it found.
        """
        return not self._config.exhaustive

    async def aclose(self) -> None:
        """No owned resources; idempotent no-op."""

    # ------------------------------------------------------------------
    # deterministic derivation
    # ------------------------------------------------------------------

    def _derive_targets(self) -> list[Target]:
        """The effective target set, computed once and cached.

        Deterministic: the result depends only on the validated
        environment and allowlist, so the cache is observationally
        transparent (repeated discovery returns the same targets).
        """
        if self._targets is None:
            self._targets = self._compute_targets()
        return list(self._targets)

    def _compute_targets(self) -> list[Target]:
        """Derive the target set from the environment, else the allowlist."""
        parsed = load_targets(self._environ)
        targets: list[Target] = []
        seen: set[str] = set()
        for spec in parsed.specs():
            address = spec.value
            if address in seen:
                continue
            seen.add(address)
            # A namespaced http(s) target without a scheme is normalized
            # like bootstrap._normalize_target (one source of truth for
            # target syntax) so it classifies as a URL.
            if spec.category in ("http", "https") and not any(
                marker in address.casefold() for marker in _URL_SCHEME_MARKERS
            ):
                address = f"{spec.category}://{address}"
            category = classify_local_target(address)
            targets.append(
                Target(
                    id=_target_id(category, address),
                    type=category,
                    address=address,
                    metadata={"source": "target_env", "mode": LOCAL_MODE_NAMES[category]},
                )
            )
        if not targets:
            for entry in sorted(self._config.target_allowlist):
                if entry in seen:
                    continue
                seen.add(entry)
                targets.append(_target_for(entry))
        return targets
