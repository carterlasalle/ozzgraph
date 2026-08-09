"""Tests for the V01 environment adapters (docs/adr/0008).

Covers the Pydantic v2 models (Scope/Target/Objective), the
EnvironmentAdapter protocol contract, LocalEnvironment's deterministic
derivation from configuration (scope surface classification, target
env-variable parsing with allowlist fallback, the single generic
objective, conservative capabilities), and HalCTFEnvironment's minimal
V01 slice (one challenge target, the flag objective, loud failure
without a challenge id).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ozzgraph.config import DEFAULT_FLAG_PATTERN, ConfigError, OzzGraphConfig
from ozzgraph.environments import (
    EnvironmentAdapter,
    HalCTFEnvironment,
    LocalEnvironment,
    Objective,
    Scope,
    Target,
)
from ozzgraph.environments.base import EnvironmentAdapter as ProtocolAdapter
from ozzgraph.environments.halctf import HALCTF_OBJECTIVE_ID, HALCTF_OBJECTIVE_SUCCESS_HINT
from ozzgraph.environments.local import (
    DEFAULT_LOCAL_CAPABILITIES,
    LOCAL_OBJECTIVE_ID,
    classify_local_target,
)


def _config(**overrides) -> OzzGraphConfig:
    base: dict[str, object] = {
        "hal_user_id": "user-42",
        "state_dir": Path("/tmp/ozzgraph-test-state"),
        "artifact_dir": Path("/tmp/ozzgraph-test-state/artifacts"),
    }
    base.update(overrides)
    return OzzGraphConfig(**base)  # type: ignore[arg-type] - test helper


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_scope_model_is_strict_and_typed() -> None:
    scope = Scope(
        name="local",
        hosts=("10.0.0.5", "intranet.example"),
        urls=("http://10.0.0.5:3000",),
        networks=("10.0.0.0/24",),
        credentials=("cred-web-1",),
        constraints={"max_command_length": 4096},
    )
    assert scope.name == "local"
    assert scope.hosts == ("10.0.0.5", "intranet.example")
    assert scope.urls == ("http://10.0.0.5:3000",)
    assert scope.networks == ("10.0.0.0/24",)
    assert scope.credentials == ("cred-web-1",)
    with pytest.raises(ValidationError):
        Scope(name="", hosts=())  # name must be non-empty
    with pytest.raises(ValidationError):
        Scope(name="x", hosts=(), bogus=True)  # type: ignore[call-arg] - extra="forbid"


def test_target_model_is_strict_and_typed() -> None:
    target = Target(
        id="target-url-abc",
        type="url",
        address="http://10.0.0.5:3000",
        metadata={"challenge_id": "web-01"},
    )
    assert target.type == "url"
    assert target.address == "http://10.0.0.5:3000"
    with pytest.raises(ValidationError):
        Target(id="t", type="url", address="")  # address must be non-empty
    with pytest.raises(ValidationError):
        Target(id="t", type="url", address="x", bogus=1)  # type: ignore[call-arg] - extra="forbid"


def test_objective_model_is_strict_and_typed() -> None:
    objective = Objective(
        id="objective-1",
        description="Complete the assessment",
        success_hint="submission accepted",
    )
    assert objective.completed is False
    assert objective.completed_at is None
    done = objective.model_copy(update={"completed": True})
    assert done.completed is True
    with pytest.raises(ValidationError):
        Objective(id="objective-1", description="")  # description required
    with pytest.raises(ValidationError):
        Objective(id="objective-1", description="x", bogus=1)  # type: ignore[call-arg] - extra="forbid"


def test_environment_adapter_protocol_contract() -> None:
    """The protocol declares exactly the five async discovery methods.

    It is intentionally NOT ``@runtime_checkable`` (documented in
    docs/adr/0008): isinstance checks on an async protocol only verify
    attribute presence, never coroutine-ness, so the harness constructs
    concrete adapters explicitly and mypy checks the contract.
    """
    expected = {
        "discover_scope",
        "discover_targets",
        "discover_objectives",
        "discover_capabilities",
        "aclose",
    }
    assert expected <= set(ProtocolAdapter.__dict__)
    assert hasattr(ProtocolAdapter, "_is_protocol")  # typing.Protocol
    assert EnvironmentAdapter is ProtocolAdapter


# ---------------------------------------------------------------------------
# LocalEnvironment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_scope_derives_surface_from_allowlist() -> None:
    env = LocalEnvironment(
        _config(
            target_allowlist=(
                "10.0.0.0/24",
                "http://10.0.0.5:3000",
                "intranet.example",
            )
        ),
        environ={},
    )
    scope = await env.discover_scope()
    assert scope.name == "local"
    assert scope.hosts == ("intranet.example",)  # sorted, classified as host
    assert scope.urls == ("http://10.0.0.5:3000",)  # URL scheme -> url
    assert scope.networks == ("10.0.0.0/24",)  # CIDR -> network
    assert scope.credentials == ()
    assert scope.constraints["max_command_length"] == 4096


@pytest.mark.asyncio
async def test_local_scope_empty_allowlist_fails_closed() -> None:
    env = LocalEnvironment(_config(), environ={})
    scope = await env.discover_scope()
    assert scope.hosts == ()
    assert scope.urls == ()
    assert scope.networks == ()


@pytest.mark.asyncio
async def test_local_targets_from_env_variables() -> None:
    env = LocalEnvironment(
        _config(target_allowlist=("10.0.0.0/24",)),
        environ={
            "OZZGRAPH_TARGET": "http://10.0.0.5:3000",
            "OZZGRAPH_TARGET_DNS": "intranet.example",
        },
    )
    targets = await env.discover_targets()
    assert [t.address for t in targets] == ["http://10.0.0.5:3000", "intranet.example"]
    assert [t.type for t in targets] == ["url", "host"]
    assert targets[0].metadata == {"source": "target_env", "mode": "url"}
    assert targets[1].metadata == {"source": "target_env", "mode": "host"}
    # Deterministic ids: stable across calls.
    assert [t.id for t in targets] == [t.id for t in await env.discover_targets()]


@pytest.mark.asyncio
async def test_local_targets_fall_back_to_allowlist() -> None:
    env = LocalEnvironment(
        _config(target_allowlist=("10.0.0.0/24", "http://10.0.0.5:3000")),
        environ={},
    )
    targets = await env.discover_targets()
    assert [t.type for t in targets] == ["network", "url"]
    assert [t.address for t in targets] == ["10.0.0.0/24", "http://10.0.0.5:3000"]


@pytest.mark.asyncio
async def test_local_targets_none_configured() -> None:
    env = LocalEnvironment(_config(), environ={})
    assert await env.discover_targets() == []


@pytest.mark.asyncio
async def test_local_targets_malformed_env_fails_loudly() -> None:
    env = LocalEnvironment(_config(), environ={"OZZGRAPH_TARGET_WEIRD": "x"})
    with pytest.raises(ConfigError, match="unsupported target namespace"):
        await env.discover_targets()


@pytest.mark.asyncio
async def test_local_objectives_and_capabilities() -> None:
    env = LocalEnvironment(_config(), environ={})
    objectives = await env.discover_objectives()
    assert len(objectives) == 1
    assert objectives[0].id == LOCAL_OBJECTIVE_ID
    assert objectives[0].completed is False
    assert objectives[0].completed_at is None
    capabilities = await env.discover_capabilities()
    assert capabilities == set(DEFAULT_LOCAL_CAPABILITIES)
    assert "http.request" in capabilities
    await env.aclose()  # idempotent no-op


# ---------------------------------------------------------------------------
# HalCTFEnvironment (V09 full adapter, docs/adr/0011)
# ---------------------------------------------------------------------------

_HALCTF_ENDPOINT = "http://127.0.0.1:9000/mcp"


def _halctf_env(**overrides) -> dict[str, str]:
    """A minimal HalCTF environment mapping (challenge id + endpoint)."""
    env = {"OZZGRAPH_CHALLENGE_ID": "web-01", "OZZGRAPH_MCP_BASE_URL": _HALCTF_ENDPOINT}
    env.update(overrides)
    return env


def test_halctf_constructs_without_endpoint() -> None:
    """HAL-002: HalCTF mode without a discoverable MCP endpoint is NOT
    a construction error — the environment builds with endpoint None
    (MCP is optional enrichment/fallback; env metadata drives the run)."""
    environment = HalCTFEnvironment(_config(), environ={"HAL_CTF_ID": "web-01"})
    assert environment.endpoint is None
    assert environment.challenge_id == "web-01"
    assert environment.snapshot.challenge_id == "web-01"


def test_halctf_requires_challenge_id() -> None:
    env = HalCTFEnvironment(_config(), environ=_halctf_env())
    assert env.challenge_id == "web-01"
    assert env.endpoint == _HALCTF_ENDPOINT


@pytest.mark.asyncio
async def test_halctf_targets_require_challenge_id() -> None:
    env = HalCTFEnvironment(_config(), environ=_halctf_env(OZZGRAPH_CHALLENGE_ID=""))
    with pytest.raises(ConfigError, match="challenge id"):
        await env.discover_targets()


@pytest.mark.asyncio
async def test_halctf_yields_one_challenge_target() -> None:
    env = HalCTFEnvironment(_config(), environ=_halctf_env())
    scope = await env.discover_scope()
    assert scope.name == "halctf"
    assert scope.constraints["challenge_id"] == "web-01"
    assert scope.constraints["mode"] == "halctf"
    targets = await env.discover_targets()
    assert len(targets) == 1
    target = targets[0]
    assert target.id == "halctf-challenge-web-01"
    assert target.address == "web-01"
    assert target.metadata == {"challenge_id": "web-01"}
    objectives = await env.discover_objectives()
    assert len(objectives) == 1
    assert objectives[0].id == HALCTF_OBJECTIVE_ID
    assert "flag" in objectives[0].description
    # V09: the objective names its deterministic completion signal.
    assert objectives[0].success_hint == HALCTF_OBJECTIVE_SUCCESS_HINT
    assert await env.discover_capabilities()
    await env.aclose()  # idempotent no-op


@pytest.mark.asyncio
async def test_halctf_uses_os_environ_by_default(monkeypatch) -> None:
    monkeypatch.setenv("OZZGRAPH_CHALLENGE_ID", "pwn-02")
    monkeypatch.setenv("OZZGRAPH_MCP_BASE_URL", _HALCTF_ENDPOINT)
    env = HalCTFEnvironment(_config())
    targets = await env.discover_targets()
    assert targets[0].address == "pwn-02"


def test_halctf_discovery_from_hal_vars() -> None:
    """V09: HAL_* runtime variables drive discovery (challenge id + endpoint)."""
    env = HalCTFEnvironment(
        _config(),
        environ={
            "HAL_CTF_ID": "crypto-03",
            "HAL_MCP_ENDPOINT": "http://halctf:9000/mcp",
        },
    )
    assert env.challenge_id == "crypto-03"
    assert env.endpoint == "http://halctf:9000/mcp"


def test_halctf_endpoint_candidate_priority() -> None:
    """V09: the first non-blank endpoint candidate wins deterministically."""
    env = HalCTFEnvironment(
        _config(),
        environ={
            "HAL_CTF_ID": "web-01",
            "OZZGRAPH_MCP_BASE_URL": "http://explicit:9000/mcp",
            "HAL_MCP_ENDPOINT": "http://halctf:9000/mcp",
            "OPENAI_BASE_URL": "http://openai:8000/v1",
        },
    )
    assert env.endpoint == "http://explicit:9000/mcp"


def test_halctf_openai_base_url_is_not_mcp_endpoint() -> None:
    """HAL-002: OPENAI_BASE_URL is the model service (/llm), never the
    MCP server (/mcp/) — it never resolves the environment endpoint."""
    environment = HalCTFEnvironment(
        _config(),
        environ={"HAL_CHALLENGE_ID": "web-01", "OPENAI_BASE_URL": "http://platform:9000/mcp"},
    )
    assert environment.endpoint is None
    assert environment.challenge_id == "web-01"


def test_halctf_environment_service_factories() -> None:
    """V09: the environment provides the HalCTF-owned services wired to
    its challenge id and the config's budgets (docs/adr/0011)."""
    env = HalCTFEnvironment(_config(), environ=_halctf_env())

    class _FakeClient:
        privileged = True

        async def submit_flag(self, challenge_id: str, flag: str):  # pragma: no cover
            raise AssertionError("not called")

        async def request_hint(self, challenge_id: str, index: int):  # pragma: no cover
            raise AssertionError("not called")

        async def get_scoreboard(self):  # pragma: no cover
            raise AssertionError("not called")

        async def aclose(self) -> None:
            return None

    client = _FakeClient()
    submission = env.submission_coordinator(client=client, run_id="run-1")
    assert submission._challenge_id == "web-01"  # type: ignore[attr-defined]
    assert submission._max_submissions == 3  # type: ignore[attr-defined]
    hint = env.hint_coordinator(client=client, run_id="run-1")
    assert hint._challenge_id == "web-01"  # type: ignore[attr-defined]
    assert hint._policy._max_hints == 1  # type: ignore[attr-defined]
    extractor = env.flag_extractor(run_id="run-1")
    assert extractor._pattern.pattern == DEFAULT_FLAG_PATTERN  # type: ignore[attr-defined]
    scoreboard = env.scoreboard_coordinator(client=client, run_id="run-1")
    assert scoreboard._client is client  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# V08 local-assessment modes (docs/adr/0010)
# ---------------------------------------------------------------------------


def test_classify_local_target_keeps_classic_shapes() -> None:
    """URLs, CIDRs, and hosts classify exactly as before."""
    assert classify_local_target("http://10.0.0.5:3000") == "url"
    assert classify_local_target("https://intranet.example/admin") == "url"
    assert classify_local_target("10.0.0.0/24") == "network"
    assert classify_local_target("10.0.0.5") == "host"
    assert classify_local_target("intranet.example") == "host"
    assert classify_local_target("10.0.0.5:3000") == "host"  # host:port, not CIDR


def test_classify_local_target_repository(tmp_path: Path, monkeypatch) -> None:
    """A path containing .git classifies as a repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert classify_local_target(str(repo)) == "repo"
    # A relative path with an embedded separator also resolves.
    monkeypatch.chdir(tmp_path)
    assert classify_local_target(f"./{repo.name}") == "repo"


def test_classify_local_target_compose(tmp_path: Path) -> None:
    """A path containing a compose file classifies as a compose project."""
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    assert classify_local_target(str(stack)) == "compose"


def test_classify_local_target_missing_path_fails_loudly(tmp_path: Path) -> None:
    """A nonexistent path-like target is a loud ConfigError."""
    with pytest.raises(ConfigError, match="does not exist"):
        classify_local_target(str(tmp_path / "nope"))


def test_classify_local_target_plain_dir_fails_loudly(tmp_path: Path) -> None:
    """An existing directory that is neither repo nor compose is rejected."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="neither a git repository"):
        classify_local_target(str(plain))


def test_classify_local_target_file_fails_loudly(tmp_path: Path) -> None:
    """A path-like target that is a file, not a directory, is rejected."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a directory"):
        classify_local_target(str(file_path))


@pytest.mark.asyncio
async def test_local_repository_target_via_environment(tmp_path: Path) -> None:
    """OZZGRAPH_TARGET pointing at a git repo yields a repository target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    env = LocalEnvironment(_config(), environ={"OZZGRAPH_TARGET": str(repo)})
    targets = await env.discover_targets()
    assert len(targets) == 1
    assert targets[0].type == "repo"
    assert targets[0].address == str(repo)
    assert targets[0].metadata == {"source": "target_env", "mode": "repository"}
    scope = await env.discover_scope()
    assert scope.constraints["mode"] == "repository"
    assert scope.constraints["target_modes"] == ["repository"]


@pytest.mark.asyncio
async def test_local_compose_target_via_environment(tmp_path: Path) -> None:
    """OZZGRAPH_TARGET pointing at a compose project yields a compose target."""
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    env = LocalEnvironment(_config(), environ={"OZZGRAPH_TARGET": str(stack)})
    targets = await env.discover_targets()
    assert len(targets) == 1
    assert targets[0].type == "compose"
    assert targets[0].metadata["mode"] == "docker-compose"


@pytest.mark.asyncio
async def test_local_bad_repository_path_fails_loudly(tmp_path: Path) -> None:
    """A configured repository path that is invalid raises ConfigError."""
    env = LocalEnvironment(_config(), environ={"OZZGRAPH_TARGET": str(tmp_path / "missing-repo")})
    with pytest.raises(ConfigError, match="does not exist"):
        await env.discover_targets()


@pytest.mark.asyncio
async def test_local_cidr_target_via_environment() -> None:
    """A CIDR in OZZGRAPH_TARGET classifies as a network target."""
    env = LocalEnvironment(_config(), environ={"OZZGRAPH_TARGET": "10.0.0.0/24"})
    targets = await env.discover_targets()
    assert targets[0].type == "network"
    assert targets[0].metadata["mode"] == "network"


@pytest.mark.asyncio
async def test_local_hybrid_scope_from_mixed_targets(tmp_path: Path) -> None:
    """Targets of mixed types yield a hybrid scope."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    env = LocalEnvironment(
        _config(target_allowlist=("http://127.0.0.1:3000", str(repo))),
        environ={},
    )
    scope = await env.discover_scope()
    assert scope.constraints["mode"] == "hybrid"
    assert scope.constraints["target_modes"] == ["repository", "url"]
    targets = await env.discover_targets()
    assert sorted(target.type for target in targets) == ["repo", "url"]
    # Repo/compose paths are local surfaces: never network buckets.
    assert str(repo) not in scope.hosts
    assert str(repo) not in scope.urls
    assert scope.urls == ("http://127.0.0.1:3000",)


@pytest.mark.asyncio
async def test_local_scope_mode_reflects_single_target_type() -> None:
    """A single-type target set yields that mode (not hybrid)."""
    env = LocalEnvironment(
        _config(target_allowlist=("http://127.0.0.1:3000",)),
        environ={},
    )
    scope = await env.discover_scope()
    assert scope.constraints["mode"] == "url"
    assert scope.constraints["target_modes"] == ["url"]


@pytest.mark.asyncio
async def test_local_scope_mode_none_without_targets() -> None:
    """No targets means scope mode ``none``."""
    env = LocalEnvironment(_config(), environ={})
    scope = await env.discover_scope()
    assert scope.constraints["mode"] == "none"
    assert scope.constraints["target_modes"] == []
