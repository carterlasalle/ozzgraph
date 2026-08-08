"""Tests for the V03 tool plane (docs/CHANGES_v2.md milestone 3).

Covers ToolCatalog / ToolInventory / CapabilityRegistry / ToolProvider
and the two V03 invariants they enforce:

- catalog completeness — every capability in the vocabulary has at
  least one catalog provider, and the provider-preference order picks
  the expected tool (curl for ``http.request``, nmap for
  ``network.port_scan``);
- inventory truth — the inventory records path/version for installed
  tools, excludes absent ones (whose capabilities are NEVER
  advertised), and the registry/provider queries round-trip off those
  records; a missing capability raises :class:`ToolProviderError`
  instead of passing through a guess.

Also covers the skill contract (``required_capabilities`` validated
against an inventory in a fake environment, monkeypatched PATH) and the
context advertisement invariant (compiled context lists only available
capabilities).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ozzgraph.context import CompiledContext, ContextRequest, compile_context
from ozzgraph.phases import Phase
from ozzgraph.profiles import GPT_PROFILE
from ozzgraph.skills import SKILLS, Skill, SkillRegistry
from ozzgraph.state_graph import StateGraph
from ozzgraph.toolplane import (
    TOOLS,
    CapabilityRegistry,
    ToolCatalog,
    ToolCatalogError,
    ToolInventory,
    ToolProvider,
    ToolProviderError,
    ToolRecord,
    ToolSpec,
    register_tool,
)

#: Script body for a fake tool whose version probe succeeds.
_FAKE_VERSION_SCRIPT = "#!/bin/sh\necho 'fake 1.0'\n"


def _write_tool(bin_dir: Path, name: str, body: str = _FAKE_VERSION_SCRIPT) -> Path:
    """Write an executable fake tool into ``bin_dir``."""
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_bin_dir(tmp_path: Path, names: list[str]) -> Path:
    """A PATH directory containing the named fake executables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in names:
        _write_tool(bin_dir, name)
    return bin_dir


# ---------------------------------------------------------------------------
# ToolCatalog
# ---------------------------------------------------------------------------


def test_catalog_every_capability_has_at_least_one_provider() -> None:
    """Completeness invariant: no capability exists without a provider.

    The vocabulary is the union of every registered tool's declared
    capabilities, so this documents the invariant a future edit must
    keep (a capability with no catalog provider could never be
    resolved by the tool plane).
    """
    catalog = ToolCatalog()
    assert catalog.capabilities()
    for capability in sorted(catalog.capabilities()):
        providers = catalog.providers_for(capability)
        assert providers, f"capability {capability!r} has no catalog provider"
        for spec in providers:
            assert capability in spec.capabilities


def test_catalog_entries_are_well_formed() -> None:
    """Every tool has a unique id, >=1 binary, >=1 capability."""
    catalog = ToolCatalog()
    assert len(TOOLS) == len(catalog.tools())
    for spec in catalog.tools():
        assert spec.tool_id
        assert spec.binaries
        assert spec.capabilities
        assert catalog.spec(spec.tool_id) is spec
    assert len({spec.tool_id for spec in catalog.tools()}) == len(catalog.tools())


def test_catalog_known_capability_mappings() -> None:
    """The task's exemplar capabilities resolve to the expected tools."""
    catalog = ToolCatalog()
    assert catalog.providers_for("http.request")[0].tool_id == "curl"
    assert catalog.providers_for("network.port_scan")[0].tool_id == "nmap"
    assert {spec.tool_id for spec in catalog.providers_for("ad.kerberos_enum")} == {
        "enum4linux",
        "kerbrute",
    }
    assert {spec.tool_id for spec in catalog.providers_for("source.sast")} == {
        "semgrep",
        "codeql",
        "trivy",
    }
    assert catalog.providers_for("web.content_discovery")[0].tool_id == "ffuf"
    assert {spec.tool_id for spec in catalog.providers_for("secrets.scan")} == {
        "gitleaks",
        "trivy",
    }
    assert "file" in {spec.tool_id for spec in catalog.providers_for("file.analyze")}
    assert "readelf" in {spec.tool_id for spec in catalog.providers_for("binary.analyze")}


def test_catalog_unknown_tool_id_raises() -> None:
    """An unknown tool id fails loudly with the typed error."""
    with pytest.raises(ToolCatalogError, match="no tool registered"):
        ToolCatalog().spec("no-such-tool")
    assert not ToolCatalog().is_registered("no-such-tool")


def test_catalog_duplicate_registration_raises() -> None:
    """Duplicate registration is a loud typed error, not a silent overwrite."""
    with pytest.raises(ToolCatalogError, match="already registered"):
        register_tool(TOOLS["curl"])


def test_catalog_snapshots_without_mutating_module_state() -> None:
    """Catalog instances copy their mapping; the module constant is untouched."""
    custom = ToolSpec(tool_id="custom", binaries=("custom-bin",), capabilities=("file.analyze",))
    catalog = ToolCatalog({"custom": custom})
    assert [spec.tool_id for spec in catalog.tools()] == ["custom"]
    assert not ToolCatalog().is_registered("custom")


# ---------------------------------------------------------------------------
# ToolInventory
# ---------------------------------------------------------------------------


def test_inventory_records_installed_tools_with_path_and_version(tmp_path: Path) -> None:
    """Installed tools get binary/path/version; capabilities are declared."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl", "nmap"])
    inventory = ToolInventory(paths=[str(bin_dir)]).run()

    curl = inventory.record("curl")
    assert curl is not None
    assert curl.installed
    assert curl.binary == "curl"
    assert curl.path == str(bin_dir / "curl")
    assert curl.version == "fake 1.0"
    assert curl.capabilities == ("http.request",)

    nmap = inventory.record("nmap")
    assert nmap is not None
    assert nmap.installed
    assert nmap.path == str(bin_dir / "nmap")
    assert nmap.version == "fake 1.0"
    assert nmap.capabilities == ("network.port_scan", "network.service_detect")


def test_inventory_excludes_absent_tools_and_never_advertises_their_capabilities(
    tmp_path: Path,
) -> None:
    """Absent tools are recorded absent with NO capabilities."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl"])
    inventory = ToolInventory(paths=[str(bin_dir)]).run()

    absent = inventory.record("ffuf")
    assert absent is not None
    assert not absent.installed
    assert absent.binary is None
    assert absent.path is None
    assert absent.version is None
    assert absent.capabilities == ()

    available = inventory.capabilities.available()
    assert available == frozenset({"http.request"})
    # The absent tool's capabilities never leak into the advertised set.
    assert "web.content_discovery" not in available
    assert "network.port_scan" not in available


def test_inventory_hermetic_empty_path_finds_nothing(tmp_path: Path) -> None:
    """An empty search path records every tool absent (hermetic mode)."""
    inventory = ToolInventory(paths=()).run()
    assert inventory.capabilities.available() == frozenset()
    for record in inventory.records.values():
        assert not record.installed


def test_inventory_version_probe_failure_records_none(tmp_path: Path) -> None:
    """A found binary whose banner cannot be probed keeps version=None.

    A nonzero probe exit yields no version even when the binary prints
    output: an erroring invocation's stderr is not a version banner.
    """
    bin_dir = _fake_bin_dir(tmp_path, [])
    _write_tool(bin_dir, "curl", "#!/bin/sh\necho 'boom'\nexit 3\n")
    inventory = ToolInventory(paths=[str(bin_dir)]).run()
    curl = inventory.record("curl")
    assert curl is not None
    assert curl.installed  # the PATH lookup is the truth
    assert curl.version is None  # the banner is informational


def test_inventory_skips_probe_for_tools_without_version_banner(tmp_path: Path) -> None:
    """Tools with no version flag are never spawned bare.

    ``nc`` (and friends without a reliable banner) declare empty
    ``version_flags``: the inventory records them installed with
    ``version=None`` and MUST NOT execute them (invoking nc bare prints
    usage junk — or blocks on stdin). A marker file proves no spawn.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "probed"
    nc = bin_dir / "nc"
    nc.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    nc.chmod(0o755)
    inventory = ToolInventory(paths=[str(bin_dir)]).run()
    record = inventory.record("nc")
    assert record is not None
    assert record.installed
    assert record.version is None
    assert not marker.exists(), "the banner-less tool must never be spawned"


def test_inventory_probing_is_idempotent(tmp_path: Path) -> None:
    """run() twice is a no-op (deterministic startup wiring)."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl"])
    inventory = ToolInventory(paths=[str(bin_dir)])
    inventory.run()
    version_after_first = inventory.record("curl")
    assert version_after_first is not None
    inventory.run()
    assert inventory.record("curl") == version_after_first


def test_inventory_uses_monkeypatched_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With paths=None the inventory reads the environment's PATH."""
    bin_dir = _fake_bin_dir(tmp_path, ["dig"])
    monkeypatch.setenv("PATH", str(bin_dir))
    inventory = ToolInventory().run()
    dig = inventory.record("dig")
    assert dig is not None and dig.installed
    assert inventory.capabilities.available() == frozenset({"dns.lookup"})


def test_inventory_catalog_order_defines_preference(tmp_path: Path) -> None:
    """First-found binary wins; records keep catalog order."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl", "wget"])
    inventory = ToolInventory(paths=[str(bin_dir)]).run()
    assert [record.tool_id for record in inventory.records.values()] == list(TOOLS)


# ---------------------------------------------------------------------------
# CapabilityRegistry
# ---------------------------------------------------------------------------


def test_capability_registry_round_trips(tmp_path: Path) -> None:
    """capabilities_for_binary / providers_for_capability / is_available."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl", "nmap"])
    registry = ToolInventory(paths=[str(bin_dir)]).run().capabilities

    assert registry.is_available("http.request")
    assert registry.is_available("network.port_scan")
    assert not registry.is_available("web.content_discovery")
    assert registry.providers_for("http.request") == ("curl",)
    assert registry.providers_for("network.port_scan") == ("nmap",)
    assert registry.capabilities_for_binary("curl") == ("http.request",)
    assert set(registry.capabilities_for_binary("nmap")) == {
        "network.port_scan",
        "network.service_detect",
    }
    assert registry.capabilities_for_binary("ffuf") == ()
    assert registry.record("nmap") is not None
    assert registry.record("no-such-tool") is None


def test_capability_registry_can_be_built_directly_from_records() -> None:
    """The registry is derived from inventory records, not an inventory."""
    records = {
        "curl": ToolRecord(
            tool_id="curl", binary="curl", path="/bin/curl", capabilities=("http.request",)
        ),
    }
    registry = CapabilityRegistry(records)
    assert registry.available() == frozenset({"http.request"})
    assert registry.providers_for("http.request") == ("curl",)


# ---------------------------------------------------------------------------
# ToolProvider
# ---------------------------------------------------------------------------


def test_tool_provider_resolves_best_installed_provider(tmp_path: Path) -> None:
    """curl wins over wget for http.request; nmap for network.port_scan."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl", "wget", "nmap"])
    inventory = ToolInventory(paths=[str(bin_dir)]).run()
    provider = ToolProvider(inventory.capabilities)

    resolved = provider.resolve("http.request")
    assert resolved.tool_id == "curl"
    assert resolved.capability == "http.request"
    assert resolved.binary == "curl"
    assert resolved.path == str(bin_dir / "curl")
    assert resolved.version == "fake 1.0"

    port_scan = provider.resolve("network.port_scan")
    assert port_scan.tool_id == "nmap"
    assert port_scan.path == str(bin_dir / "nmap")

    assert provider.is_resolvable("http.request")
    assert not provider.is_resolvable("web.content_discovery")


def test_tool_provider_skips_absent_catalog_preference(tmp_path: Path) -> None:
    """When the preferred provider is absent the next installed one wins."""
    bin_dir = _fake_bin_dir(tmp_path, ["wget"])
    provider = ToolProvider(ToolInventory(paths=[str(bin_dir)]).run().capabilities)
    resolved = provider.resolve("http.request")
    assert resolved.tool_id == "wget"


def test_tool_provider_missing_capability_raises_loudly(tmp_path: Path) -> None:
    """No installed provider -> typed error naming the catalog providers."""
    bin_dir = _fake_bin_dir(tmp_path, ["curl"])
    provider = ToolProvider(ToolInventory(paths=[str(bin_dir)]).run().capabilities)
    with pytest.raises(ToolProviderError, match="web.content_discovery") as excinfo:
        provider.resolve("web.content_discovery")
    assert "ffuf" in str(excinfo.value)  # the catalog providers are named


def test_tool_provider_unknown_capability_raises_loudly(tmp_path: Path) -> None:
    """A capability outside the catalog vocabulary is never a guess."""
    provider = ToolProvider(ToolInventory(paths=[str(tmp_path)]).run().capabilities)
    with pytest.raises(ToolProviderError, match="not a catalog capability"):
        provider.resolve("no.such.capability")


# ---------------------------------------------------------------------------
# Skill contract: required_capabilities validated against the inventory
# ---------------------------------------------------------------------------


def _full_skill_toolset(tmp_path: Path) -> Path:
    """Every capability every registered skill requires, as fake tools."""
    return _fake_bin_dir(
        tmp_path,
        [
            "curl",
            "openssl",
            "dig",
            "ffuf",
            "nc",
            "ss",
            "grep",
            "searchsploit",
            "sqlmap",
            "base64",
        ],
    )


def test_every_skill_required_capabilities_are_catalog_capabilities() -> None:
    """The tool-contract seed: skill requirements never name unknown ids."""
    vocabulary = ToolCatalog().capabilities()
    for skill in SKILLS.values():
        unknown = set(skill.required_capabilities) - vocabulary
        assert not unknown, f"skill {skill.skill_id!r} requires unknown capabilities: {unknown}"


def test_skill_required_capabilities_validated_against_fake_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skills advertise only when EVERY required capability is installed."""
    bin_dir = _full_skill_toolset(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    registry = SkillRegistry()

    # With the full toolset every registered skill is available.
    available = ToolInventory().run().capabilities.available()
    all_ids = {summary.skill_id for summary in registry.list_available(available)}
    assert all_ids == set(SKILLS)

    # Drop sqlmap -> only the SQL-injection skill becomes unavailable.
    (bin_dir / "sqlmap").unlink()
    monkeypatch.setenv("PATH", str(bin_dir))
    reduced = ToolInventory().run().capabilities.available()
    reduced_ids = {summary.skill_id for summary in registry.list_available(reduced)}
    assert reduced_ids == set(SKILLS) - {"exploit_parameter_injection"}

    # Drop everything -> no skill with requirements is advertised.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    none_available = ToolInventory().run().capabilities.available()
    assert none_available == frozenset()
    assert registry.list_available(none_available) == []


def test_skill_without_required_capabilities_is_always_available() -> None:
    """A skill declaring no requirements is never filtered out."""
    skill = Skill(
        skill_id="noreq",
        name="NoReq",
        phases=(Phase.RECON,),
        description="no requirements",
        card="card",
        timeout_seconds=10,
        required_capabilities=(),
    )
    registry = SkillRegistry({"noreq": skill})
    assert [s.skill_id for s in registry.list_available(())] == ["noreq"]


def test_skill_required_capabilities_are_normalized() -> None:
    """Duplicate requirements are dropped and the tuple is sorted."""
    skill = Skill(
        skill_id="norm",
        name="Norm",
        phases=(Phase.RECON,),
        description="normalized requirements",
        card="card",
        timeout_seconds=10,
        required_capabilities=("web.content_discovery", "http.request", "http.request"),
    )
    assert skill.required_capabilities == ("http.request", "web.content_discovery")


# ---------------------------------------------------------------------------
# Context advertisement: only available capabilities reach the model
# ---------------------------------------------------------------------------


async def _compile_empty(path: Path, capabilities: tuple[str, ...]) -> CompiledContext:
    """Compile a context over an empty graph with the given advertisement."""
    async with StateGraph(path) as graph:
        return await compile_context(
            graph,
            GPT_PROFILE,
            ContextRequest(mission="M", capabilities=capabilities),
        )


@pytest.mark.asyncio
async def test_context_advertisement_only_includes_available_capabilities(
    tmp_path: Path,
) -> None:
    """The compiled context lists exactly the advertised (available) set."""
    compiled = await _compile_empty(
        tmp_path / "graph.db", capabilities=("http.request", "network.port_scan")
    )
    assert compiled.capabilities == ("http.request", "network.port_scan")
    summary = compiled.graph_summary
    assert "AVAILABLE CAPABILITIES\n- http.request\n- network.port_scan\n" in summary
    # A capability that is NOT available is never advertised.
    assert "web.content_discovery" not in summary
    assert "secrets.scan" not in summary
    assert compiled.used_chars <= GPT_PROFILE.context_soft_limit


@pytest.mark.asyncio
async def test_context_advertisement_is_sorted_and_deduplicated(tmp_path: Path) -> None:
    """Render order is deterministic regardless of the caller's input order."""
    compiled = await _compile_empty(
        tmp_path / "graph.db",
        capabilities=("network.port_scan", "http.request", "network.port_scan"),
    )
    assert compiled.capabilities == ("http.request", "network.port_scan")
    assert compiled.graph_summary.index("http.request") < compiled.graph_summary.index(
        "network.port_scan"
    )


@pytest.mark.asyncio
async def test_context_advertisement_empty_renders_nothing(tmp_path: Path) -> None:
    """No available capabilities -> no capabilities section at all."""
    compiled = await _compile_empty(tmp_path / "graph.db", capabilities=())
    assert compiled.capabilities == ()
    assert "AVAILABLE CAPABILITIES" not in compiled.graph_summary
    # The empty-graph projection shape is unchanged (no extra overhead).
    assert compiled.graph_summary == "PROJECTED ENTITIES\n(none)\nPROJECTED EDGES\n(none)\n"
