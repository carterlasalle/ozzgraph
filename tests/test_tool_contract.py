"""Tool-contract tests (V10, docs/BENCHMARKS.md "Tool contract").

The V03 capabilities-not-binaries contract, hardened by V10: every
skill's ``required_capabilities`` must resolve to a WORKING INSTALLED
provider through the tool plane, and a capability whose provider is
unavailable must be detectable and fail loudly (never a silent guess).

The contract has three layers, all asserted here:

1. Vocabulary (static): every capability a skill requires is a
   REGISTERED catalog capability (a skill requiring an unregistered
   capability could never resolve and is a registry bug).
2. Resolution (dynamic, environment-truthful): the real PATH inventory
   is probed, and every skill's required capability must resolve to an
   installed provider — the shipped skill declarations and catalog are
   fixed so this holds in any base environment (curl-backed content
   discovery, no hard sqlmap/searchsploit requirements; see
   docs/CHANGES_v2.md milestone 10).
3. Loud failure: a capability with no installed provider reports
   ``is_resolvable() == False`` and ``ToolProvider.resolve`` raises
   :class:`ToolProviderError` with the capability named — the
   unavailable-provider signal, never a silent pass-through.
"""

from __future__ import annotations

import os
import shutil

import pytest

from ozzgraph.skills import SKILLS, SkillRegistry
from ozzgraph.toolplane import (
    CapabilityRegistry,
    ToolCatalog,
    ToolInventory,
    ToolProvider,
    ToolProviderError,
)


def _provider() -> ToolProvider:
    """The real-environment provider: PATH-probed inventory + catalog."""
    inventory = ToolInventory().run()
    return ToolProvider(CapabilityRegistry(inventory.records))


def _all_required_capabilities() -> dict[str, tuple[str, ...]]:
    """skill_id -> its required capabilities (deterministic registry order)."""
    return {
        skill_id: skill.required_capabilities
        for skill_id, skill in sorted(SKILLS.items())
        if skill.required_capabilities
    }


# ---------------------------------------------------------------------------
# layer 1: vocabulary contract
# ---------------------------------------------------------------------------


def test_every_skill_required_capability_is_registered_in_catalog() -> None:
    """A skill may only require capabilities the catalog knows.

    A capability outside the catalog vocabulary could never be provided
    by any tool — the declaration is a registry bug (AGENTS.md rule #9:
    fail loudly, not silently).
    """
    vocabulary = ToolCatalog().capabilities()
    for skill_id, capabilities in _all_required_capabilities().items():
        unknown = [capability for capability in capabilities if capability not in vocabulary]
        assert not unknown, (
            f"skill {skill_id!r} requires capabilities with no catalog provider: {unknown}"
        )


# ---------------------------------------------------------------------------
# layer 2: full resolution contract
# ---------------------------------------------------------------------------


def test_every_skill_required_capability_resolves_in_this_environment() -> None:
    """Every required capability resolves to an installed provider here.

    This is the V10 tool-contract guarantee (docs/BENCHMARKS.md): the
    shipped skills must be usable in the shipped runtime. The catalog
    and skill declarations are fixed so the guarantee holds in any base
    environment — curl is a registered ``web.content_discovery``
    provider (bounded path probing is curl's own primitive), and the
    sqlmap/searchsploit deep-dive capabilities were removed from the
    skill requirements (the cards keep them as guidance).
    """
    provider = _provider()
    missing = [
        capability
        for skill_id, capabilities in _all_required_capabilities().items()
        for capability in capabilities
        if not provider.is_resolvable(capability)
    ]
    assert not missing, (
        f"skills require capabilities with no installed provider: {missing}; "
        "fix the toolplane catalog or the skill declarations"
    )
    # And every one of them resolves to a concrete executable.
    for skill_id, capabilities in _all_required_capabilities().items():
        for capability in capabilities:
            resolved = provider.resolve(capability)
            assert resolved.capability == capability
            assert resolved.tool_id, skill_id


def test_resolved_provider_is_a_working_installed_executable() -> None:
    """resolve() returns a real, executable binary that is on PATH."""
    provider = _provider()
    for capability in sorted(ToolCatalog().capabilities()):
        if not provider.is_resolvable(capability):
            continue  # unavailable capabilities are covered by the loud test
        resolved = provider.resolve(capability)
        assert os.path.isabs(resolved.path), capability
        assert os.path.isfile(resolved.path), f"{capability}: {resolved.path}"
        assert os.access(resolved.path, os.X_OK), f"{capability}: {resolved.path} not executable"
        assert shutil.which(resolved.binary) is not None, capability


def test_skills_list_available_matches_provider_contract() -> None:
    """With every required capability resolvable, every skill is advertised.

    ``SkillRegistry.list_available`` over the environment's available
    capabilities must include every registered skill — the V03 bridge
    between skills and the tool plane, proven end to end.
    """
    inventory = ToolInventory().run()
    available = inventory.capabilities.available()
    advertised = {summary.skill_id for summary in SkillRegistry().list_available(available)}
    assert advertised == set(SKILLS), (
        f"skills missing from the advertisement: {sorted(set(SKILLS) - advertised)}"
    )


# ---------------------------------------------------------------------------
# layer 3: loud-failure contract
# ---------------------------------------------------------------------------


def test_unavailable_capability_is_detectable_and_fails_loudly() -> None:
    """An unavailable provider is detectable and raises, never guesses.

    Picks a REGISTERED capability with no installed provider in the
    current environment (when every registered capability happens to be
    installed, an unknown capability stands in — the catalog itself
    names it as having no provider). Either way the contract holds:
    ``is_resolvable`` is False and ``resolve`` raises
    :class:`ToolProviderError` naming the capability.
    """
    catalog = ToolCatalog()
    provider = _provider()
    capability = next(
        (
            candidate
            for candidate in sorted(catalog.capabilities())
            if not provider.is_resolvable(candidate)
        ),
        None,
    )
    if capability is not None:
        assert provider.is_resolvable(capability) is False
        with pytest.raises(ToolProviderError, match=capability):
            provider.resolve(capability)
    # The catalog-independent case: a capability the catalog does not
    # even know is reported loudly as unresolvable, never passed through.
    unknown = "capability.that_does_not_exist"
    assert unknown not in catalog.capabilities()
    assert provider.is_resolvable(unknown) is False
    with pytest.raises(ToolProviderError, match=unknown):
        provider.resolve(unknown)
