"""Deterministic registry for the synthetic test lab (PR27).

``LAB_REGISTRY`` lists every target class in a fixed, stable order
(matching docs/TESTING_AND_QA.md "Synthetic Challenge Suite" and
docs/SYNTHETIC_LAB.md). ``get_target(name)`` returns a FRESH instance
per call so tests and solves are isolated from each other; the
registry itself carries no state and never exposes flags.
"""

from __future__ import annotations

from dataclasses import dataclass

from ozzgraph.lab.base import LabError, SyntheticTarget
from ozzgraph.lab.targets import (
    AuthLogicTarget,
    BinaryStringsTarget,
    CredentialReuseTarget,
    FileForensicsTarget,
    HiddenRoutesTarget,
    HttpReconTarget,
    MultiStageTarget,
    NetworkPivotTarget,
    SourceVulnTarget,
)

#: Every lab target, in stable catalogue order (one per challenge
#: category from docs/TESTING_AND_QA.md). Registry order is part of the
#: lab's determinism contract: tests assert it, docs mirror it.
LAB_REGISTRY: tuple[type[SyntheticTarget], ...] = (
    HttpReconTarget,
    HiddenRoutesTarget,
    AuthLogicTarget,
    SourceVulnTarget,
    FileForensicsTarget,
    BinaryStringsTarget,
    CredentialReuseTarget,
    NetworkPivotTarget,
    MultiStageTarget,
)


@dataclass(frozen=True)
class TargetInfo:
    """Read-only identity of one registered target (never the flag).

    Attributes:
        name: Registry key, usable with :func:`get_target`.
        category: The challenge category (docs/TESTING_AND_QA.md).
        description: Human-readable description of the challenge.
    """

    name: str
    category: str
    description: str


def get_target(name: str) -> SyntheticTarget:
    """A fresh instance of the named target (isolated per call).

    Args:
        name: A registered target name (``target.name``).

    Raises:
        LabError: If ``name`` is not a registered target (fail loudly,
            AGENTS.md rule #9).

    Returns:
        A newly constructed, not-yet-started target. Call ``start()``
        (or use it as a context manager) before reading
        ``target_value``.
    """
    for target_class in LAB_REGISTRY:
        if target_class.name == name:
            return target_class()
    available = ", ".join(target_class.name for target_class in LAB_REGISTRY)
    raise LabError(f"unknown synthetic target {name!r}; available: {available}")


def list_targets() -> tuple[TargetInfo, ...]:
    """Identity of every registered target, in registry order.

    Flags are intentionally absent: the catalogue describes the
    challenges, it never spoils them.
    """
    return tuple(
        TargetInfo(
            name=target_class.name,
            category=target_class.category,
            description=target_class.description,
        )
        for target_class in LAB_REGISTRY
    )
