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

from ozzgraph.config import ConfigError, OzzGraphConfig
from ozzgraph.environments import (
    EnvironmentAdapter,
    HalCTFEnvironment,
    LocalEnvironment,
    Objective,
    Scope,
    Target,
)
from ozzgraph.environments.base import EnvironmentAdapter as ProtocolAdapter
from ozzgraph.environments.halctf import HALCTF_OBJECTIVE_ID
from ozzgraph.environments.local import (
    DEFAULT_LOCAL_CAPABILITIES,
    LOCAL_OBJECTIVE_ID,
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
    assert targets[0].metadata == {"source": "target_env"}
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
# HalCTFEnvironment (minimal V01 slice)
# ---------------------------------------------------------------------------


def test_halctf_requires_challenge_id() -> None:
    env = HalCTFEnvironment(_config(), environ={})
    assert env.challenge_id == ""


@pytest.mark.asyncio
async def test_halctf_targets_require_challenge_id() -> None:
    env = HalCTFEnvironment(_config(), environ={})
    with pytest.raises(ConfigError, match="OZZGRAPH_CHALLENGE_ID"):
        await env.discover_targets()


@pytest.mark.asyncio
async def test_halctf_yields_one_challenge_target() -> None:
    env = HalCTFEnvironment(_config(), environ={"OZZGRAPH_CHALLENGE_ID": "web-01"})
    scope = await env.discover_scope()
    assert scope.name == "halctf"
    assert scope.constraints["challenge_id"] == "web-01"
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
    assert await env.discover_capabilities()
    await env.aclose()  # idempotent no-op


@pytest.mark.asyncio
async def test_halctf_uses_os_environ_by_default(monkeypatch) -> None:
    monkeypatch.setenv("OZZGRAPH_CHALLENGE_ID", "pwn-02")
    env = HalCTFEnvironment(_config())
    targets = await env.discover_targets()
    assert targets[0].address == "pwn-02"
