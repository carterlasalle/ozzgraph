"""Synthetic test lab for OzzGraph (PR27): isolated, deterministic targets.

The lab (docs/SYNTHETIC_LAB.md) provides loopback-only synthetic
challenge targets the harness can be pointed at via ``OZZGRAPH_TARGET``
— one target per category from docs/TESTING_AND_QA.md "Synthetic
Challenge Suite". Everything is stdlib (``http.server`` +
``tempfile``), binds ``127.0.0.1`` only, and cleans up on ``stop()``.

Typical use::

    from ozzgraph.lab import get_target

    with get_target("hidden-routes") as target:
        url = target.target_value  # OZZGRAPH_TARGET=http://127.0.0.1:<port>

Public surface:

- :class:`~ozzgraph.lab.base.SyntheticTarget` — the target protocol/base.
- :data:`~ozzgraph.lab.registry.LAB_REGISTRY` — every target class, in
  stable order.
- :func:`~ozzgraph.lab.registry.get_target` — a fresh, isolated instance
  by name.
- :func:`~ozzgraph.lab.registry.list_targets` — the catalogue (names,
  categories, descriptions; flags intentionally absent).
"""

from ozzgraph.lab.base import (
    FileTreeTarget,
    LabError,
    LoopbackHttpTarget,
    SyntheticTarget,
    lab_flag,
)
from ozzgraph.lab.registry import LAB_REGISTRY, TargetInfo, get_target, list_targets

__all__ = [
    "LAB_REGISTRY",
    "FileTreeTarget",
    "LabError",
    "LoopbackHttpTarget",
    "SyntheticTarget",
    "TargetInfo",
    "get_target",
    "lab_flag",
    "list_targets",
]
