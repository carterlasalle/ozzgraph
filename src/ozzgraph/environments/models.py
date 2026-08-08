"""Environment models for the OzzGraph generic runtime (V01).

Defines the typed contracts an :class:`~ozzgraph.environments.base.EnvironmentAdapter`
returns: the authorized :class:`Scope`, the discovered :class:`Target` set,
and the run's :class:`Objective` set. Pydantic v2 with ``extra="forbid"``
so a malformed adapter payload fails loudly (AGENTS.md rule #9) instead of
silently carrying unknown fields into the kernel.

These models are the v2 pivot (docs/CHANGES_v2.md): the harness is a
general security-research runtime and a CTF challenge is just one
environment's target set. The models deliberately carry no HalCTF or
flag semantics — objectives are plain, typed completion contracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The target kinds the generic runtime understands (docs/CHANGES_v2.md,
#: milestone 8 "local-assessment": URL/network/repository/Docker-Compose
#: modes, plus host for a bare hostname/IP).
TargetType = Literal["url", "host", "network", "repo", "compose"]


class Scope(BaseModel):
    """The authorized assessment surface of one run.

    Everything the harness may touch, expressed as data (never prose):
    the named scope, the authorized hosts/URLs/networks, references to
    credentials the operator supplied, and any constraints (e.g. the
    policy gate's command families, command-length ceiling, or runtime
    directories). ``credentials`` carries references only — actual
    secrets never enter environment models.

    Attributes:
        name: Stable scope name (e.g. ``"local"`` or ``"halctf"``).
        hosts: Authorized hostnames / bare IPs.
        urls: Authorized URLs (with scheme).
        networks: Authorized CIDR networks.
        credentials: References (ids/names) to operator-supplied
            credentials, never the credentials themselves.
        constraints: Bounded, JSON-serializable constraint data.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    hosts: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    constraints: dict[str, object] = Field(default_factory=dict)


class Target(BaseModel):
    """One discovered assessment target within the authorized scope.

    Attributes:
        id: Deterministic, run-scoped target id (e.g.
            ``target-http-<sha256 prefix>``).
        type: The target kind — one of :data:`TargetType`.
        address: The concrete address the harness assesses (a URL, a
            hostname, a CIDR, a repository path, or a compose project).
        metadata: Bounded, JSON-serializable sidecar data (e.g. the
            target's challenge id for a HalCTF environment).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    type: TargetType
    address: str = Field(min_length=1, max_length=2048)
    metadata: dict[str, object] = Field(default_factory=dict)


class Objective(BaseModel):
    """One completion contract for the run.

    The generic kernel's DONE predicate (docs/adr/0008) is "every
    objective completed": the router reads ``objective`` graph entities
    carrying this shape, and the runner marks an objective completed
    (flipping ``completed`` and stamping ``completed_at``) only through
    deterministic evidence paths — never because a model claimed it.

    Attributes:
        id: Deterministic, run-scoped objective id.
        description: Bounded statement of what completing the objective
            means.
        success_hint: Optional deterministic success signal (e.g. a
            submission-accepted condition) — guidance for evaluators,
            never a kernel phase.
        completed: Whether the objective is satisfied.
        completed_at: UTC timestamp of completion, None until completed.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    success_hint: str | None = Field(default=None, max_length=512)
    completed: bool = False
    completed_at: datetime | None = None
