"""V09 halctf-adapter tests (docs/CHANGES_v2.md milestone 9, docs/adr/0011).

Covers the milestone's five slices:

1. Discovery — deterministic HalCTF runtime discovery from ``HAL_*`` /
   ``MCP_ENDPOINT`` / ``OPENAI_BASE_URL`` variables in config, with
   HalCTF mode failing loudly (``ConfigError``) when the MCP endpoint is
   missing. The local default is unchanged (no HalCTF runtime variable
   means a local assessment; ``HAL_USER_ID`` alone never selects HalCTF
   mode).
2. Official tool set — ``hal_client`` exposes ``list_ctfs``,
   ``challenges``, ``status``, ``submit_flag``, ``request_hint``,
   ``scoreboard`` (``OFFICIAL_HALCTF_TOOLS``), wiring ``ctf.list`` /
   ``challenge.list`` over the JSON-RPC 2.0 wire.
3. Smoke flag + scoring + hint costs — challenge status carries the
   smoke-flag signal and the deterministic scoring breakdown; bootstrap
   records them; hint results carry the platform's per-hint cost.
4. Graceful completion — a HalCTF run whose flag is submitted and
   accepted terminates COMPLETED through the generic DONE path and
   renders the V08 report bundle.
5. Kernel decoupling — no module outside ``ozzgraph.environments``
   imports the moved hint/submission/flag/scoreboard modules.
6. HAL-001 real-runtime target snapshot — the platform-injected
   ``HAL_TARGET_*`` service pairs (Tottori's exact env shape) parse
   into one real-URL / bare-IP Target per service with service +
   challenge metadata, the scope carries hosts/urls + the merged
   ``target_allowlist`` constraint, sidecar/model/MCP infrastructure
   is excluded, the single ``HAL_TARGET_IP``/``HAL_TARGET_PORT``
   variant works, and challenge-id-only environments keep the V09
   fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp_fake import rpc_result

import ozzgraph
from ozzgraph.artifacts import ArtifactStore
from ozzgraph.bootstrap import BootstrapRunner
from ozzgraph.budgets import Budgets
from ozzgraph.config import (
    HALCTF_ENDPOINT_CANDIDATES,
    ConfigError,
    OzzGraphConfig,
    build_halctf_runtime_snapshot,
    discover_halctf_challenge_id,
    discover_halctf_endpoint,
    discover_halctf_services,
    halctf_infra_authorities,
    halctf_mode_selected,
    halctf_target_allowlist,
    load_config,
)
from ozzgraph.environments import HalCTFEnvironment
from ozzgraph.events import BOOTSTRAP_CHALLENGE_STATUS, SCOREBOARD_RETRIEVED, EventLog
from ozzgraph.hal_client import (
    OFFICIAL_HALCTF_TOOLS,
    Challenge,
    ChallengeList,
    ChallengeStatus,
    Ctf,
    CtfList,
    HalClient,
    HintResult,
    Scoring,
)
from ozzgraph.policy import ScopePolicy
from ozzgraph.runner import AutonomousRunner, RunnerStatus
from ozzgraph.state_graph import StateGraph
from ozzgraph.toolplane import ToolInventory

ENDPOINT = "http://127.0.0.1:9000/mcp"


def _config(tmp_path: Path, **overrides) -> OzzGraphConfig:
    base: dict[str, object] = {
        "hal_user_id": "user-42",
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
        "target_allowlist": ("127.0.0.1",),
    }
    base.update(overrides)
    return OzzGraphConfig(**base)  # type: ignore[arg-type] - test helper


def _halctf_env(**overrides) -> dict[str, str]:
    env = {"OZZGRAPH_CHALLENGE_ID": "web-01", "OZZGRAPH_MCP_BASE_URL": ENDPOINT}
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# 1. deterministic discovery
# ---------------------------------------------------------------------------


def test_discovery_hal_user_id_never_selects_halctf_mode() -> None:
    """HAL_USER_ID is identity (required for every run) — it must not
    select HalCTF mode, so the local default is unchanged."""
    assert halctf_mode_selected({"HAL_USER_ID": "user-42"}) is False
    assert halctf_mode_selected({}) is False


def test_discovery_mode_selected_by_hal_runtime_vars() -> None:
    for var in (
        "HAL_CTF_ID",
        "HAL_CHALLENGE_ID",
        "HAL_ENDPOINT",
        "HAL_MCP_ENDPOINT",
        "MCP_ENDPOINT",
    ):
        assert halctf_mode_selected({var: "x"}) is True, var
    assert halctf_mode_selected({"OZZGRAPH_CHALLENGE_ID": "web-01"}) is True
    # Blank values never select the mode.
    assert halctf_mode_selected({"HAL_CTF_ID": "  "}) is False


def test_discovery_challenge_id_and_endpoint_priority() -> None:
    env = {
        "HAL_USER_ID": "user-42",
        "HAL_CTF_ID": "ctf-id",
        "HAL_CHALLENGE_ID": "challenge-id",
        "OZZGRAPH_CHALLENGE_ID": "legacy-id",
        "OZZGRAPH_MCP_BASE_URL": "http://first:9000/mcp",
        "HAL_MCP_ENDPOINT": "http://second:9000/mcp",
        "HAL_ENDPOINT": "http://third:9000/mcp",
        "MCP_ENDPOINT": "http://fourth:9000/mcp",
        "OPENAI_BASE_URL": "http://fifth:8000/v1",
    }
    assert discover_halctf_challenge_id(env) == "ctf-id"  # first non-blank wins
    assert discover_halctf_endpoint(env) == "http://first:9000/mcp"
    # The candidate list is exactly the documented order.
    assert HALCTF_ENDPOINT_CANDIDATES == (
        "OZZGRAPH_MCP_BASE_URL",
        "HAL_MCP_ENDPOINT",
        "HAL_ENDPOINT",
        "MCP_ENDPOINT",
        "OPENAI_BASE_URL",
    )


def test_discovery_openai_base_url_resolves_endpoint_only() -> None:
    """OPENAI_BASE_URL can carry the endpoint once another variable
    selected HalCTF mode, but never selects the mode itself."""
    env = {"HAL_CTF_ID": "web-01", "OPENAI_BASE_URL": "http://platform:9000/mcp"}
    assert halctf_mode_selected(env) is True
    assert discover_halctf_endpoint(env) == "http://platform:9000/mcp"
    assert halctf_mode_selected({"OPENAI_BASE_URL": "http://x:8000/v1"}) is False


def test_load_config_fails_loudly_when_halctf_mode_without_endpoint() -> None:
    """V09: HalCTF mode selected but the endpoint is missing -> ConfigError
    at load time (fail loudly, AGENTS.md rule #9)."""
    with pytest.raises(ConfigError, match="endpoint"):
        load_config(environ={"HAL_USER_ID": "user-42", "HAL_CTF_ID": "web-01"})
    # With an endpoint the same configuration loads.
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            "HAL_CTF_ID": "web-01",
            "HAL_ENDPOINT": ENDPOINT,
        }
    )
    assert config.hal_user_id == "user-42"


def test_load_config_local_mode_needs_no_endpoint() -> None:
    """The local default is unchanged: no HalCTF runtime variable means no
    endpoint is required."""
    config = load_config(environ={"HAL_USER_ID": "user-42"})
    assert config.hal_user_id == "user-42"


def test_environment_fails_loudly_without_endpoint() -> None:
    with pytest.raises(ConfigError, match="endpoint"):
        HalCTFEnvironment(_config(Path("/tmp/x")), environ={"HAL_CTF_ID": "web-01"})


# ---------------------------------------------------------------------------
# 2. official tool set
# ---------------------------------------------------------------------------


def test_official_tool_set_exposed() -> None:
    """The six official HalCTF tools map to live client methods."""
    assert OFFICIAL_HALCTF_TOOLS == {
        "list_ctfs": "list_ctfs",
        "challenges": "list_challenges",
        "status": "get_status",
        "submit_flag": "submit_flag",
        "request_hint": "request_hint",
        "scoreboard": "get_scoreboard",
    }
    for method in OFFICIAL_HALCTF_TOOLS.values():
        assert callable(getattr(HalClient, method))


def test_list_ctfs_wires_ctf_list(run_mcp) -> None:
    """list_ctfs -> ctf.list, normalized into CtfList."""

    def handler(request):
        assert request["method"] == "ctf.list"
        assert request["params"] == {}
        return rpc_result(
            request,
            {
                "ctfs": [
                    {"id": "ctf-1", "name": "First CTF", "challenge_count": 3, "solved": 1},
                ]
            },
        )

    async def scenario(server) -> None:
        async with HalClient(base_url=server.base_url) as client:
            result = await client.list_ctfs()
        assert isinstance(result, CtfList)
        assert len(result.ctfs) == 1
        assert isinstance(result.ctfs[0], Ctf)
        assert result.ctfs[0].id == "ctf-1"
        assert result.ctfs[0].name == "First CTF"
        assert result.ctfs[0].challenge_count == 3

    run_mcp(handler, scenario)


def test_list_challenges_wires_challenge_list(run_mcp) -> None:
    """challenges -> challenge.list (with the optional ctf_id filter)."""

    def handler(request):
        assert request["method"] == "challenge.list"
        assert request["params"] == {"ctf_id": "ctf-1"}
        return rpc_result(
            request,
            {
                "challenges": [
                    {
                        "id": "web-01",
                        "title": "Baby Web",
                        "description": "Find the flag in the source.",
                        "category": "web",
                        "points": 100,
                    },
                ]
            },
        )

    async def scenario(server) -> None:
        async with HalClient(base_url=server.base_url) as client:
            result = await client.list_challenges("ctf-1")
        assert isinstance(result, ChallengeList)
        assert len(result.challenges) == 1
        assert isinstance(result.challenges[0], Challenge)
        assert result.challenges[0].id == "web-01"

    run_mcp(handler, scenario)


def test_list_challenges_without_ctf_id_sends_empty_params(run_mcp) -> None:
    def handler(request):
        assert request["method"] == "challenge.list"
        assert request["params"] == {}
        return rpc_result(request, {"challenges": []})

    async def scenario(server) -> None:
        async with HalClient(base_url=server.base_url) as client:
            result = await client.list_challenges()
        assert result.challenges == []

    run_mcp(handler, scenario)


# ---------------------------------------------------------------------------
# 3. smoke flag + scoring + hint costs
# ---------------------------------------------------------------------------


def test_status_carries_smoke_flag_and_scoring() -> None:
    """Challenge status includes the smoke-flag signal and the scoring
    breakdown (V09)."""
    status = ChallengeStatus(
        challenge_id="web-01",
        solved=False,
        attempts=1,
        hints_used=1,
        points_earned=0,
        smoke_flag=True,
        scoring=Scoring(max_points=100, solves=7, hint_penalty=25),
        updated_at="2026-08-08T00:00:00Z",
    )
    assert status.smoke_flag is True
    assert status.scoring is not None
    assert status.scoring.max_points == 100
    assert status.scoring.solves == 7
    assert status.scoring.hint_penalty == 25
    # Backward compatible: a status without the new fields parses.
    bare = ChallengeStatus(
        challenge_id="web-01",
        solved=False,
        attempts=0,
        hints_used=0,
        points_earned=0,
        updated_at="2026-08-08T00:00:00Z",
    )
    assert bare.smoke_flag is False
    assert bare.scoring is None


def test_hint_result_carries_cost() -> None:
    hint = HintResult(challenge_id="web-01", index=1, hint="look harder", paid=True, cost=25)
    assert hint.cost == 25
    bare = HintResult(challenge_id="web-01", index=0, hint="free", paid=False)
    assert bare.cost is None


def test_bootstrap_status_records_smoke_flag_and_scoring(tmp_path, run_mcp) -> None:
    """The bootstrap challenge-status event carries smoke flag + scoring."""

    def handler(request):
        assert request["method"] == "challenge.status"
        return rpc_result(
            request,
            {
                "challenge_id": "web-01",
                "solved": False,
                "attempts": 2,
                "hints_used": 1,
                "points_earned": 0,
                "smoke_flag": True,
                "scoring": {"max_points": 100, "solves": 3, "hint_penalty": 25},
                "updated_at": "2026-08-08T00:00:00Z",
            },
        )

    async def scenario(server) -> None:
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        event_log = EventLog.for_run(state)
        config = _config(tmp_path, target_allowlist=())
        async with HalClient(base_url=server.base_url, privileged=True) as client:
            runner = BootstrapRunner(
                config=config,
                run_id="run-1",
                event_log=event_log,
                client=client,
                environ={"OZZGRAPH_CHALLENGE_ID": "web-01"},
            )
            await runner.run()
        records = [json.loads(line) for line in (state / "actions.jsonl").read_text().splitlines()]
        status_events = [r for r in records if r["event_type"] == BOOTSTRAP_CHALLENGE_STATUS]
        assert len(status_events) == 1
        payload = status_events[0]["payload"]
        assert payload["smoke_flag"] is True
        assert payload["scoring"]["max_points"] == 100
        assert payload["scoring"]["hint_penalty"] == 25

    run_mcp(handler, scenario)


def test_scoreboard_coordinator_records_retrieval(tmp_path, run_mcp) -> None:
    """The environment's scoreboard service records a bounded run event."""

    def handler(request):
        assert request["method"] == "scoreboard.get"
        return rpc_result(
            request,
            {
                "entries": [
                    {"rank": 1, "user_id": "alice", "points": 900, "solved": 9},
                ]
            },
        )

    async def scenario(server) -> None:

        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        event_log = EventLog.for_run(state)
        environment = HalCTFEnvironment(_config(tmp_path), environ=_halctf_env())
        async with HalClient(base_url=server.base_url) as client:
            coordinator = environment.scoreboard_coordinator(
                client=client, run_id="run-1", event_log=event_log
            )
            board = await coordinator.refresh()
        assert board.entries[0].user_id == "alice"
        records = [json.loads(line) for line in (state / "actions.jsonl").read_text().splitlines()]
        retrievals = [r for r in records if r["event_type"] == SCOREBOARD_RETRIEVED]
        assert len(retrievals) == 1
        assert retrievals[0]["payload"]["entries"] == 1
        assert retrievals[0]["payload"]["top_user"] == "alice"
        assert retrievals[0]["payload"]["top_points"] == 900

    run_mcp(handler, scenario)


def test_environment_service_factories_wire_halctf_scope() -> None:
    """The environment-provided coordinators default to the discovered
    challenge id and the config's budgets (docs/adr/0011)."""
    environment = HalCTFEnvironment(_config(Path("/tmp/x")), environ=_halctf_env())

    class _FakeClient:
        privileged = True

        async def submit_flag(self, challenge_id: str, flag: str):  # pragma: no cover
            raise AssertionError("not called")

        async def request_hint(self, challenge_id: str, index: int):  # pragma: no cover
            raise AssertionError("not called")

        async def aclose(self) -> None:
            return None

    submission = environment.submission_coordinator(client=_FakeClient(), run_id="run-1")
    assert submission._challenge_id == "web-01"  # type: ignore[attr-defined]
    assert submission._max_submissions == 3  # type: ignore[attr-defined]
    hint = environment.hint_coordinator(client=_FakeClient(), run_id="run-1")
    assert hint._challenge_id == "web-01"  # type: ignore[attr-defined]
    assert hint._policy._max_hints == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 4. graceful completion (objective satisfied -> COMPLETED + report bundle)
# ---------------------------------------------------------------------------


def _runner(
    tmp_path: Path,
    graph: StateGraph,
    *,
    environment: HalCTFEnvironment,
    budgets: Budgets | None = None,
) -> AutonomousRunner:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    log = EventLog.for_run(state)
    if budgets is None:
        budgets = Budgets(
            max_tokens=0,
            max_model_calls=5,
            max_tool_calls=5,
            max_workers=4,
            max_hints=1,
            max_runtime_s=60.0,
        )
    return AutonomousRunner(
        config=_config(tmp_path),
        graph=graph,
        event_log=log,
        artifacts=ArtifactStore(state / "artifacts"),
        budgets=budgets,
        environment=environment,
        run_id="run-1",
        model_id="test-model",
        # Hermetic: no tools, no model calls (the DONE route terminates
        # before any turn reaches the model).
        inventory=ToolInventory(paths=()),
        policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
    )


@pytest.mark.asyncio
async def test_halctf_run_completes_gracefully_with_report_bundle(tmp_path: Path) -> None:
    """A HalCTF run whose flag was submitted and accepted terminates
    COMPLETED (generic DONE path) and renders the V08 report bundle.

    The accepted submission is the deterministic completion signal
    (docs/adr/0008 + 0011): the environment seeds the objective, the
    router's ``has_accepted_submission`` predicate routes DONE, the
    runner completes the objective and renders report.md / report.json /
    report.sarif / graph.sqlite / events.jsonl.
    """
    from ozzgraph.environments.halctf import flag_candidate_id
    from ozzgraph.router import EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    async with StateGraph(state / "graph.db") as graph:
        # The verified candidate + accepted submission (the HalCTF
        # terminal signal, exactly as SubmissionCoordinator persists it).
        candidate_id = flag_candidate_id("flag{graceful-1}")
        await graph.create_entity(
            candidate_id, "flag_candidate", {"flag": "flag{graceful-1}", "verified": True}
        )
        await graph.create_entity(
            "submission-1", "submission", {"accepted": True, "challenge_id": "web-01"}
        )
        await graph.create_edge(
            "submission-1-submits-flag",
            EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE,
            "submission-1",
            candidate_id,
        )
        environment = HalCTFEnvironment(_config(tmp_path), environ=_halctf_env())
        runner = _runner(tmp_path, graph, environment=environment)
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.COMPLETED
        objective = await graph.get_entity("objective-halctf-flag")
        assert objective is not None
        assert objective.data["completed"] is True
        assert objective.data["completed_at"] is not None

    # The V08 report bundle was rendered into state_dir.
    assert (state / "report.md").is_file()
    assert (state / "report.json").is_file()
    assert (state / "report.sarif").is_file()
    assert (state / "graph.sqlite").is_file()
    assert (state / "events.jsonl").is_file()


# ---------------------------------------------------------------------------
# 5. kernel decoupling
# ---------------------------------------------------------------------------


def test_kernel_never_imports_moved_halctf_modules() -> None:
    """No module outside ozzgraph.environments imports the moved hint /
    submission / flag / scoreboard modules (docs/adr/0011). The kernel
    reaches them only through the ozzgraph.environments.halctf shim."""
    root = Path(ozzgraph.__file__).parent
    banned_fragments = (
        "from ozzgraph.hints",
        "import ozzgraph.hints",
        "from ozzgraph.submissions",
        "import ozzgraph.submissions",
        "from ozzgraph.flags",
        "import ozzgraph.flags",
        # Reaching INTO the moved modules directly (the package shim and
        # the environment's service factories are the only surfaces).
        "from ozzgraph.environments.halctf.hints",
        "from ozzgraph.environments.halctf.submissions",
        "from ozzgraph.environments.halctf.flags",
        "from ozzgraph.environments.halctf.scoreboard",
    )
    offenders: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if "environments" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for fragment in banned_fragments:
            if fragment in text:
                offenders.append((str(path.relative_to(root)), fragment))
    assert offenders == []


# ---------------------------------------------------------------------------
# 6. HAL-001: real-runtime target snapshot (HAL_TARGET_* services)
# ---------------------------------------------------------------------------

#: Tottori's exact platform-injected env shape (verified cross-repo from
#: kazuki005276ssh/halctf-team-tottori committed live-run logs): named
#: service IP/PORT pairs, challenge metadata, runtime identity,
#: flag-like variables (BONUS_FLAG + a FLAG_* variable), and the
#: sidecar model/MCP infrastructure endpoints.
TOTTORI_ENV: dict[str, str] = {
    "HAL_TARGET_FERRY_IP": "10.0.0.11",
    "HAL_TARGET_FERRY_PORT": "8080",
    "HAL_TARGET_UNDERWORLD_IP": "10.0.0.12",
    "HAL_TARGET_UNDERWORLD_PORT": "3000",
    "HAL_CHALLENGE_ID": "18",
    "HAL_CHALLENGE_NAME": "underworld",
    "HAL_CHALLENGE_CATEGORY": "web",
    "HAL_AGENT_MODEL": "gpt-4o-mini",
    "HAL_RUN_ID": "run-2026-08-09",
    "HAL_TEAM_UUID": "team-42",
    "OPENAI_BASE_URL": "http://127.0.0.1:9000/llm",
    "MCP_ENDPOINT": "http://127.0.0.1:9000/mcp",
    "BONUS_FLAG": "flag{bonus}",
    "FLAG_MAIN": "flag{underworld-main}",
}


def test_halctf_services_parsed_from_tottori_env_shape() -> None:
    """The named HAL_TARGET_<NAME>_IP/_PORT pairs parse into ordered
    services; the allowlist carries each bare IP and IP:PORT authority."""
    services = discover_halctf_services(TOTTORI_ENV)
    assert [(s.name, s.ip, s.port) for s in services] == [
        ("ferry", "10.0.0.11", 8080),
        ("underworld", "10.0.0.12", 3000),
    ]
    assert halctf_target_allowlist(TOTTORI_ENV) == (
        "10.0.0.11",
        "10.0.0.11:8080",
        "10.0.0.12",
        "10.0.0.12:3000",
    )


def test_halctf_infra_authorities_include_sidecar_model_mcp() -> None:
    """The sidecar, OPENAI_BASE_URL, and MCP_ENDPOINT authorities are
    infrastructure — a HAL_TARGET_* entry matching one is excluded."""
    infra = halctf_infra_authorities(TOTTORI_ENV)
    assert "127.0.0.1:9000" in infra  # sidecar + model + MCP all resolve here
    env = {
        **TOTTORI_ENV,
        # A service pointing at the sidecar / model service is dropped.
        "HAL_TARGET_SIDECAR_IP": "127.0.0.1",
        "HAL_TARGET_SIDECAR_PORT": "9000",
        # A service matching the OPENAI_BASE_URL authority is dropped.
        "HAL_TARGET_LLM_IP": "10.0.0.99",
        "HAL_TARGET_LLM_PORT": "8000",
        "OPENAI_BASE_URL": "http://10.0.0.99:8000/v1",
    }
    services = discover_halctf_services(env)
    assert [(s.name, s.ip, s.port) for s in services] == [
        ("ferry", "10.0.0.11", 8080),
        ("underworld", "10.0.0.12", 3000),
    ]


def test_halctf_invalid_port_fails_loudly() -> None:
    """A set-but-invalid HAL_TARGET_PORT is a loud ConfigError (fail
    loudly, AGENTS.md rule #9) — never a silent host-only guess."""
    with pytest.raises(ConfigError, match="HAL_TARGET_PORT"):
        discover_halctf_services(
            _halctf_env(HAL_TARGET_IP="10.0.0.21", HAL_TARGET_PORT="not-a-port")
        )


@pytest.mark.asyncio
async def test_halctf_targets_are_real_urls_with_services() -> None:
    """With platform-injected services the targets are REAL URLs (never
    the bare challenge id '18'), each carrying its service name and the
    challenge id in metadata."""
    environment = HalCTFEnvironment(_config(Path("/tmp/x")), environ=TOTTORI_ENV)
    targets = await environment.discover_targets()
    assert [t.address for t in targets] == [
        "http://10.0.0.11:8080",
        "http://10.0.0.12:3000",
    ]
    assert all(t.type == "url" for t in targets)
    assert all(t.address != "18" for t in targets)
    assert [t.id for t in targets] == ["halctf-service-ferry", "halctf-service-underworld"]
    assert targets[0].metadata == {"service": "ferry", "challenge_id": "18"}
    assert targets[1].metadata == {"service": "underworld", "challenge_id": "18"}


@pytest.mark.asyncio
async def test_halctf_scope_carries_service_surface_and_merged_allowlist() -> None:
    """The scope carries the service surface (bare-IP hosts, http URLs)
    and the merged target_allowlist constraint: operator-configured
    entries still merge; sidecar infra never appears."""
    environment = HalCTFEnvironment(
        _config(Path("/tmp/x"), target_allowlist=("192.168.1.5",)),
        environ=TOTTORI_ENV,
    )
    scope = await environment.discover_scope()
    assert scope.hosts == ("10.0.0.11", "10.0.0.12")
    assert scope.urls == ("http://10.0.0.11:8080", "http://10.0.0.12:3000")
    allowlist = scope.constraints["target_allowlist"]
    assert isinstance(allowlist, tuple)
    assert set(allowlist) == {
        "192.168.1.5",  # operator-configured entry still merges
        "10.0.0.11",
        "10.0.0.11:8080",
        "10.0.0.12",
        "10.0.0.12:3000",
    }
    assert "127.0.0.1:9000" not in allowlist  # sidecar infra never authorized
    assert scope.constraints["challenge_id"] == "18"
    assert scope.constraints["mode"] == "halctf"


@pytest.mark.asyncio
async def test_halctf_single_service_ip_port_variant() -> None:
    """The single-service HAL_TARGET_IP/HAL_TARGET_PORT form yields one
    'default' URL target and one host/url in the scope."""
    environment = HalCTFEnvironment(
        _config(Path("/tmp/x")),
        environ=_halctf_env(HAL_TARGET_IP="10.0.0.21", HAL_TARGET_PORT="4444"),
    )
    targets = await environment.discover_targets()
    assert len(targets) == 1
    assert targets[0].type == "url"
    assert targets[0].address == "http://10.0.0.21:4444"
    assert targets[0].metadata["service"] == "default"
    scope = await environment.discover_scope()
    assert scope.hosts == ("10.0.0.21",)
    assert scope.urls == ("http://10.0.0.21:4444",)


@pytest.mark.asyncio
async def test_halctf_host_only_service_without_port() -> None:
    """A service with an IP but no PORT stays a bare-IP host target (no
    port is ever invented)."""
    environment = HalCTFEnvironment(
        _config(Path("/tmp/x"), target_allowlist=()),
        environ=_halctf_env(HAL_TARGET_IP="10.0.0.21"),
    )
    targets = await environment.discover_targets()
    assert len(targets) == 1
    assert targets[0].type == "host"
    assert targets[0].address == "10.0.0.21"
    scope = await environment.discover_scope()
    assert scope.hosts == ("10.0.0.21",)
    assert scope.urls == ()
    assert scope.constraints["target_allowlist"] == ("10.0.0.21",)


@pytest.mark.asyncio
async def test_halctf_challenge_id_only_env_keeps_v09_fallback() -> None:
    """Without HAL_TARGET_* services the challenge-id-only behavior is
    unchanged: one URL target carrying the challenge id itself."""
    environment = HalCTFEnvironment(_config(Path("/tmp/x")), environ=_halctf_env())
    targets = await environment.discover_targets()
    assert len(targets) == 1
    assert targets[0].id == "halctf-challenge-web-01"
    assert targets[0].address == "web-01"
    scope = await environment.discover_scope()
    # Today's surface: the configured allowlist as hosts, no merged
    # target_allowlist constraint (no services to derive).
    assert scope.hosts == ("127.0.0.1",)
    assert scope.urls == ()
    assert "target_allowlist" not in scope.constraints


def test_load_config_merges_halctf_service_allowlist() -> None:
    """In HalCTF mode load_config merges the platform-injected service
    allowlist into config.target_allowlist — no manual
    OZZGRAPH_TARGET_ALLOWLIST step is needed for the policy gate."""
    config = load_config(environ={"HAL_USER_ID": "user-42", **TOTTORI_ENV})
    assert set(config.target_allowlist) == {
        "10.0.0.11",
        "10.0.0.11:8080",
        "10.0.0.12",
        "10.0.0.12:3000",
    }
    # Operator-configured entries still merge alongside the services.
    config = load_config(
        environ={
            "HAL_USER_ID": "user-42",
            **TOTTORI_ENV,
            "OZZGRAPH_TARGET_ALLOWLIST": "192.168.1.5",
        }
    )
    assert "192.168.1.5" in config.target_allowlist
    assert "10.0.0.11:8080" in config.target_allowlist


def test_halctf_runtime_snapshot_parses_tottori_env() -> None:
    """build_halctf_runtime_snapshot parses the FULL platform-injected
    runtime from TOTTORI_ENV: services, challenge metadata, runtime
    identity, flag-like env values (BONUS_FLAG + FLAG_*), and the
    model/MCP infrastructure endpoints."""
    snapshot = build_halctf_runtime_snapshot(TOTTORI_ENV)
    assert [(s.name, s.ip, s.port) for s in snapshot.services] == [
        ("ferry", "10.0.0.11", 8080),
        ("underworld", "10.0.0.12", 3000),
    ]
    assert snapshot.challenge_id == "18"
    assert snapshot.challenge_name == "underworld"
    assert snapshot.challenge_category == "web"
    assert snapshot.agent_model == "gpt-4o-mini"
    assert snapshot.run_id == "run-2026-08-09"
    assert snapshot.team_uuid == "team-42"
    # BONUS_FLAG first, then FLAG_* variables (sorted by name).
    assert snapshot.flag_like == ("flag{bonus}", "flag{underworld-main}")
    assert snapshot.openai_base_url == "http://127.0.0.1:9000/llm"
    # The resolved MCP endpoint (MCP_ENDPOINT wins over OPENAI_BASE_URL
    # in the candidate order) — the same value the environment drives.
    assert snapshot.mcp_endpoint == "http://127.0.0.1:9000/mcp"
    assert (
        snapshot.mcp_endpoint
        == HalCTFEnvironment(_config(Path("/tmp/x")), environ=TOTTORI_ENV).endpoint
    )


def test_halctf_runtime_snapshot_graceful_without_metadata() -> None:
    """An env WITHOUT the platform-injected metadata parses gracefully:
    every optional field is None and flag_like is empty — never a loud
    failure for absent metadata."""
    snapshot = build_halctf_runtime_snapshot({})
    assert snapshot.services == ()
    assert snapshot.challenge_id is None
    assert snapshot.challenge_name is None
    assert snapshot.challenge_category is None
    assert snapshot.agent_model is None
    assert snapshot.run_id is None
    assert snapshot.team_uuid is None
    assert snapshot.flag_like == ()
    assert snapshot.openai_base_url is None
    assert snapshot.mcp_endpoint is None
    # Blank metadata is treated exactly like unset.
    blank = build_halctf_runtime_snapshot(
        {
            "HAL_CHALLENGE_NAME": "   ",
            "BONUS_FLAG": " ",
            "FLAG_MAIN": "",
        }
    )
    assert blank.challenge_name is None
    assert blank.flag_like == ()


@pytest.mark.asyncio
async def test_halctf_environment_exposes_runtime_snapshot() -> None:
    """HalCTFEnvironment builds the snapshot at construction and surfaces
    the parsed metadata in the scope constraints (challenge name /
    category ride alongside challenge_id and mode)."""
    environment = HalCTFEnvironment(_config(Path("/tmp/x")), environ=TOTTORI_ENV)
    snapshot = environment.snapshot
    assert snapshot.challenge_name == "underworld"
    assert snapshot.challenge_category == "web"
    assert snapshot.agent_model == "gpt-4o-mini"
    assert snapshot.run_id == "run-2026-08-09"
    assert snapshot.team_uuid == "team-42"
    assert snapshot.flag_like == ("flag{bonus}", "flag{underworld-main}")
    scope = await environment.discover_scope()
    assert scope.constraints["challenge_id"] == "18"
    assert scope.constraints["mode"] == "halctf"
    assert scope.constraints["challenge_name"] == "underworld"
    assert scope.constraints["challenge_category"] == "web"
    # Without injected metadata the constraints carry only id + mode.
    bare = HalCTFEnvironment(_config(Path("/tmp/x")), environ=_halctf_env())
    bare_scope = await bare.discover_scope()
    assert bare_scope.constraints["challenge_id"] == "web-01"
    assert "challenge_name" not in bare_scope.constraints
    assert "challenge_category" not in bare_scope.constraints
