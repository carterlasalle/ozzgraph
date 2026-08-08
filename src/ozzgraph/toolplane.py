"""Tool plane for OzzGraph (V03, docs/CHANGES_v2.md milestone 3 "tool-runtime").

The TOOL PLANE is the deterministic bridge between the model's
capability vocabulary and the executables actually installed in the
runtime environment (docs/CHANGES_v2.md, "Key technical changes" ->
ToolInventory). Four components:

- :data:`TOOLS` / :class:`ToolCatalog` — the static registry of known
  tools. Every entry declares the first-class capability ids it
  provides (``http.request``, ``network.port_scan``,
  ``ad.kerberos_enum``, ``source.sast``, ``web.content_discovery``,
  ``file.analyze``, ``binary.analyze``, ``secrets.scan``, ...) and the
  binary name(s) that provide them.
- :class:`ToolInventory` — the startup inventory: deterministically
  probes the environment (PATH lookup + version probe) for every
  catalog tool and records path/version/capabilities. Absent tools are
  recorded as absent and their capabilities are NEVER advertised.
- :class:`CapabilityRegistry` — the set of capabilities actually
  available in the current environment, derived from the inventory;
  answers ``capabilities_for_binary``, ``providers_for_capability``,
  and ``is_available``.
- :class:`ToolProvider` — intent/requirement -> concrete executable
  resolution: given a requested capability, return the best installed
  provider (e.g. curl for ``http.request``, nmap for
  ``network.port_scan``). Raises :class:`ToolProviderError` when no
  installed provider exists — it never silently passes through a guess.

Design rules (AGENTS.md):

- Deterministic: the catalog is a static module-level registry (the
  same shape as :data:`ozzgraph.skills.SKILLS`), inventory records are
  keyed by ``tool_id`` in catalog order (catalog order IS the
  provider-preference order), and every query returns catalog-ordered
  results. No set-iteration order leaks into output.
- Fail loudly (AGENTS.md rule #9): resolving a capability with no
  installed provider raises :class:`ToolProviderError`, and an unknown
  tool id raises :class:`ToolCatalogError`. The inventory itself never
  raises for a probe failure — a found binary with an unprobeable
  version is recorded with ``version=None`` (the truth is the PATH
  lookup, not the banner).
- No dynamic imports, and no hidden mutable state beyond the
  module-level registry constant (AGENTS.md rule #10).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

#: Per-version-probe wall-clock timeout, in seconds. Version banners are
#: fast; this only guards a broken/hung executable.
PROBE_TIMEOUT_SECONDS = 3.0

#: Version strings are informational and bounded; a longer banner is cut.
VERSION_MAX_CHARS = 200


class ToolPlaneError(RuntimeError):
    """Base error for the tool plane layer (AGENTS.md rule #9)."""


class ToolCatalogError(ToolPlaneError):
    """An unknown tool id was referenced (a configuration error)."""


class ToolProviderError(ToolPlaneError):
    """No installed provider exists for a requested capability.

    Raised by :meth:`ToolProvider.resolve` — the tool plane never
    silently passes through a guess when the environment lacks the
    executable behind a capability.
    """


class ToolSpec(BaseModel):
    """One known tool: the capability ids it provides and the binaries.

    Attributes:
        tool_id: Stable tool identifier (e.g. ``"curl"``).
        binaries: Candidate binary names providing the tool, in
            preference order; the first one found on PATH wins.
        capabilities: First-class capability ids this tool provides
            (e.g. ``("http.request",)`` for curl).
        version_flags: Argument vector for the version probe (e.g.
            ``("--version",)``); empty when the tool has no reliable
            version banner (the record then keeps ``version=None``).
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(min_length=1, max_length=64)
    binaries: tuple[str, ...] = Field(min_length=1)
    capabilities: tuple[str, ...] = Field(min_length=1)
    version_flags: tuple[str, ...] = ()


class ToolRecord(BaseModel):
    """The inventory's deterministic record for one catalog tool.

    An absent tool keeps ``binary``/``path``/``version`` as None and
    declares NO capabilities — the invariant that makes an absent
    tool's capabilities unadvertisable. ``version`` is None until the
    version probe ran (or when the probe failed); ``installed`` is
    derived purely from the PATH lookup.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    binary: str | None = None
    path: str | None = None
    version: str | None = None
    capabilities: tuple[str, ...] = ()

    @property
    def installed(self) -> bool:
        """True when the PATH lookup resolved a binary for this tool."""
        return self.path is not None


class ResolvedTool(BaseModel):
    """The concrete executable behind one requested capability.

    Returned by :meth:`ToolProvider.resolve`: the tool id, the
    requested capability, the resolved binary name, its absolute path,
    and the probed version (when known).
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    capability: str
    binary: str
    path: str
    version: str | None = None


class ToolCatalog:
    """Snapshot of the static tool registry (like SkillRegistry).

    Instances snapshot the module-level :data:`TOOLS` mapping at
    construction (or an injected override), so callers get an isolated
    view. Methods are deterministic: :meth:`tools` and
    :meth:`providers_for` return results in catalog order, which is the
    provider-preference order.

    Args:
        tools: Optional mapping override (``tool_id`` -> ToolSpec),
            defaulting to the module-level :data:`TOOLS`. Copied, so
            later mutation of the caller's dict does not leak in.
    """

    def __init__(self, tools: Mapping[str, ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = dict(TOOLS if tools is None else tools)

    def spec(self, tool_id: str) -> ToolSpec:
        """The spec for ``tool_id``.

        Raises:
            ToolCatalogError: If ``tool_id`` is not registered.
        """
        try:
            return self._tools[tool_id]
        except KeyError:
            raise ToolCatalogError(f"no tool registered for id {tool_id!r}") from None

    def is_registered(self, tool_id: str) -> bool:
        """True when ``tool_id`` is a registered catalog tool."""
        return tool_id in self._tools

    def tools(self) -> tuple[ToolSpec, ...]:
        """Every catalog tool, in catalog (preference) order."""
        return tuple(self._tools.values())

    def capabilities(self) -> frozenset[str]:
        """The complete capability vocabulary the catalog knows.

        The union of every registered tool's declared capabilities — a
        capability with no catalog provider is not part of the
        vocabulary.
        """
        return frozenset(
            capability for spec in self._tools.values() for capability in spec.capabilities
        )

    def providers_for(self, capability: str) -> tuple[ToolSpec, ...]:
        """Every catalog tool declaring ``capability``, in preference order."""
        return tuple(spec for spec in self._tools.values() if capability in spec.capabilities)


class ToolInventory:
    """The deterministic startup inventory of the runtime environment.

    Probes the environment for every catalog tool: a PATH lookup
    (:func:`shutil.which`) resolves the first present binary, and a
    version probe (:meth:`run`) captures its banner. Absent tools are
    recorded as absent (``path=None``, no capabilities); a present
    binary whose version cannot be probed keeps ``version=None`` — the
    inventory never raises for a probe failure.

    Deterministic: records are keyed by ``tool_id`` in catalog order,
    the search path is taken from the ``paths`` argument (defaulting to
    ``PATH``) without reordering, and probing is idempotent
    (:meth:`run` re-running is a no-op).

    Args:
        catalog: Optional catalog override (defaults to
            :class:`ToolCatalog` over the module registry).
        paths: The search path directories to probe, in order. Defaults
            to ``os.environ["PATH"]``. An empty sequence probes nothing
            (every tool is recorded absent) — the hermetic mode tests
            use.
        probe_timeout_seconds: Wall-clock bound for one version probe.
    """

    def __init__(
        self,
        catalog: ToolCatalog | None = None,
        *,
        paths: Sequence[str] | None = None,
        probe_timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._catalog = catalog if catalog is not None else ToolCatalog()
        self._probe_timeout_seconds = probe_timeout_seconds
        if paths is None:
            self._paths = tuple(
                entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry
            )
        else:
            self._paths = tuple(entry for entry in paths if entry)
        self._records: dict[str, ToolRecord] = self._lookup()
        self._probed = False

    @property
    def records(self) -> Mapping[str, ToolRecord]:
        """The per-tool inventory records (catalog order)."""
        return self._records

    @property
    def capabilities(self) -> CapabilityRegistry:
        """The capabilities actually available in this environment."""
        return CapabilityRegistry(self._records)

    def record(self, tool_id: str) -> ToolRecord | None:
        """The inventory record for ``tool_id``, or None when unknown."""
        return self._records.get(tool_id)

    def run(self) -> ToolInventory:
        """Probe versions for every installed tool; idempotent.

        A tool whose spec declares no ``version_flags`` is never
        probed (its record keeps ``version=None``): invoking it bare
        would print usage junk or, worse, block on stdin.

        Returns:
            Self, so startup wiring can chain (``inventory.run()``).
        """
        if self._probed:
            return self
        for tool_id, record in self._records.items():
            if record.path is None:
                continue
            flags = self._catalog.spec(tool_id).version_flags
            if not flags:
                # No reliable version banner: never spawn the binary
                # bare (usage junk / stdin block); version stays None.
                continue
            version = self._probe_version(record.path, flags)
            if version is not None:
                self._records[tool_id] = record.model_copy(update={"version": version})
        self._probed = True
        return self

    # ------------------------------------------------------------------
    # probing
    # ------------------------------------------------------------------

    def _lookup(self) -> dict[str, ToolRecord]:
        """PATH lookups for every catalog tool, in catalog order."""
        records: dict[str, ToolRecord] = {}
        search_path = os.pathsep.join(self._paths)
        for spec in self._catalog.tools():
            binary, path = self._find_binary(spec.binaries, search_path)
            if binary is None:
                records[spec.tool_id] = ToolRecord(tool_id=spec.tool_id, capabilities=())
                continue
            records[spec.tool_id] = ToolRecord(
                tool_id=spec.tool_id,
                binary=binary,
                path=path,
                capabilities=spec.capabilities,
            )
        return records

    @staticmethod
    def _find_binary(binaries: Sequence[str], search_path: str) -> tuple[str | None, str | None]:
        """The first present binary and its path, or (None, None)."""
        if not search_path:
            return None, None
        for binary in binaries:
            path = shutil.which(binary, path=search_path)
            if path is not None:
                return binary, path
        return None, None

    def _probe_version(self, path: str, flags: Sequence[str]) -> str | None:
        """The bounded first output line of ``path <flags>``, or None.

        Any spawn/exec/timeout failure yields None — a found binary
        whose banner cannot be read is still installed (the PATH lookup
        is the truth; the banner is informational). A nonzero exit also
        yields None: an erroring invocation's stderr is not a version.
        """
        try:
            completed = subprocess.run(
                [path, *flags],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._probe_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
        for line in combined.splitlines():
            line = line.strip()
            if line:
                return line[:VERSION_MAX_CHARS]
        return None


class CapabilityRegistry:
    """The capabilities actually available in the current environment.

    Derived from an inventory's records: only capabilities declared by
    INSTALLED tools (``record.installed``) are available — an absent
    tool's capabilities are never advertised. All query results are
    deterministic (catalog order).

    Args:
        records: The inventory's records mapping (``tool_id`` ->
            ToolRecord), in catalog order.
    """

    def __init__(self, records: Mapping[str, ToolRecord]) -> None:
        self._records = records

    def available(self) -> frozenset[str]:
        """Every capability with at least one installed provider."""
        available: set[str] = set()
        for record in self._records.values():
            if record.installed:
                available.update(record.capabilities)
        return frozenset(available)

    def is_available(self, capability: str) -> bool:
        """True when an installed tool provides ``capability``."""
        return capability in self.available()

    def providers_for(self, capability: str) -> tuple[str, ...]:
        """Installed tool ids providing ``capability``, in catalog order."""
        return tuple(
            record.tool_id
            for record in self._records.values()
            if record.installed and capability in record.capabilities
        )

    def capabilities_for_binary(self, binary: str) -> tuple[str, ...]:
        """Capabilities provided by installed records using ``binary``.

        Deduplicated, in catalog order (the union of the records whose
        resolved binary name is exactly ``binary``).
        """
        capabilities: list[str] = []
        seen: set[str] = set()
        for record in self._records.values():
            if record.installed and record.binary == binary:
                for capability in record.capabilities:
                    if capability not in seen:
                        seen.add(capability)
                        capabilities.append(capability)
        return tuple(capabilities)

    def record(self, tool_id: str) -> ToolRecord | None:
        """The inventory record for ``tool_id``, or None when unknown."""
        return self._records.get(tool_id)


class ToolProvider:
    """Intent/requirement -> concrete executable resolution.

    Given a requested capability, :meth:`resolve` returns the BEST
    installed provider — the first installed catalog tool declaring the
    capability, in catalog (preference) order (e.g. curl for
    ``http.request``, nmap for ``network.port_scan``). When no
    installed provider exists it raises :class:`ToolProviderError`: the
    tool plane never silently passes through a guess.

    Args:
        registry: The capability registry derived from the inventory.
        catalog: The catalog whose order defines provider preference
            (and whose names appear in error messages); defaults to the
            module registry.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        catalog: ToolCatalog | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog if catalog is not None else ToolCatalog()

    def resolve(self, capability: str) -> ResolvedTool:
        """The best installed provider for ``capability``.

        Raises:
            ToolProviderError: When no installed provider exists (or
                the capability is not part of the catalog vocabulary).
        """
        for tool_id in self._registry.providers_for(capability):
            record = self._registry.record(tool_id)
            if record is not None and record.path is not None:
                return ResolvedTool(
                    tool_id=tool_id,
                    capability=capability,
                    binary=record.binary or tool_id,
                    path=record.path,
                    version=record.version,
                )
        catalog_names = ", ".join(spec.tool_id for spec in self._catalog.providers_for(capability))
        if not catalog_names:
            catalog_names = "(none — not a catalog capability)"
        raise ToolProviderError(
            f"no installed provider for capability {capability!r} "
            f"(catalog providers: {catalog_names})"
        )

    def is_resolvable(self, capability: str) -> bool:
        """True when ``capability`` has an installed provider."""
        return self._registry.is_available(capability)


#: Deterministic registry: tool_id -> ToolSpec. Populated at import with
#: the known tools; extensible via :func:`register_tool` (explicit
#: registration only — no discovery, AGENTS.md rule #10). Catalog order
#: IS the provider-preference order.
TOOLS: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    """Register ``spec`` under its ``tool_id``.

    Raises:
        ToolCatalogError: If a tool is already registered for the id
            (duplicate registration fails loudly rather than silently
            overwriting).
    """
    if spec.tool_id in TOOLS:
        raise ToolCatalogError(f"a tool is already registered for id {spec.tool_id!r}")
    TOOLS[spec.tool_id] = spec


def _tool(
    *,
    tool_id: str,
    binaries: Sequence[str],
    capabilities: Sequence[str],
    version_flags: Sequence[str] = ("--version",),
) -> ToolSpec:
    """Convenience constructor for initial catalog tools."""
    return ToolSpec(
        tool_id=tool_id,
        binaries=tuple(binaries),
        capabilities=tuple(capabilities),
        version_flags=tuple(version_flags),
    )


# ---------------------------------------------------------------------------
# HTTP / web
# ---------------------------------------------------------------------------

#: HTTP request issuance and header/body retrieval (the workhorse of the
#: recon/enumeration skills).
register_tool(_tool(tool_id="curl", binaries=("curl",), capabilities=("http.request",)))
register_tool(_tool(tool_id="wget", binaries=("wget",), capabilities=("http.request",)))

#: Web content discovery: bounded wordlist fuzzing of paths/vhosts.
register_tool(
    _tool(
        tool_id="ffuf",
        binaries=("ffuf",),
        capabilities=("web.content_discovery",),
        version_flags=("-V",),
    )
)
register_tool(
    _tool(
        tool_id="feroxbuster",
        binaries=("feroxbuster",),
        capabilities=("web.content_discovery",),
    )
)
register_tool(
    _tool(
        tool_id="gobuster",
        binaries=("gobuster",),
        capabilities=("web.content_discovery",),
        version_flags=("version",),
    )
)

#: Portable fallback content discovery: bounded curl-based path probing
#: (``curl -o /dev/null -w '%{http_code}' <candidate>``) — the same
#: primitive the content-discovery skill cards use. Registered AFTER
#: the dedicated fuzzers so they stay the preferred providers when
#: installed; this entry guarantees ``web.content_discovery`` resolves
#: in ANY environment with curl (the V10 tool-contract guarantee,
#: docs/BENCHMARKS.md: every skill's required capability resolves to a
#: working installed provider in the base environment).
register_tool(
    _tool(
        tool_id="curl_probe",
        binaries=("curl",),
        capabilities=("web.content_discovery",),
    )
)

#: Automated web vulnerability scanning (template-driven).
register_tool(
    _tool(
        tool_id="nuclei",
        binaries=("nuclei",),
        capabilities=("web.vuln_scan",),
        version_flags=("-version",),
    )
)

#: SQL injection detection/exploitation once a hypothesis is evidenced.
register_tool(_tool(tool_id="sqlmap", binaries=("sqlmap",), capabilities=("web.sql_injection",)))

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

#: TCP port scanning and service/version detection.
register_tool(
    _tool(
        tool_id="nmap",
        binaries=("nmap",),
        capabilities=("network.port_scan", "network.service_detect"),
    )
)

#: Connect-based probes and banner grabs (no version banner: None).
register_tool(
    _tool(
        tool_id="nc",
        binaries=("nc", "netcat"),
        capabilities=("network.probe",),
        version_flags=(),
    )
)

#: TLS/SSL service probing and certificate inspection.
register_tool(
    _tool(
        tool_id="openssl",
        binaries=("openssl",),
        capabilities=("network.tls_probe", "crypto.analyze"),
        version_flags=("version",),
    )
)

#: Local listener enumeration.
register_tool(_tool(tool_id="ss", binaries=("ss",), capabilities=("network.listener",)))
register_tool(_tool(tool_id="netstat", binaries=("netstat",), capabilities=("network.listener",)))

#: Packet capture.
register_tool(_tool(tool_id="tcpdump", binaries=("tcpdump",), capabilities=("network.capture",)))

# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------

register_tool(
    _tool(tool_id="dig", binaries=("dig",), capabilities=("dns.lookup",), version_flags=("-v",))
)
register_tool(
    _tool(tool_id="host", binaries=("host",), capabilities=("dns.lookup",), version_flags=("-V",))
)
register_tool(
    _tool(
        tool_id="nslookup", binaries=("nslookup",), capabilities=("dns.lookup",), version_flags=()
    )
)

# ---------------------------------------------------------------------------
# Active Directory / SMB
# ---------------------------------------------------------------------------

#: AD/SMB/authentication enumeration and probing (nxc is the flagship).
register_tool(
    _tool(
        tool_id="netexec",
        binaries=("nxc", "netexec"),
        capabilities=("ad.enum", "smb.enum", "network.auth_probe"),
    )
)
register_tool(
    _tool(
        tool_id="smbmap",
        binaries=("smbmap",),
        capabilities=("smb.enum",),
        version_flags=(),
    )
)
register_tool(
    _tool(
        tool_id="enum4linux",
        binaries=("enum4linux", "enum4linux-ng"),
        capabilities=("ad.kerberos_enum", "smb.enum"),
        version_flags=(),
    )
)
register_tool(
    _tool(
        tool_id="kerbrute",
        binaries=("kerbrute",),
        capabilities=("ad.kerberos_enum",),
        version_flags=("version",),
    )
)
register_tool(
    _tool(
        tool_id="ldapsearch",
        binaries=("ldapsearch",),
        capabilities=("ad.ldap_query",),
        version_flags=("-VV",),
    )
)

# ---------------------------------------------------------------------------
# Authentication / credentials
# ---------------------------------------------------------------------------

#: Online service credential brute forcing (no version banner: None).
register_tool(
    _tool(
        tool_id="hydra",
        binaries=("hydra",),
        capabilities=("network.auth_bruteforce",),
        version_flags=(),
    )
)
#: Offline password cracking.
register_tool(
    _tool(
        tool_id="john",
        binaries=("john", "john-jumbo"),
        capabilities=("password.crack",),
        version_flags=(),
    )
)
register_tool(_tool(tool_id="hashcat", binaries=("hashcat",), capabilities=("password.crack",)))

# ---------------------------------------------------------------------------
# Source / secrets / containers
# ---------------------------------------------------------------------------

register_tool(_tool(tool_id="semgrep", binaries=("semgrep",), capabilities=("source.sast",)))
register_tool(
    _tool(
        tool_id="codeql",
        binaries=("codeql",),
        capabilities=("source.sast",),
        version_flags=("version",),
    )
)
register_tool(
    _tool(
        tool_id="trivy",
        binaries=("trivy",),
        capabilities=("container.scan", "secrets.scan", "source.sast"),
    )
)
register_tool(
    _tool(
        tool_id="gitleaks",
        binaries=("gitleaks",),
        capabilities=("secrets.scan",),
        version_flags=("version",),
    )
)

# ---------------------------------------------------------------------------
# Exploit search
# ---------------------------------------------------------------------------

register_tool(
    _tool(tool_id="searchsploit", binaries=("searchsploit",), capabilities=("exploit.search",))
)

# ---------------------------------------------------------------------------
# File / binary analysis
# ---------------------------------------------------------------------------

register_tool(_tool(tool_id="file", binaries=("file",), capabilities=("file.analyze",)))
register_tool(
    _tool(
        tool_id="strings",
        binaries=("strings",),
        capabilities=("file.analyze", "binary.analyze"),
    )
)
register_tool(
    _tool(
        tool_id="exiftool",
        binaries=("exiftool",),
        capabilities=("file.metadata",),
        version_flags=("-ver",),
    )
)
register_tool(
    _tool(
        tool_id="binwalk",
        binaries=("binwalk",),
        capabilities=("binary.analyze", "file.extract"),
        version_flags=(),
    )
)
register_tool(_tool(tool_id="readelf", binaries=("readelf",), capabilities=("binary.analyze",)))
register_tool(
    _tool(
        tool_id="checksec",
        binaries=("checksec",),
        capabilities=("binary.analyze",),
        version_flags=(),
    )
)
register_tool(_tool(tool_id="objdump", binaries=("objdump",), capabilities=("binary.analyze",)))
register_tool(_tool(tool_id="grep", binaries=("grep",), capabilities=("file.search",)))
register_tool(_tool(tool_id="base64", binaries=("base64",), capabilities=("crypto.decode",)))
register_tool(_tool(tool_id="sqlite3", binaries=("sqlite3",), capabilities=("database.query",)))
