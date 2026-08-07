"""Tests for the scope policy, fingerprints, and duplicate store (PR10).

Covers the AGENTS.md tool-change expectations for the policy gate:
representative success (allowlisted target + allowed family + under
length), command-length rejection, allowlist violations, platform
metadata / loopback / public-internet blocks, family and phase
permissions, fingerprint normalization (whitespace and case variants
collapse to one fingerprint), duplicate rejection with and without
persistence, failure paths (corrupt store, empty commands, adversarial
destination hiding), and a check_then_run() integration test that
executes a benign command and rejects a duplicate.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from ozzgraph.policy import (
    COMMAND_FAMILIES,
    DEFAULT_FAMILY,
    PHASES,
    AllowlistViolationError,
    CommandLengthError,
    DuplicateActionError,
    FamilyPermissionError,
    FingerprintStore,
    FingerprintStoreError,
    PhasePermissionError,
    PlatformDestinationError,
    PublicInternetError,
    ScopePolicy,
    ScopeViolationError,
    check_then_run,
    classify_family,
    extract_destinations,
    fingerprint_command,
    normalize_command,
)
from ozzgraph.shell import ShellRunner


def _policy(
    *,
    max_command_length: int = 512,
    target_allowlist: Sequence[str] = ("10.0.0.5", "challenge.local"),
    allowed_command_families: Sequence[str] = ("shell", "recon", "exploit"),
) -> ScopePolicy:
    """A policy with permissive defaults; keyword args tweak the gate."""
    return ScopePolicy(
        max_command_length=max_command_length,
        target_allowlist=target_allowlist,
        allowed_command_families=allowed_command_families,
    )


# --- representative success -------------------------------------------------


def test_allowlisted_target_and_family_approved() -> None:
    """An allowlisted IP target in an allowed family under the length limit passes."""
    decision = _policy().check("curl -s http://10.0.0.5:8080/health")
    assert decision.family == "recon"
    assert decision.destinations == ["10.0.0.5"]
    assert len(decision.fingerprint) == 64
    assert decision.fingerprint == fingerprint_command("curl -s http://10.0.0.5:8080/health")[1]
    assert decision.canonical == "curl -s http://10.0.0.5:8080/health"


def test_allowlisted_hostname_approved() -> None:
    """A dotted hostname that is allowlisted passes."""
    decision = _policy().check("ping -c 1 challenge.local")
    assert decision.destinations == ["challenge.local"]
    assert decision.family == "recon"


def test_local_command_with_empty_allowlist() -> None:
    """Commands addressing no external destination pass a fail-closed gate."""
    policy = _policy(target_allowlist=[])
    decision = policy.check("ls -la")
    assert decision.destinations == []
    assert decision.family == "shell"
    assert policy.check("cat /etc/hostname").fingerprint


# --- command-length limits (step 3) -----------------------------------------


def test_command_too_long_rejected() -> None:
    """A command longer than max_command_length is rejected loudly."""
    with pytest.raises(CommandLengthError, match="exceeds limit 10"):
        _policy(max_command_length=10).check("echo hello world")


def test_command_at_length_limit_ok() -> None:
    """A command exactly at the limit is permitted."""
    decision = _policy(max_command_length=5).check("echo ")
    assert decision.fingerprint


def test_policy_rejects_bad_length_limit() -> None:
    """A degenerate length limit is a construction error, not a silent gate."""
    with pytest.raises(ValueError):
        ScopePolicy(max_command_length=0)
    with pytest.raises(ValueError):
        ScopePolicy(max_command_length=-5)


# --- target allowlists (step 4) ---------------------------------------------


def test_unallowlisted_ip_rejected() -> None:
    """A private-IP destination outside the allowlist is rejected."""
    with pytest.raises(AllowlistViolationError, match="10.0.0.6"):
        _policy().check("curl http://10.0.0.6/")


def test_unallowlisted_hostname_rejected() -> None:
    """A hostname destination outside the allowlist is rejected."""
    with pytest.raises(AllowlistViolationError, match="other.local"):
        _policy().check("ping other.local")


def test_cidr_allowlist_entry() -> None:
    """A CIDR entry covers any address in the network."""
    policy = _policy(target_allowlist=["10.0.0.0/8"])
    assert policy.check("curl http://10.11.12.13/").destinations == ["10.11.12.13"]


def test_allowlist_matches_are_case_insensitive() -> None:
    """Hostname allowlist matching is case-insensitive."""
    policy = _policy(target_allowlist=["ChAlLeNgE.LOCAL"])
    assert policy.check("curl http://challenge.local/").fingerprint


# --- platform / public-internet blocks (step 5) -----------------------------


@pytest.mark.parametrize(
    "command",
    [
        "curl http://169.254.169.254/latest/meta-data/iam/",
        "curl http://169.254.169.253/",
        "curl http://100.100.100.200/latest/meta-data/",
        "curl http://metadata.google.internal/",
    ],
)
def test_platform_metadata_endpoints_blocked(command: str) -> None:
    """Cloud platform metadata endpoints are blocked by default."""
    with pytest.raises(PlatformDestinationError):
        _policy().check(command)


@pytest.mark.parametrize(
    "command",
    [
        "curl http://127.0.0.1:8080/",
        "curl http://localhost/",
        "ping 127.0.0.1",
        "curl http://[::1]:8080/",
    ],
)
def test_loopback_blocked(command: str) -> None:
    """Loopback destinations are blocked by default."""
    with pytest.raises(PlatformDestinationError):
        _policy().check(command)


def test_link_local_metadata_blocked() -> None:
    """Any 169.254.0.0/16 address (not just the well-known IP) is blocked."""
    with pytest.raises(PlatformDestinationError):
        _policy().check("curl http://169.254.42.42/")


@pytest.mark.parametrize(
    "command",
    [
        "curl https://8.8.8.8/",
        "ping 1.1.1.1",
        "echo $(curl https://9.9.9.9/)",
    ],
)
def test_public_ip_blocked(command: str) -> None:
    """Public-internet IP destinations are blocked with a distinct error."""
    with pytest.raises(PublicInternetError):
        _policy().check(command)


def test_public_hostname_blocked() -> None:
    """Public-internet hostnames are rejected as unallowlisted destinations."""
    with pytest.raises(AllowlistViolationError):
        _policy().check("curl https://example.com/")


def test_explicit_allowlist_overrides_platform_block() -> None:
    """An explicitly allowlisted metadata endpoint passes ('unless allowlisted')."""
    policy = _policy(target_allowlist=["169.254.169.254"])
    decision = policy.check("curl http://169.254.169.254/latest/meta-data/")
    assert decision.destinations == ["169.254.169.254"]


def test_explicit_allowlist_overrides_loopback_block() -> None:
    """An explicitly allowlisted loopback address passes."""
    policy = _policy(target_allowlist=["127.0.0.1"])
    assert policy.check("curl http://127.0.0.1:8080/").fingerprint


def test_explicit_allowlist_overrides_public_block() -> None:
    """An explicitly allowlisted public IP passes."""
    policy = _policy(target_allowlist=["8.8.8.8"])
    assert policy.check("curl https://8.8.8.8/").fingerprint


# --- family / phase / worker-scope permissions (step 6) ---------------------


def test_family_not_permitted_rejected() -> None:
    """A recon tool is rejected when only shell is allowed."""
    with pytest.raises(FamilyPermissionError, match="recon"):
        _policy(allowed_command_families=["shell"]).check("nmap -sV 10.0.0.5")


def test_family_permitted_accepted() -> None:
    """A recon tool passes when recon is allowed."""
    assert _policy(allowed_command_families=["recon"]).check("nmap -sV 10.0.0.5").family == "recon"


def test_phase_recon_blocks_exploit() -> None:
    """Exploit-family commands are rejected in the RECON phase."""
    policy = _policy()
    with pytest.raises(PhasePermissionError, match="phase RECON"):
        policy.check("sqlmap -u http://10.0.0.5/", phase="RECON")
    assert policy.check("nmap -sV 10.0.0.5", phase="recon").family == "recon"


def test_phase_exploitation_blocks_recon() -> None:
    """Recon-family commands are rejected in the EXPLOITATION phase."""
    policy = _policy()
    with pytest.raises(PhasePermissionError, match="phase EXPLOITATION"):
        policy.check("nmap -sV 10.0.0.5", phase="EXPLOITATION")
    assert policy.check("sqlmap -u http://10.0.0.5/", phase="EXPLOITATION").family == "exploit"


def test_unknown_phase_fails_closed() -> None:
    """An unknown phase permits nothing rather than guessing."""
    with pytest.raises(PhasePermissionError, match="unknown phase"):
        _policy().check("echo hi", phase="NONSENSE")


def test_done_phase_allows_nothing() -> None:
    """The DONE phase has no permitted command families."""
    with pytest.raises(PhasePermissionError):
        _policy().check("echo hi", phase="DONE")


def test_worker_scope_restricts_family() -> None:
    """A worker scope allows only its declared families (no implicit shell)."""
    policy = _policy()
    assert policy.check("nmap -sV 10.0.0.5", worker_scope="recon").family == "recon"
    with pytest.raises(FamilyPermissionError, match="exploit"):
        policy.check("sqlmap -u http://10.0.0.5/", worker_scope="recon")
    with pytest.raises(FamilyPermissionError):
        policy.check("nmap -sV 10.0.0.5", worker_scope="exploit")


def test_worker_scope_combined_with_phase() -> None:
    """Phase and worker scope both constrain the family."""
    policy = _policy()
    assert (
        policy.check(
            "sqlmap -u http://10.0.0.5/", phase="EXPLOITATION", worker_scope="exploit"
        ).family
        == "exploit"
    )
    with pytest.raises(PhasePermissionError):
        policy.check("sqlmap -u http://10.0.0.5/", phase="RECON", worker_scope="exploit")


def test_empty_worker_scope_denies_everything() -> None:
    """A blank worker scope fails closed."""
    with pytest.raises(FamilyPermissionError):
        _policy().check("echo hi", worker_scope="")


# --- fingerprint normalization (step 7) -------------------------------------


def test_normalize_collapses_whitespace_and_case() -> None:
    """Whitespace runs collapse and casing folds to one canonical form."""
    assert normalize_command("  echo   hello   world  ") == "echo hello world"
    assert normalize_command("ECHO HELLO WORLD") == "echo hello world"
    assert fingerprint_command("echo  hello") == fingerprint_command("echo hello")
    assert fingerprint_command("  ECHO   HELLO  ") == fingerprint_command("echo hello")


def test_normalize_unwraps_sh_c_wrapper() -> None:
    """sh -c / bash -c wrappers unwrap to the inner command."""
    assert normalize_command("sh -c 'echo hi'") == "echo hi"
    assert normalize_command('bash -c "echo hi"') == "echo hi"
    assert fingerprint_command("sh -c 'echo hi'") == fingerprint_command("echo hi")
    assert fingerprint_command('/bin/sh -c "echo hi;"') == fingerprint_command("echo hi")


def test_normalize_strips_trailing_shell_noise() -> None:
    """Trailing ;, &&, and || tokens are trivial shell noise."""
    assert normalize_command("echo hi;") == "echo hi"
    assert normalize_command("echo hi &&") == "echo hi"
    assert normalize_command("echo hi ||") == "echo hi"
    assert fingerprint_command("echo a && echo b") == fingerprint_command("echo a && echo b;")


def test_distinct_commands_fingerprint_distinctly() -> None:
    """Genuinely different commands produce different fingerprints."""
    assert fingerprint_command("echo hi")[1] != fingerprint_command("echo bye")[1]
    assert fingerprint_command("cat file")[1] != fingerprint_command("cat other")[1]


def test_fingerprint_is_deterministic_sha256() -> None:
    """The fingerprint is a stable 64-char sha256 hex digest of the canonical form."""
    canonical, digest = fingerprint_command("echo hello")
    assert canonical == "echo hello"
    assert len(digest) == 64
    assert digest == fingerprint_command("echo hello")[1]


# --- duplicate detection (step 8) -------------------------------------------


def test_duplicate_fingerprint_rejected() -> None:
    """Recording the same fingerprint twice fails loudly."""
    store = FingerprintStore()
    decision = _policy().check("echo hello")
    store.record(decision.fingerprint, canonical=decision.canonical)
    assert store.contains(decision.fingerprint)
    assert store.known == 1
    with pytest.raises(DuplicateActionError, match="already recorded"):
        store.record(decision.fingerprint, canonical=decision.canonical)
    assert store.known == 1


def test_distinct_commands_accepted_by_store() -> None:
    """Distinct fingerprints record side by side."""
    store = FingerprintStore()
    store.record("a" * 64, canonical="echo one")
    store.record("b" * 64, canonical="echo two")
    assert store.known == 2


def test_normalized_variants_dedupe_via_policy() -> None:
    """Whitespace/case variants of one command share a fingerprint and dedupe."""
    policy = _policy()
    store = FingerprintStore()
    first = policy.check("echo  hello")
    store.record(first.fingerprint, canonical=first.canonical)
    variant = policy.check("echo HELLO")
    assert variant.fingerprint == first.fingerprint
    with pytest.raises(DuplicateActionError):
        store.record(variant.fingerprint, canonical=variant.canonical)


def test_store_persists_to_jsonl(tmp_path: Path) -> None:
    """Approved fingerprints mirror to duplicates.jsonl in event-log style."""
    path = tmp_path / "duplicates.jsonl"
    decision = _policy().check("echo hello")
    FingerprintStore(path).record(decision.fingerprint, canonical=decision.canonical)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["fingerprint"] == decision.fingerprint
    assert record["canonical"] == "echo hello"
    assert "recorded_at" in record


def test_for_run_points_at_duplicates_jsonl(tmp_path: Path) -> None:
    """The standard run store lives at state_dir / duplicates.jsonl."""
    store = FingerprintStore.for_run(tmp_path)
    assert store.path == tmp_path / "duplicates.jsonl"


def test_store_reloads_from_disk(tmp_path: Path) -> None:
    """A reopened store remembers prior fingerprints and still rejects repeats."""
    path = tmp_path / "duplicates.jsonl"
    decision = _policy().check("echo hello")
    FingerprintStore(path).record(decision.fingerprint, canonical=decision.canonical)
    reloaded = FingerprintStore(path)
    assert reloaded.contains(decision.fingerprint)
    with pytest.raises(DuplicateActionError):
        reloaded.record(decision.fingerprint, canonical=decision.canonical)


def test_in_memory_store_requires_no_path() -> None:
    """A store without a path dedupes in memory only."""
    store = FingerprintStore()
    store.record("c" * 64, canonical="echo hi")
    assert store.path is None
    assert store.contains("c" * 64)


# --- failure paths and adversarial input ------------------------------------


def test_empty_command_rejected() -> None:
    """Empty and whitespace-only commands fail loudly at the gate."""
    policy = _policy()
    with pytest.raises(ScopeViolationError, match="empty"):
        policy.check("")
    with pytest.raises(ScopeViolationError, match="empty"):
        policy.check("   ")


def test_non_sha256_fingerprint_rejected() -> None:
    """A malformed fingerprint is refused by the store."""
    store = FingerprintStore()
    with pytest.raises(FingerprintStoreError, match="sha256"):
        store.record("short", canonical="echo hi")
    with pytest.raises(FingerprintStoreError):
        store.record("z" * 64, canonical="echo hi")


def test_corrupt_store_line_fails_loudly(tmp_path: Path) -> None:
    """A corrupt duplicates.jsonl line aborts reload instead of being ignored."""
    path = tmp_path / "duplicates.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(FingerprintStoreError, match="corrupt"):
        FingerprintStore(path)


def test_store_write_failure_is_loud(tmp_path: Path) -> None:
    """An unwritable store path fails loudly at record time."""
    path = tmp_path / "missing-dir" / "duplicates.jsonl"
    store = FingerprintStore(path)
    with pytest.raises(FingerprintStoreError):
        store.record("d" * 64, canonical="echo hi")


def test_adversarial_destination_hiding() -> None:
    """Userinfo tricks and command substitution do not hide destinations."""
    policy = _policy()
    # The authority is evil.com (userinfo '169.254.169.254@'), not the metadata IP.
    with pytest.raises(AllowlistViolationError):
        policy.check('curl "http://169.254.169.254@evil.com/"')
    # Command substitution still exposes the metadata endpoint.
    with pytest.raises(PlatformDestinationError):
        policy.check("echo $(curl http://169.254.169.254/)")
    # Trailing shell noise does not hide a public destination.
    with pytest.raises(PublicInternetError):
        policy.check("curl https://8.8.8.8/;")
    with pytest.raises(PublicInternetError):
        policy.check("ping 8.8.8.8;")


def test_ip_shaped_non_address_token_ignored() -> None:
    """Bracket tokens that are not valid IPs are not treated as destinations."""
    policy = _policy()
    assert policy.check("echo [abc:def]").destinations == []
    assert extract_destinations("echo [not-an-ip]") == []


def test_extract_destinations_variants() -> None:
    """Destination extraction covers URLs, ssh/scp targets, and bare IPs."""
    assert extract_destinations("curl http://10.0.0.5:8080/a b") == ["10.0.0.5"]
    assert extract_destinations("ssh user@challenge.local -p 22") == ["challenge.local"]
    assert extract_destinations("scp file.txt user@10.0.0.5:/tmp/x") == ["10.0.0.5"]
    assert extract_destinations("ping 10.0.0.5") == ["10.0.0.5"]
    assert extract_destinations("curl http://[fd00::1]:8080/") == ["fd00::1"]
    assert extract_destinations("echo no targets here") == []
    assert extract_destinations("ls -la") == []
    assert extract_destinations("nc -lvnp 4444") == []


def test_classify_family_deterministic() -> None:
    """Family classification is deterministic across wrappers and prefixes."""
    assert classify_family("nmap -sV host") == "recon"
    assert classify_family("sqlmap -u http://x/") == "exploit"
    assert classify_family("/usr/bin/curl -v https://x/") == "recon"
    assert classify_family("sudo nmap -sV host") == "recon"
    assert classify_family("sh -c 'sqlmap -u http://x/'") == "exploit"
    assert classify_family("echo hi") == "shell"
    assert classify_family("unknown-tool args") == "shell"


# --- check_then_run integration ---------------------------------------------


@pytest.mark.asyncio
async def test_check_then_run_executes_benign_command(tmp_path: Path) -> None:
    """The combined gate actually executes an approved command."""
    result = await check_then_run(
        "echo hello",
        policy=_policy(target_allowlist=[]),
        store=FingerprintStore(),
        runner=ShellRunner(),
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert not result.timeout_state


@pytest.mark.asyncio
async def test_check_then_run_rejects_duplicate(tmp_path: Path) -> None:
    """A normalized-repeat command is rejected before the second run."""
    policy = _policy(target_allowlist=[])
    store = FingerprintStore()
    runner = ShellRunner()
    await check_then_run(
        "echo hello",
        policy=policy,
        store=store,
        runner=runner,
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    with pytest.raises(DuplicateActionError):
        await check_then_run(
            "echo  HELLO ",  # same normalized command as the first run
            policy=policy,
            store=store,
            runner=runner,
            timeout_seconds=10,
            stdout_limit=1024,
            stderr_limit=1024,
            working_directory=tmp_path,
        )
    result = await check_then_run(
        "echo world",  # distinct command still executes
        policy=policy,
        store=store,
        runner=runner,
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert "world" in result.stdout
    assert store.known == 2


@pytest.mark.asyncio
async def test_check_then_run_gate_fails_before_spawn(tmp_path: Path) -> None:
    """A policy rejection records nothing and never reaches the runner."""
    store = FingerprintStore()
    with pytest.raises(CommandLengthError):
        await check_then_run(
            "echo hello",
            policy=_policy(max_command_length=4),
            store=store,
            runner=ShellRunner(),
            timeout_seconds=10,
            stdout_limit=1024,
            stderr_limit=1024,
            working_directory=tmp_path,
        )
    assert store.known == 0


# ---------------------------------------------------------------------------
# PR22: the executor can never reach a submit command
# ---------------------------------------------------------------------------


def test_executor_can_never_reach_a_submit_command_family() -> None:
    """Unsupported flag submissions: zero (docs/PRD.md success metric).

    No command family grants submission and no phase permits one, so a
    model-bound executor — which is constrained by the phase's permitted
    command families — can never reach a submit command through the
    policy gate. For every phase, a submit-shaped command is either
    rejected by the gate or classified into a family that is never a
    submission family (``submit``).
    """
    assert "submit" not in COMMAND_FAMILIES
    assert DEFAULT_FAMILY != "submit"
    policy = _policy()
    for phase in PHASES:
        try:
            decision = policy.check("halctl submit --flag 'flag{x}' --json", phase=phase)
        except ScopeViolationError:
            continue  # the gate rejected it — the executor cannot reach it
        assert decision.family != "submit"


def test_verify_and_submit_phase_permits_only_shell() -> None:
    """VERIFY_AND_SUBMIT allows only the shell family (PR22 invariant).

    The submission coordinator, not any command family, owns submission:
    even in the submission phase a model can only reach generic shell
    commands, and ``halctl submit`` itself is privilege-guarded at the
    wire (HalPrivilegeError), so no model path can submit a flag.
    """
    policy = _policy()
    # A read-only halctl command is fine in the phase ...
    decision = policy.check("halctl status --json", phase="VERIFY_AND_SUBMIT")
    assert decision.family == "shell"
    # ... but a submit attempt never classifies into a submission family.
    with pytest.raises(ScopeViolationError):
        policy.check("halctl submit --flag 'flag{x}' --json", phase="DONE")
