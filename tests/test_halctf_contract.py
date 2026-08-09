"""HAL-011 halctf-real-contract regression tests (docs/CHANGES_v2.md HAL-011).

The existing benchmark/suite drives the kernel against synthetic lab
targets + scripted models, not an actual HalCTF runtime contract.
These tests stand up the :mod:`halctf_contract_fixture` — Tottori's
exact platform-injected env shape plus the observed target and sidecar
HTTP responses, all as REAL plain-HTTP listeners — and prove the FULL
harness (a real ``python -m ozzgraph`` child process, the HAL-001..010
production composition) scores and COMPLETEs against it:

1. The fixture env reproduces Tottori's exact shape (named
   ``HAL_TARGET_*_IP``/``_PORT`` pairs, ``HAL_CHALLENGE_ID=18``,
   challenge metadata, runtime identity, ``OPENAI_BASE_URL``,
   ``MCP_ENDPOINT``) and the fixture servers speak the observed wire
   contract: ``GET /fetch`` -> 403/404/502/200 and ``POST /submit`` ->
   ``{"status": "correct", "points_awarded": 1}``.
2. Against the fixture, discovery yields REAL URL targets
   (``http://IP:PORT``, never the challenge id), the scope allowlist
   admits them (no allowlist refusal), and the model routing sources
   from ``HAL_AGENT_MODEL`` + ``OPENAI_BASE_URL`` (HAL-003).
3. The full-harness child process against the fixture terminates
   COMPLETED (exit 0, ``TERMINATION: completed``), scored through an
   accepted sidecar submission (``objective-halctf-flag`` completed,
   ``submission.accepted`` in the run log, ``findings.json`` written)
   — NOT unexhausted-complete (HAL-006: completion is acceptance-
   gated) and NOT allowlist-refused.
4. The negative control is deterministic: without the fixture's
   ``HAL_TARGET_*`` services the target IS the bare challenge id (the
   V09 fallback the fixture replaces), and a non-allowlisted policy
   refuses the same curl the fixture's allowlist admits.

Style mirrors tests/test_e2e_run.py (subprocess harness, scripted
model, graph/event assertions) and tests/test_sidecar.py (the sidecar
wire shape).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

import pytest
from halctf_contract_fixture import (
    FETCH_STATUSES,
    SIDECAR_SUBMIT_RESPONSE,
    TOTTORI_AGENT_MODEL,
    TOTTORI_CHALLENGE_CATEGORY,
    TOTTORI_CHALLENGE_ID,
    TOTTORI_CHALLENGE_NAME,
    TOTTORI_FLAG,
    TOTTORI_FLAG_LIKE,
    TOTTORI_RUN_ID,
    TOTTORI_TEAM_UUID,
    ContractSidecarServer,
    HalctfTargetServer,
    ScriptedModelServer,
    tottori_env,
)

from ozzgraph.config import (
    build_halctf_runtime_snapshot,
    discover_halctf_services,
    halctf_target_allowlist,
    load_config,
)
from ozzgraph.environments import HalCTFEnvironment
from ozzgraph.environments.halctf.sidecar import discover_halctf_sidecar_base_url
from ozzgraph.policy import AllowlistViolationError, PlatformDestinationError, ScopePolicy
from ozzgraph.state_graph import StateGraph

_RUNTIME_S = 120


def _get_status(url: str) -> int:
    """The HTTP status of a GET, including non-2xx (urllib raises)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    """POST a JSON body; returns (status, parsed body), non-2xx included."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _child_env(state_dir: Path, platform_env: dict[str, str]) -> dict[str, str]:
    """A clean child environment: the fixture's platform env + harness knobs.

    Filters inherited ``OZZGRAPH_*`` / ``HAL_*`` knobs like
    tests/test_e2e_run.py's hermetic env builders, then layers the
    fixture's platform env on top. ``OZZGRAPH_MODEL_BASE_URL`` /
    ``OZZGRAPH_MODEL_ID`` are deliberately NOT set: the run must route
    the model client from ``HAL_AGENT_MODEL`` + ``OPENAI_BASE_URL``
    (HAL-003), or the scripted endpoint is never reached and the run
    fails to complete.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("OZZGRAPH_", "HAL_"))
    }
    env.update(platform_env)
    env.update(
        {
            "HAL_USER_ID": "user-42",
            "OZZGRAPH_STATE_DIR": str(state_dir),
            "OZZGRAPH_MAX_RUNTIME_S": str(_RUNTIME_S),
            "OZZGRAPH_HEARTBEAT_INTERVAL_S": "300",
            # The env-only bootstrap's best-effort MCP status/hint calls
            # (the fixture's MCP endpoint is the sidecar origin + /mcp,
            # which 404s) fail instantly instead of backing off.
            "OZZGRAPH_MCP_MAX_RETRIES": "0",
            "OZZGRAPH_MCP_TIMEOUT_S": "1",
        }
    )
    return env


def _run_ozzgraph(
    *args: str,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Spawn ``python -m ozzgraph ...`` as a REAL child process."""
    return subprocess.run(
        [sys.executable, "-m", "ozzgraph", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )


class _EntitySnapshot(NamedTuple):
    """One graph entity as the test reads it (id + payload)."""

    id: str
    data: dict[str, object]


def _graph_entity_types(state_dir: Path) -> dict[str, list[_EntitySnapshot]]:
    """Group the run's SQLite graph entities by type."""

    async def _read() -> dict[str, list[_EntitySnapshot]]:
        grouped: dict[str, list[_EntitySnapshot]] = {}
        async with StateGraph(state_dir / "graph.db") as graph:
            for record in await graph.list_entities():
                grouped.setdefault(record.type, []).append(_EntitySnapshot(record.id, record.data))
        return grouped

    return asyncio.run(_read())


def _event_types(state_dir: Path) -> list[dict[str, object]]:
    """Every run-log event from ``state_dir/actions.jsonl``."""
    path = state_dir / "actions.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _fixture_platform_env() -> dict[str, str]:
    """The fixture env with fixed (shape-test) endpoint URLs."""
    return tottori_env(
        ferry_url="http://10.0.0.11:8080",
        underworld_url="http://10.0.0.12:3000",
        model_base_url="http://127.0.0.1:9000/v1",
        sidecar_base_url="http://127.0.0.1:9000",
        mcp_endpoint="http://127.0.0.1:9000/mcp",
    )


# ---------------------------------------------------------------------------
# 1. the fixture reproduces Tottori's exact env shape and wire responses
# ---------------------------------------------------------------------------


def test_fixture_env_reproduces_tottori_platform_shape() -> None:
    """The fixture env dict is exactly the platform-injected shape.

    Named ``HAL_TARGET_<NAME>_IP``/``_PORT`` pairs, challenge metadata
    (``HAL_CHALLENGE_ID=18``), runtime identity, flag-like values, and
    the model/MCP infrastructure endpoints — the HAL-001 verified shape
    from Tottori's committed live-run logs.
    """
    env = _fixture_platform_env()
    assert env == {
        "HAL_TARGET_FERRY_IP": "10.0.0.11",
        "HAL_TARGET_FERRY_PORT": "8080",
        "HAL_TARGET_UNDERWORLD_IP": "10.0.0.12",
        "HAL_TARGET_UNDERWORLD_PORT": "3000",
        "HAL_CHALLENGE_ID": TOTTORI_CHALLENGE_ID,
        "HAL_CHALLENGE_NAME": TOTTORI_CHALLENGE_NAME,
        "HAL_CHALLENGE_CATEGORY": TOTTORI_CHALLENGE_CATEGORY,
        "HAL_AGENT_MODEL": TOTTORI_AGENT_MODEL,
        "HAL_RUN_ID": TOTTORI_RUN_ID,
        "HAL_TEAM_UUID": TOTTORI_TEAM_UUID,
        "BONUS_FLAG": TOTTORI_FLAG_LIKE[0],
        "FLAG_MAIN": TOTTORI_FLAG,
        "OPENAI_BASE_URL": "http://127.0.0.1:9000/v1",
        "MCP_ENDPOINT": "http://127.0.0.1:9000/mcp",
        "OZZGRAPH_SIDECAR_BASE_URL": "http://127.0.0.1:9000",
    }

    # The named service pairs parse into ordered services, and the
    # derived allowlist carries each bare IP + IP:PORT authority.
    services = discover_halctf_services(env)
    assert [(s.name, s.ip, s.port) for s in services] == [
        ("ferry", "10.0.0.11", 8080),
        ("underworld", "10.0.0.12", 3000),
    ]
    assert halctf_target_allowlist(env) == (
        "10.0.0.11",
        "10.0.0.11:8080",
        "10.0.0.12",
        "10.0.0.12:3000",
    )

    # The full runtime snapshot parses the platform shape, and the
    # sidecar resolves env-first (HAL-004).
    snapshot = build_halctf_runtime_snapshot(env)
    assert snapshot.challenge_id == TOTTORI_CHALLENGE_ID
    assert snapshot.challenge_name == TOTTORI_CHALLENGE_NAME
    assert snapshot.challenge_category == TOTTORI_CHALLENGE_CATEGORY
    assert snapshot.agent_model == TOTTORI_AGENT_MODEL
    assert snapshot.run_id == TOTTORI_RUN_ID
    assert snapshot.team_uuid == TOTTORI_TEAM_UUID
    assert snapshot.flag_like == TOTTORI_FLAG_LIKE
    assert snapshot.openai_base_url == "http://127.0.0.1:9000/v1"
    assert snapshot.mcp_endpoint == "http://127.0.0.1:9000/mcp"
    assert discover_halctf_sidecar_base_url(env) == "http://127.0.0.1:9000"


def test_fixture_servers_serve_observed_wire_responses(tmp_path: Path) -> None:
    """The fixture's real listeners speak the observed HTTP contract.

    The target serves the scripted ``GET /fetch`` statuses
    403/404/502/200 (the 200 path carrying the challenge flag), and the
    sidecar answers ``POST /submit`` with the exact observed
    ``{"status": "correct", "points_awarded": 1}`` and ``POST /done``
    with 200 — while the MCP ``/mcp`` path (sharing the origin) is
    NOT the sidecar surface.
    """
    ferry = HalctfTargetServer(service="ferry")
    underworld = HalctfTargetServer(service="underworld")
    sidecar = ContractSidecarServer()
    with ferry, underworld, sidecar:
        # The observed target /fetch surface: one status per path.
        assert dict(FETCH_STATUSES) == {
            "/fetch": 403,
            "/fetch/missing": 404,
            "/fetch/down": 502,
            "/fetch/ok": 200,
        }
        for path, status in FETCH_STATUSES.items():
            assert _get_status(ferry.base_url + path) == status, path
        # The 200 path serves the challenge flag in the body.
        with urllib.request.urlopen(ferry.base_url + "/fetch/ok", timeout=10) as response:
            assert TOTTORI_FLAG in response.read().decode("utf-8")

        # The observed sidecar wire responses.
        status, body = _post_json(
            sidecar.base_url + "/submit",
            {"challenge_id": TOTTORI_CHALLENGE_ID, "flag": TOTTORI_FLAG},
        )
        assert status == 200
        assert body == SIDECAR_SUBMIT_RESPONSE
        assert sidecar.submits == [{"challenge_id": TOTTORI_CHALLENGE_ID, "flag": TOTTORI_FLAG}]
        status, body = _post_json(sidecar.base_url + "/done", {"run_id": "run-1"})
        assert status == 200
        assert body == {"ok": True}
        assert sidecar.dones == [{"run_id": "run-1"}]
        # /mcp is the MCP server's path, not the sidecar surface.
        assert _get_status(sidecar.base_url + "/mcp") == 404

        # Both named services are real listeners.
        assert ferry.base_url != underworld.base_url
        # The target recorded every /fetch probe it served (the E2E
        # test asserts the harness's exact probe sequence against a
        # fresh listener).
        fetch_paths = {path for _, path in ferry.requests if path.startswith("/fetch")}
        assert fetch_paths == set(FETCH_STATUSES)


# ---------------------------------------------------------------------------
# 2. discovery against the fixture: real URLs, accepted allowlist, routing
# ---------------------------------------------------------------------------


def test_targets_are_real_urls_allowlisted_and_model_routed(tmp_path: Path) -> None:
    """Against the fixture the targets are REAL URLs, never the id.

    Each ``HAL_TARGET_*`` pair becomes a URL target carrying its
    service name + challenge id metadata; the scope carries the merged
    allowlist so the policy gate admits the exact curl commands the
    run will propose (no allowlist refusal); and the model routing
    sources from ``HAL_AGENT_MODEL`` + ``OPENAI_BASE_URL`` — the
    ``(model_id, base_url)`` pair ``Supervisor._model_routing`` returns
    in HalCTF mode (HAL-003).
    """
    ferry = HalctfTargetServer(service="ferry")
    underworld = HalctfTargetServer(service="underworld")
    with ferry, underworld:
        env = tottori_env(
            ferry_url=ferry.base_url,
            underworld_url=underworld.base_url,
            model_base_url="http://127.0.0.1:9000/v1",
            sidecar_base_url="http://127.0.0.1:9000",
            mcp_endpoint="http://127.0.0.1:9000/mcp",
        )
        config = load_config(environ={"HAL_USER_ID": "user-42", **env})
        environment = HalCTFEnvironment(config, environ=env)

        targets = asyncio.run(environment.discover_targets())
        assert [t.address for t in targets] == [ferry.base_url, underworld.base_url]
        assert all(t.type == "url" for t in targets)
        assert all(t.address != TOTTORI_CHALLENGE_ID for t in targets)
        assert [t.id for t in targets] == ["halctf-service-ferry", "halctf-service-underworld"]
        assert targets[0].metadata == {"service": "ferry", "challenge_id": TOTTORI_CHALLENGE_ID}

        scope = asyncio.run(environment.discover_scope())
        assert scope.hosts == ("127.0.0.1",)
        assert set(scope.urls) == {ferry.base_url, underworld.base_url}
        allowlist = scope.constraints["target_allowlist"]
        assert isinstance(allowlist, tuple)
        assert "127.0.0.1" in allowlist
        assert ferry.base_url.removeprefix("http://") in allowlist
        assert underworld.base_url.removeprefix("http://") in allowlist
        assert scope.constraints["challenge_id"] == TOTTORI_CHALLENGE_ID
        assert scope.constraints["mode"] == "halctf"

        # The gate admits the exact probes the scripted model proposes
        # (the fixture-derived allowlist is what prevents a refusal).
        policy = ScopePolicy(target_allowlist=config.target_allowlist)
        decision = policy.check(f"curl -sS --max-time 5 {ferry.base_url}/fetch")
        assert decision.destinations == ["127.0.0.1"]
        policy.check(f"curl -sS --max-time 5 {ferry.base_url}/fetch/ok")

        # Model routing (HAL-003): the same snapshot values the
        # supervisor's _model_routing returns drive the model client.
        snapshot = build_halctf_runtime_snapshot(env)
        assert (snapshot.agent_model, snapshot.openai_base_url) == (
            TOTTORI_AGENT_MODEL,
            "http://127.0.0.1:9000/v1",
        )


def test_negative_control_allowlist_refusal_and_challenge_id_fallback(tmp_path: Path) -> None:
    """Without the fixture's allowlist the same probe is refused.

    The deterministic negative control: the acceptance in the positive
    path comes from the fixture-derived allowlist — an empty allowlist
    refuses the very curl the fixture admits (``AllowlistViolationError``,
    the fail-closed gate), and an env WITHOUT ``HAL_TARGET_*`` services
    keeps the V09 fallback: the target address IS the bare challenge id
    (``"18"``), never a real URL.
    """
    ferry = HalctfTargetServer(service="ferry")
    with ferry:
        # A loopback destination without the allowlist is refused as a
        # blocked platform address (fail-closed); a private non-
        # allowlisted destination is refused as an allowlist violation.
        with pytest.raises(PlatformDestinationError):
            ScopePolicy(target_allowlist=()).check(f"curl -sS --max-time 5 {ferry.base_url}/fetch")
        with pytest.raises(AllowlistViolationError):
            ScopePolicy(target_allowlist=()).check(
                "curl -sS --max-time 5 http://10.0.0.11:8080/fetch"
            )
        # The fixture-derived allowlist admits the same probes (the
        # positive path of the E2E run).
        policy = ScopePolicy(target_allowlist=halctf_target_allowlist(_fixture_platform_env()))
        policy.check("curl -sS --max-time 5 http://10.0.0.11:8080/fetch")

    # Challenge-id-only env: the fallback target is the id itself, not
    # a URL — the exact gap the fixture's real service pairs close.
    env = {"HAL_USER_ID": "user-42", "HAL_CHALLENGE_ID": TOTTORI_CHALLENGE_ID}
    environment = HalCTFEnvironment(load_config(environ=env), environ=env)
    targets = asyncio.run(environment.discover_targets())
    assert [t.address for t in targets] == [TOTTORI_CHALLENGE_ID]
    assert [t.id for t in targets] == ["halctf-challenge-18"]
    scope = asyncio.run(environment.discover_scope())
    assert scope.urls == ()
    assert "target_allowlist" not in scope.constraints


# ---------------------------------------------------------------------------
# 3. the FULL harness (real process) scores and COMPLETEs against the fixture
# ---------------------------------------------------------------------------


def test_full_harness_completes_scored_against_fixture(tmp_path: Path) -> None:
    """``python -m ozzgraph`` under the fixture's HalCTF env COMPLETEs scored.

    The real child process runs the HAL-001..010 production composition
    against the fixture: the model (routed from ``HAL_AGENT_MODEL`` +
    ``OPENAI_BASE_URL``) proposes four bounded curl probes of the ferry
    service's observed ``/fetch`` surface (403 -> 404 -> 502 -> 200),
    the 200 body delivers the challenge flag, the supervisor-owned hook
    extracts and submits it through the REAL plain-HTTP sidecar
    (``POST /submit`` -> ``{"status": "correct", "points_awarded": 1}``),
    the accepted submission completes ``objective-halctf-flag``, and
    the run terminates COMPLETED (exit 0, ``TERMINATION: completed``)
    with ``findings.json`` rendered.

    The assertions prove it is a SCORED completion — not an
    unexhausted-complete (HAL-006: ``submission.accepted`` present,
    objective completed) and not an allowlist refusal (all four probes
    executed against the target listener).
    """
    ferry = HalctfTargetServer(service="ferry")
    underworld = HalctfTargetServer(service="underworld")
    model = ScriptedModelServer(
        [
            f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {ferry.base_url}/fetch",
            f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {ferry.base_url}/fetch/missing",
            f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {ferry.base_url}/fetch/down",
            f"ACTION: run\nPAYLOAD: curl -sS --max-time 5 {ferry.base_url}/fetch/ok",
        ]
    )
    sidecar = ContractSidecarServer()
    with ferry, underworld, model, sidecar:
        state_dir = tmp_path / "state"
        env = _child_env(
            state_dir,
            tottori_env(
                ferry_url=ferry.base_url,
                underworld_url=underworld.base_url,
                model_base_url=model.base_url,
                sidecar_base_url=sidecar.base_url,
                mcp_endpoint=f"{sidecar.base_url}/mcp",
            ),
        )
        result = _run_ozzgraph(env=env, cwd=tmp_path, timeout=120)

    # Process outcome: COMPLETED -> exit 0, the human-readable
    # termination line last (AGENTS.md rule 9).
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "USER ID: user-42"
    assert result.stdout.splitlines()[-1] == "TERMINATION: completed"

    # The scripted model script was consumed exactly — the run never
    # loops beyond the four scripted probes.
    assert model.calls == 4

    # Every probe executed against the real target listener: no
    # allowlist refusal, no re-proposed duplicate, no extra traffic.
    assert ferry.requests == [
        ("GET", "/fetch"),
        ("GET", "/fetch/missing"),
        ("GET", "/fetch/down"),
        ("GET", "/fetch/ok"),
    ]
    assert underworld.requests == []

    # Scored through the REAL sidecar wire: exactly one accepted
    # submission of the fixture flag, and the best-effort /done fired.
    assert sidecar.submits == [{"challenge_id": TOTTORI_CHALLENGE_ID, "flag": TOTTORI_FLAG}]
    assert len(sidecar.dones) == 1
    assert sidecar.dones[0]["reason"] == "completed"

    # The graph holds the real-URL targets and the completed objective.
    entities = _graph_entity_types(state_dir)
    assert set(entities.keys()) >= {
        "run",
        "scope",
        "target",
        "objective",
        "action",
        "observation",
        "evidence",
        "hypothesis",
        "finding",
    }
    targets = entities["target"]
    assert sorted(t.data["address"] for t in targets) == sorted(
        [ferry.base_url, underworld.base_url]
    )
    assert all(t.data["address"] != TOTTORI_CHALLENGE_ID for t in targets)
    objectives = entities["objective"]
    assert [o.id for o in objectives] == ["objective-halctf-flag"]
    assert objectives[0].data["completed"] is True
    assert objectives[0].data["completed_at"] is not None

    # findings.json is the operator-facing render of the authoritative
    # graph entity (the validated hypothesis).
    findings_path = state_dir / "findings.json"
    assert findings_path.is_file()
    rendered = json.loads(findings_path.read_text(encoding="utf-8"))
    assert len(rendered) == 1
    assert rendered[0]["id"] == "finding-" + rendered[0]["hypothesis_id"]

    # The run log records the accepted submission and the terminal
    # event with the structured reason — never collapsed.
    events = _event_types(state_dir)
    event_types = [event["event_type"] for event in events]
    assert "submission.accepted" in event_types
    assert "submission.rejected" not in event_types
    assert event_types[-1] == "termination"
    assert events[-1]["payload"] == {"reason": "completed"}

    # The V08 report bundle rendered for the scored COMPLETED run.
    assert (state_dir / "report.json").is_file()
    report = json.loads((state_dir / "report.json").read_text(encoding="utf-8"))
    assert report["termination"]["status"] == "completed"
    assert report["counts"]["finding"] == 1
