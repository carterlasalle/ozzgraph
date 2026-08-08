"""Scope policy, fingerprints, and duplicate detection for OzzGraph (PR10).

Implements AGENTS.md Security Boundaries steps 3-8 as a gate that runs
BEFORE :class:`~ozzgraph.shell.ShellRunner` executes anything:

3. command-length limits,
4. target allowlists,
5. platform/metadata and public-internet destination blocking,
6. worker-scope and phase permissions (command families),
7. normalized fingerprints,
8. duplicate rejection via the :class:`FingerprintStore`.

The executor (a later PR) calls :meth:`ScopePolicy.check` before
:meth:`ShellRunner.run` and carries ``PolicyDecision.fingerprint`` into
events and actions; :func:`check_then_run` wires the whole gate for the
common path without changing :mod:`ozzgraph.shell` at all.

The gate is deterministic and fail-closed (AGENTS.md rule #9): unknown
phases, unknown command families, and destinations that are not
allowlisted are rejected loudly with structured
:class:`ScopeViolationError` subclasses. Fingerprints are a
loop-prevention heuristic, not a semantic-equivalence oracle —
normalization collapses whitespace, unwraps ``sh -c`` wrappers, strips
trivial trailing shell noise, and casefolds, so semantically-identical
commands hash the same while genuinely distinct commands (usually) do
not. Every approved fingerprint is mirrored to
``state_dir/duplicates.jsonl`` in the same append-only JSONL style as
the event log.

Destination extraction is intentionally a deterministic heuristic (URL
authorities, network-verb arguments, and bare IP literals), not a shell
parser — the allowlist is a defense-in-depth layer, not a sandbox.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from ozzgraph.shell import ShellRunner, ToolResult

#: Default ceiling for a single command line, in characters.
DEFAULT_MAX_COMMAND_LENGTH = 4096

#: Default target allowlist: empty, so no external destination may be
#: addressed until the operator configures targets (fail closed).
DEFAULT_TARGET_ALLOWLIST: tuple[str, ...] = ()

#: Default command families permitted at the policy level. Phases and
#: worker scopes narrow this further per call.
DEFAULT_ALLOWED_COMMAND_FAMILIES: tuple[str, ...] = ("shell", "recon", "exploit")

#: Family assigned to commands that match no known tool (and to the
#: ``sh``/``bash`` wrappers themselves).
DEFAULT_FAMILY = "shell"

#: Well-known platform metadata / cloud-init endpoints plus the loopback
#: hostnames. Blocked unless explicitly allowlisted.
_PLATFORM_DESTINATIONS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "169.254.169.253",
        "100.100.100.200",
        "metadata.google.internal",
        "metadata.azure.internal",
        "instance-data",
        "fd00:ec2::254",
        "localhost",
        "localhost.localdomain",
    }
)

_RECON_COMMANDS: frozenset[str] = frozenset(
    {
        "nmap",
        "masscan",
        "ping",
        "ping6",
        "arp",
        "netstat",
        "ss",
        "route",
        "traceroute",
        "tracepath",
        "dig",
        "host",
        "nslookup",
        "whois",
        "curl",
        "wget",
        "gobuster",
        "ffuf",
        "dirb",
        "nikto",
        "dnsrecon",
        "dnsenum",
        "theharvester",
        "sublist3r",
    }
)

_EXPLOIT_COMMANDS: frozenset[str] = frozenset(
    {
        "sqlmap",
        "hydra",
        "medusa",
        "ncrack",
        "john",
        "hashcat",
        "msfconsole",
        "msfvenom",
        "searchsploit",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "crackmapexec",
        "netexec",
        "evil-winrm",
        "wpscan",
        "enum4linux",
        "smbmap",
        "smbclient",
        "responder",
        "mimikatz",
    }
)

#: Command-family table: program basename (lowercase) -> family.
_COMMAND_FAMILIES: dict[str, str] = {
    **{command: "recon" for command in _RECON_COMMANDS},
    **{command: "exploit" for command in _EXPLOIT_COMMANDS},
}

#: Command families permitted per graph phase (docs/ARCHITECTURE.md,
#: "Phase Router"). Unknown phases map to nothing so the gate fails
#: closed rather than guessing. V01 (docs/adr/0008): FLAG_HUNT /
#: VERIFY_AND_SUBMIT left the generic kernel, so their phase entries
#: are gone; the halctf environment adapter owns them in V09.
_PHASE_FAMILIES: dict[str, frozenset[str]] = {
    "BOOTSTRAP": frozenset({"shell", "recon"}),
    "RECON": frozenset({"shell", "recon"}),
    "ENUMERATION": frozenset({"shell", "recon"}),
    "EXPLOITATION": frozenset({"shell", "exploit"}),
    "POST_EXPLOITATION": frozenset({"shell", "exploit"}),
    "PIVOT": frozenset({"shell", "recon"}),
    "REPLAN": frozenset({"shell"}),
    "DONE": frozenset(),
}

#: Known phase names (uppercase), for configuration and introspection.
PHASES: frozenset[str] = frozenset(_PHASE_FAMILIES)

#: Known command families.
COMMAND_FAMILIES: frozenset[str] = frozenset(_COMMAND_FAMILIES.values()) | {DEFAULT_FAMILY}

#: ``sh -c '...'`` / ``bash -c "..."`` wrappers (any path prefix).
_SH_WRAPPER_RE = re.compile(r"^(?:\S*/)?(?:sh|bash|dash|zsh|ksh)\s+-c\s+([\"'])(.*?)\1$", re.DOTALL)

#: Prefixes that do not change what a command is (sudo env nohup ...).
_FAMILY_OVERRIDE_RE = re.compile(r"^(?:sudo|env|nohup|time|nice)\s+")

#: URL authorities, e.g. ``http://user:pass@host:8080/path``.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s'\"]+)", re.IGNORECASE)

#: Standalone IPv4 tokens, optionally with a port (``10.0.0.5:8080``).
_IPV4_TOKEN_RE = re.compile(
    r"(?<!\S)(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d+)?(?=\s|$)"
)

#: Bracketed IPv6 literals, e.g. ``[fd00::1]`` or ``[::1]:8080``.
_IPV6_BRACKET_RE = re.compile(r"\[([0-9a-fA-F:]+)\]")

#: Verbs whose following argument may be a host (curl, ping, ssh, ...).
_NETWORK_VERBS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "ping",
        "ping6",
        "nc",
        "ncat",
        "netcat",
        "nmap",
        "masscan",
        "dig",
        "host",
        "nslookup",
        "traceroute",
        "tracepath",
        "telnet",
        "ftp",
        "git",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "sqlmap",
        "hydra",
        "medusa",
        "nikto",
        "gobuster",
        "ffuf",
        "wpscan",
        "smbclient",
        "socat",
        "whois",
        "dnsrecon",
        "dnsenum",
        "ss",
    }
)

#: sha256 hex digest shape for fingerprints.
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class ScopeViolationError(RuntimeError):
    """Base error for every policy-gate rejection (AGENTS.md rule #9).

    Subclasses distinguish the failing security boundary so the
    supervisor can classify and log each rejection precisely.
    """


class CommandLengthError(ScopeViolationError):
    """Raised when a command exceeds ``max_command_length`` (step 3)."""


class AllowlistViolationError(ScopeViolationError):
    """Raised when a command addresses a destination outside the allowlist (step 4)."""


class PlatformDestinationError(ScopeViolationError):
    """Raised for platform metadata, loopback, and link-local destinations (step 5)."""


class PublicInternetError(ScopeViolationError):
    """Raised for public-internet destinations that are not allowlisted (step 5)."""


class FamilyPermissionError(ScopeViolationError):
    """Raised when the command family is not permitted (step 6)."""


class PhasePermissionError(FamilyPermissionError):
    """Raised when the command family is not permitted in the current phase (step 6)."""


class DuplicateActionError(ScopeViolationError):
    """Raised when a command's fingerprint was already recorded (step 8)."""


class FingerprintStoreError(RuntimeError):
    """Raised when the fingerprint store is corrupt or misused."""


class PolicyDecision(BaseModel):
    """Outcome of an approved policy check.

    Attributes:
        command: The exact command string that was approved.
        canonical: Normalized form of the command (whitespace collapsed,
            shell noise stripped, casefolded).
        fingerprint: sha256 hex digest of ``canonical`` — the stable
            identity an executor can carry into events and actions.
        family: Command family the command was classified into.
        destinations: Hosts/IPs the command addresses (may be empty).
    """

    command: str
    canonical: str
    fingerprint: str
    family: str
    destinations: list[str] = Field(default_factory=list)


class ScopePolicy:
    """Validate a proposed shell command before execution.

    Implements AGENTS.md Security Boundaries steps 3-7 in order: command
    length, destinations (allowlist, platform/public-internet blocks),
    family permissions (phase and worker scope), then the normalized
    fingerprint. The duplicate gate (step 8) lives in
    :class:`FingerprintStore`, which the executor consults after
    :meth:`check` returns a decision.

    Args:
        max_command_length: Reject commands longer than this many
            characters.
        target_allowlist: Hostnames, IPs, and CIDR networks that
            commands may address. Empty means no external destination is
            permitted (fail closed); entries override the default
            platform/public-internet blocks.
        allowed_command_families: Families any command may belong to at
            the policy level; phases and worker scopes narrow this
            per call.

    Raises:
        ValueError: If ``max_command_length`` is less than 1.
    """

    def __init__(
        self,
        *,
        max_command_length: int = DEFAULT_MAX_COMMAND_LENGTH,
        target_allowlist: Sequence[str] = DEFAULT_TARGET_ALLOWLIST,
        allowed_command_families: Sequence[str] = DEFAULT_ALLOWED_COMMAND_FAMILIES,
    ) -> None:
        if max_command_length < 1:
            raise ValueError(f"max_command_length must be >= 1, got {max_command_length}")
        self._max_command_length = max_command_length
        self._target_allowlist = tuple(target_allowlist)
        self._allowed_families = frozenset(family.casefold() for family in allowed_command_families)

    def check(
        self,
        command: str,
        *,
        phase: str | None = None,
        worker_scope: str | None = None,
    ) -> PolicyDecision:
        """Validate ``command`` against the gate.

        Args:
            command: The proposed shell command line.
            phase: Optional graph phase (case-insensitive). Only the
                phase's command families are permitted; unknown phases
                fail closed.
            worker_scope: Optional comma-separated list of command
                families the worker may run. ``shell`` is not implied —
                include it explicitly when the worker needs it.

        Raises:
            ScopeViolationError: For empty commands.
            CommandLengthError: If the command exceeds the length limit.
            AllowlistViolationError: If a destination is not allowlisted.
            PlatformDestinationError: For platform metadata, loopback,
                or link-local destinations.
            PublicInternetError: For public-internet destinations.
            FamilyPermissionError: If the command family is not
                permitted by the policy or worker scope.
            PhasePermissionError: If the command family is not permitted
                in the given phase (a ``FamilyPermissionError`` subclass).

        Returns:
            The approved decision carrying the canonical form and
            fingerprint for the duplicate gate and event/action wiring.
        """
        if not command.strip():
            raise ScopeViolationError("command must not be empty or whitespace-only")
        if len(command) > self._max_command_length:
            raise CommandLengthError(
                f"command length {len(command)} exceeds limit {self._max_command_length}"
            )
        destinations = extract_destinations(command)
        for destination in destinations:
            self._check_destination(destination)
        family = classify_family(command)
        self._check_family(family, phase=phase, worker_scope=worker_scope)
        canonical, fingerprint = fingerprint_command(command)
        return PolicyDecision(
            command=command,
            canonical=canonical,
            fingerprint=fingerprint,
            family=family,
            destinations=destinations,
        )

    def _check_destination(self, destination: str) -> None:
        """Reject a destination unless allowlisted or harmless-local.

        The allowlist wins: an explicitly allowlisted destination passes
        even when it is a platform endpoint or public address ("unless
        explicitly allowlisted"). Everything else is classified into a
        loud, specific error.
        """
        if self._is_allowlisted(destination):
            return
        lowered = destination.casefold()
        if lowered in _PLATFORM_DESTINATIONS:
            raise PlatformDestinationError(
                f"destination {destination!r} is a blocked platform/metadata or loopback "
                "address; allowlist it explicitly to permit it"
            )
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            raise AllowlistViolationError(
                f"destination {destination!r} is not in the target allowlist"
            ) from None
        if address.is_loopback or address.is_link_local:
            raise PlatformDestinationError(
                f"destination {destination!r} is a blocked loopback or link-local address; "
                "allowlist it explicitly to permit it"
            )
        if address.is_global:
            raise PublicInternetError(
                f"destination {destination!r} is a public-internet address not in the "
                "target allowlist"
            )
        raise AllowlistViolationError(f"destination {destination!r} is not in the target allowlist")

    def _is_allowlisted(self, destination: str) -> bool:
        """True when ``destination`` matches an allowlist entry.

        Entries are hostnames (exact, case-insensitive), IP addresses,
        or CIDR networks. Hostname destinations match only exact
        hostname entries; IP destinations match exact IP or contained-in
        CIDR entries.
        """
        lowered = destination.casefold()
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            return any(entry.casefold() == lowered for entry in self._target_allowlist)
        for entry in self._target_allowlist:
            try:
                network = ipaddress.ip_network(entry.casefold(), strict=False)
            except ValueError:
                continue  # hostname entry cannot match an IP destination
            address_is_v4 = isinstance(address, ipaddress.IPv4Address)
            if address_is_v4 != isinstance(network, ipaddress.IPv4Network):
                continue
            if address in network:
                return True
        return False

    def _check_family(
        self,
        family: str,
        *,
        phase: str | None,
        worker_scope: str | None,
    ) -> None:
        """Reject a command family the policy, phase, or scope forbids."""
        allowed: set[str] = set(self._allowed_families)
        phase_label: str | None = None
        if phase is not None:
            key = phase.strip().casefold().upper()
            phase_families = _PHASE_FAMILIES.get(key)
            if phase_families is None:
                raise PhasePermissionError(
                    f"unknown phase {phase!r}: the gate fails closed with no permitted "
                    "command families"
                )
            phase_label = key
            allowed &= set(phase_families)
        if worker_scope is not None:
            scope_families = {
                token.strip().casefold() for token in worker_scope.split(",") if token.strip()
            }
            allowed &= scope_families
        if family not in allowed:
            if phase_label is not None:
                raise PhasePermissionError(
                    f"command family {family!r} is not permitted in phase {phase_label}; "
                    f"permitted: {sorted(allowed)}"
                )
            raise FamilyPermissionError(
                f"command family {family!r} is not permitted; permitted: {sorted(allowed)}"
            )


class FingerprintStore:
    """Duplicate record for approved command fingerprints (AGENTS.md step 8).

    Keeps every approved fingerprint in memory and, when given a path,
    mirrors each record to ``duplicates.jsonl`` in the same JSONL style
    as the event log (one JSON object per line, appended and flushed,
    never rewritten). The parent directory must already exist.

    Args:
        path: Optional JSONL file to persist records to and reload from.
            When None the store is purely in-memory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._known: set[str] = set()
        if path is not None and path.is_file():
            self._load()

    @classmethod
    def for_run(cls, state_dir: Path) -> FingerprintStore:
        """The standard run store at ``state_dir / 'duplicates.jsonl'``."""
        return cls(state_dir / "duplicates.jsonl")

    @property
    def path(self) -> Path | None:
        """The JSONL path, or None for a purely in-memory store."""
        return self._path

    @property
    def known(self) -> int:
        """Number of distinct fingerprints recorded so far."""
        return len(self._known)

    def contains(self, fingerprint: str) -> bool:
        """True when ``fingerprint`` was already recorded."""
        return fingerprint in self._known

    def record(self, fingerprint: str, *, canonical: str) -> None:
        """Record an approved fingerprint, rejecting repeats.

        The fingerprint is recorded before execution (loop prevention: a
        repeat of a command that timed out or errored is still blocked).
        With a path, the record is appended to the JSONL file and
        flushed before returning.

        Args:
            fingerprint: 64-char sha256 hex digest of the canonical form.
            canonical: Normalized command text, for auditability.

        Raises:
            DuplicateActionError: If ``fingerprint`` was already
                recorded.
            FingerprintStoreError: If ``fingerprint`` is not a sha256
                hex digest or the JSONL file is corrupt/unwritable.
        """
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise FingerprintStoreError(
                f"invalid fingerprint {fingerprint!r}: expected a 64-char sha256 hex digest"
            )
        if fingerprint in self._known:
            raise DuplicateActionError(
                f"duplicate action rejected: fingerprint {fingerprint} already recorded "
                f"(command {canonical!r})"
            )
        self._known.add(fingerprint)
        if self._path is None:
            return
        line = json.dumps(
            {
                "fingerprint": fingerprint,
                "canonical": canonical,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError as exc:
            raise FingerprintStoreError(
                f"failed to append fingerprint record to {self._path}: {exc}"
            ) from exc

    def _load(self) -> None:
        """Rehydrate ``self._known`` from the JSONL file (fail loudly).

        Raises:
            FingerprintStoreError: If the file is unreadable or any line
                is not a JSON object carrying a string fingerprint.
        """
        assert self._path is not None
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise FingerprintStoreError(
                f"failed to read fingerprint store {self._path}: {exc}"
            ) from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                fingerprint = record["fingerprint"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise FingerprintStoreError(
                    f"corrupt fingerprint store line {line_number} in {self._path}: {exc}"
                ) from exc
            if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
                raise FingerprintStoreError(
                    f"corrupt fingerprint store line {line_number} in {self._path}: "
                    "fingerprint is not a sha256 hex digest"
                )
            self._known.add(fingerprint)


def normalize_command(command: str) -> str:
    """Canonical form of a command for fingerprinting.

    Unwraps ``sh -c '...'`` / ``bash -c "..."`` wrappers, collapses runs
    of whitespace to single spaces, strips trailing ``;`` / ``&&`` /
    ``||`` shell noise, and casefolds. The result is deterministic so
    semantically-identical commands (modulo whitespace, casing, and
    wrapper quoting) hash the same.
    """
    stripped = command.strip()
    wrapper = _SH_WRAPPER_RE.match(stripped)
    if wrapper:
        stripped = wrapper.group(2).strip()
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    collapsed = re.sub(r"(?:\s*;)+$", "", collapsed).strip()
    collapsed = re.sub(r"\s*(?:&&|\|\|)\s*$", "", collapsed).strip()
    return collapsed.casefold()


def fingerprint_command(command: str) -> tuple[str, str]:
    """Return ``(canonical, fingerprint)`` for ``command``.

    The fingerprint is the sha256 hex digest of the canonical form and
    is stable across whitespace/case variants of the same command.
    """
    canonical = normalize_command(command)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def classify_family(command: str) -> str:
    """Classify a command into a family (``shell``, ``recon``, ``exploit``).

    The first program token (after unwrapping ``sh -c`` wrappers and
    ``sudo``/``env``/``nohup`` prefixes) is matched against the known
    tool table; unknown programs fall back to :data:`DEFAULT_FAMILY`.
    """
    stripped = command.strip()
    wrapper = _SH_WRAPPER_RE.match(stripped)
    if wrapper:
        stripped = wrapper.group(2)
    stripped = _FAMILY_OVERRIDE_RE.sub("", stripped, count=1)
    tokens = stripped.split()
    if not tokens:
        return DEFAULT_FAMILY
    program = tokens[0].rsplit("/", 1)[-1].strip("\"'")
    return _COMMAND_FAMILIES.get(program.casefold(), DEFAULT_FAMILY)


def extract_destinations(command: str) -> list[str]:
    """Hosts/IPs a command addresses, in first-seen order (deduped).

    Sources: URL authorities, ``ssh``/``scp``/``sftp``/``rsync``
    targets, standalone IPv4 tokens, bracketed IPv6 literals, and
    host-looking arguments that follow network verbs. Tokens that merely
    look IP-ish but do not parse (``[abc:def]``) are ignored.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        host = _host_from_authority(raw)
        if not host or host in seen:
            return
        if ":" in host or re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host):
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return  # IP-shaped token that is not a valid address
        seen.add(host)
        found.append(host)

    for match in _URL_RE.finditer(command):
        _add(match.group(1))
    for match in _IPV4_TOKEN_RE.finditer(command):
        _add(match.group(0))
    for match in _IPV6_BRACKET_RE.finditer(command):
        _add(match.group(1))
    for token in _verb_host_tokens(command):
        _add(token)
    return found


def _host_from_authority(authority: str) -> str:
    """Extract the host from a URL/ssh authority (strip userinfo, port)."""
    host = authority.rsplit("@", 1)[-1].strip("\"'").rstrip(";|&")
    if not host:
        return ""
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        candidate = host.rsplit(":", 1)[0]
        # Only strip a port when the remainder plausibly hosts an address
        # (an IP literal or a dotted hostname); "abc:def" is not host:port.
        if _looks_like_host(candidate):
            return candidate
        return host
    return host


def _verb_host_tokens(command: str) -> list[str]:
    """Host-looking tokens that follow network verbs.

    For ``scp``/``sftp``/``rsync`` the target is the last non-flag
    argument (``host:path``); for every other verb the first
    host-looking non-flag argument is used (``git`` skips its
    subcommand).
    """
    tokens = command.split()
    hosts: list[str] = []
    for index, token in enumerate(tokens):
        verb = token.rsplit("/", 1)[-1].strip("\"'").casefold()
        if verb not in _NETWORK_VERBS:
            continue
        start = index + 2 if verb == "git" else index + 1
        candidates = [candidate for candidate in tokens[start:] if not candidate.startswith("-")]
        if not candidates:
            continue
        if verb in {"scp", "sftp", "rsync"}:
            target = candidates[-1]
            host = target.split(":", 1)[0].rsplit("@", 1)[-1]
            if _looks_like_host(host):
                hosts.append(host.rstrip(";|&"))
            continue
        for candidate in candidates:
            if _looks_like_host(candidate):
                hosts.append(candidate.rstrip(";|&"))
                break
    return hosts


def _looks_like_host(token: str) -> bool:
    """Heuristic: is ``token`` plausibly a host (IP, dotted name, localhost)?"""
    stripped = token.strip("\"'")
    host = stripped.rsplit("@", 1)[-1].rstrip(";|&")
    if host.casefold() in {"localhost", "localhost.localdomain"}:
        return True
    if host.isdigit() or host.startswith(".") or "/" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return "." in host or ":" in host


async def check_then_run(
    command: str,
    *,
    policy: ScopePolicy,
    store: FingerprintStore,
    runner: ShellRunner,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    working_directory: Path,
    phase: str | None = None,
    worker_scope: str | None = None,
) -> ToolResult:
    """Gate, record, and execute one command (policy steps 3-8 + runner).

    Runs ``policy.check`` (length, destinations, permissions,
    fingerprint), records the fingerprint in ``store`` (rejecting
    duplicates), then delegates to ``runner.run``. The fingerprint is
    recorded before execution so a repeat of a command that timed out or
    errored is still blocked; the executor can also call
    ``policy.check`` directly to obtain the decision's fingerprint for
    events and actions.

    Args:
        command: The command line to gate and execute.
        policy: The scope policy to enforce.
        store: The fingerprint store to record into.
        runner: The bounded shell runner to execute with.
        timeout_seconds / stdout_limit / stderr_limit / working_directory:
            Passed through to ``runner.run``.
        phase / worker_scope: Passed through to ``policy.check``.

    Raises:
        ScopeViolationError: If the command violates the policy gate.
        DuplicateActionError: If the command's fingerprint was already
            recorded (a subclass of ``ScopeViolationError``).
        ShellRunnerError: If the run arguments are invalid or the
            command could not be spawned.
    """
    decision = policy.check(command, phase=phase, worker_scope=worker_scope)
    store.record(decision.fingerprint, canonical=decision.canonical)
    return await runner.run(
        command=command,
        timeout_seconds=timeout_seconds,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        working_directory=working_directory,
    )
