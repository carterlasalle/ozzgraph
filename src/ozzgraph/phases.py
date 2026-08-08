"""Graph phases for the OzzGraph phase router (PR17, V01 generic runtime).

Defines :class:`Phase`: the eight supported graph phases in canonical
order (docs/ARCHITECTURE.md, "Phase Router"). The enum is the single
phase vocabulary shared by the skill registry (:mod:`ozzgraph.skills`)
and the graph-driven phase router (PR18) — and its VALUES are
exactly the uppercase phase names the policy gate already uses
(:data:`ozzgraph.policy.PHASES`, the ``phase="..."`` argument of
:meth:`ozzgraph.policy.ScopePolicy.check`), so a :class:`Phase` member
compares equal to the phase strings already flowing through
:mod:`ozzgraph.policy` and :mod:`ozzgraph.context` without any string
translation.

V01 (docs/adr/0008): FLAG_HUNT and VERIFY_AND_SUBMIT were removed from
the generic kernel — the kernel is a general security-research runtime
whose phases end at the generic lifecycle
(BOOTSTRAP/RECON/ENUMERATION/EXPLOITATION/POST_EXPLOITATION/PIVOT/
REPLAN/DONE). Flag hunting and submission are HalCTF behaviors owned by
the halctf environment adapter (full adapter in V09); the generic DONE
predicate is "all objectives completed".

Design rules:

- Deterministic: member order is the canonical ARCHITECTURE.md order,
  and iterating :class:`Phase` yields that same order. Callers that
  render phases (e.g. skill summaries) sort by this definition order.
- No hidden state: the enum carries no behavior beyond its values.
"""

from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    """One supported graph phase, in canonical ARCHITECTURE.md order.

    Iterating :class:`Phase` yields BOOTSTRAP, RECON, ENUMERATION,
    EXPLOITATION, POST_EXPLOITATION, PIVOT, REPLAN, DONE — the exact
    order of docs/ARCHITECTURE.md ("Phase Router"). Values are the
    uppercase names the policy gate uses, so ``Phase.RECON == "RECON"``
    and a member can be passed to
    :meth:`ozzgraph.policy.ScopePolicy.check` and
    :class:`ozzgraph.context.ContextRequest` without conversion.
    """

    BOOTSTRAP = "BOOTSTRAP"
    RECON = "RECON"
    ENUMERATION = "ENUMERATION"
    EXPLOITATION = "EXPLOITATION"
    POST_EXPLOITATION = "POST_EXPLOITATION"
    PIVOT = "PIVOT"
    REPLAN = "REPLAN"
    DONE = "DONE"
