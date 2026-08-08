"""Runtime environment adapters for the OzzGraph generic runtime (V01).

The v2 pivot (docs/CHANGES_v2.md): OzzGraph is a general autonomous
security-research harness that supports HalCTF as ONE runtime adapter.
This package defines the environment contract
(:class:`~ozzgraph.environments.base.EnvironmentAdapter`) and the V01
concrete adapters:

- :class:`~ozzgraph.environments.local.LocalEnvironment` — deterministic
  local assessment scope derived from configuration (milestone 8).
- :class:`~ozzgraph.environments.halctf.HalCTFEnvironment` — MINIMAL
  HalCTF adapter (milestone 9 lands the full one).

The kernel (supervisor, runner, router) never imports HalCTF/CTF
concepts directly; it drives environments through the protocol.
"""

from __future__ import annotations

from ozzgraph.environments.base import EnvironmentAdapter
from ozzgraph.environments.halctf import HalCTFEnvironment
from ozzgraph.environments.local import LocalEnvironment
from ozzgraph.environments.models import Objective, Scope, Target

__all__ = [
    "EnvironmentAdapter",
    "HalCTFEnvironment",
    "LocalEnvironment",
    "Objective",
    "Scope",
    "Target",
]
