"""Environment adapter protocol for the OzzGraph generic runtime (V01).

Defines the :class:`EnvironmentAdapter` contract — the v2 pivot
(docs/CHANGES_v2.md): the supervisor and :class:`~ozzgraph.runner.AutonomousRunner`
talk to a runtime environment ONLY through this protocol. A CTF
challenge (HalCTF), a local web app, a Docker Compose stack, a Git
repository, and a vulnerable VM are all just different adapters behind
the same six methods.

Design rules:

- The protocol is deliberately small and async: discovery may require
  I/O (e.g. a HalCTF MCP status call in V09), and the kernel must never
  block on an adapter.
- Discovery is deterministic per environment: the same configuration
  yields the same scope/targets/objectives/capabilities, so routing,
  planning, and the DONE predicate are reproducible.
- ``verdict_satisfies_objectives`` is the environment's objective-
  completion predicate (HAL-006): the runner consults it before
  completing objectives on an evaluator COMPLETE verdict, so completion
  semantics stay environment-specific (local assessment accepts the
  verdict; HalCTF requires an accepted submission in the graph).
- ``discover_capabilities`` is the conservative capability vocabulary
  the harness advertises to the model (``http.request``,
  ``network.probe``, ``filesystem.read``, ...). V03 (tool-runtime)
  turns these into a real ToolInventory; V01 environments return
  conservative static sets.
- The protocol is NOT decorated with ``@runtime_checkable``:
  ``isinstance`` checks on an async protocol only verify that methods
  EXIST, never that they are coroutine functions, so a broken adapter
  could pass a runtime check and then fail loudly mid-run. The harness
  constructs concrete adapters explicitly (no duck-typed injection
  points), and mypy checks the structural contract statically — the
  same convention as :class:`~ozzgraph.bootstrap.ProbeRunner`.
"""

from __future__ import annotations

from typing import Protocol

from ozzgraph.environments.models import Objective, Scope, Target
from ozzgraph.state_graph import StateGraph


class EnvironmentAdapter(Protocol):
    """The runtime-environment contract the kernel drives.

    Implementations are concrete classes (``LocalEnvironment``,
    ``HalCTFEnvironment``, ...); the protocol is the static contract
    mypy enforces. Every method is async and deterministic; ``aclose``
    releases any owned resources and is idempotent.

    Methods:
        discover_scope: The authorized assessment surface.
        discover_targets: The concrete targets within the scope, in
            deterministic order.
        discover_objectives: The run's completion contracts.
        discover_capabilities: The capability vocabulary the
            environment supports (conservative; V03 makes it
            inventory-backed).
        verdict_satisfies_objectives: Whether an evaluator COMPLETE
            verdict (on the given graph state) satisfies the run's
            objectives — the environment-specific completion predicate
            (HAL-006).
        aclose: Release owned resources; never raises.
    """

    async def discover_scope(self) -> Scope: ...

    async def discover_targets(self) -> list[Target]: ...

    async def discover_objectives(self) -> list[Objective]: ...

    async def discover_capabilities(self) -> set[str]: ...

    async def verdict_satisfies_objectives(self, graph: StateGraph) -> bool: ...

    async def aclose(self) -> None: ...
