"""Tests for the supervisor kernel (PR2–PR4): identity, dirs, lifecycle,
heartbeat, budget exhaustion, signal handling, and structured event
logging."""

import asyncio
import json
from pathlib import Path

import pytest

from ozzgraph.config import OzzGraphConfig
from ozzgraph.events import BOOTSTRAP, TERMINATION
from ozzgraph.supervisor import Supervisor, TerminationReason


def _config(tmp_path, user_id: str = "user-42", **overrides) -> OzzGraphConfig:
    base = {
        "hal_user_id": user_id,
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
    }
    base.update(overrides)
    return OzzGraphConfig(**base)


def test_start_prints_identity_immediately(tmp_path, capsys) -> None:
    """start() prints the USER ID line before anything else."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    out = capsys.readouterr().out
    assert out.startswith("USER ID: user-42")


def test_start_initializes_runtime_directories(tmp_path) -> None:
    """start() creates state and artifact directories."""
    state = tmp_path / "state"
    artifacts = tmp_path / "state" / "artifacts"
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    assert state.is_dir()
    assert artifacts.is_dir()


def test_start_is_idempotent(tmp_path) -> None:
    """Calling start() twice must not raise."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    supervisor.start()


def test_run_exhausts_runtime_budget_without_model_dependency(tmp_path, capsys) -> None:
    """Without a model dependency, the loop runs until the time budget
    exhausts, emitting at least one heartbeat, and reports BUDGET_EXHAUSTED."""
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=2, heartbeat_interval_s=1))
    reason = asyncio.run(supervisor.run())
    assert reason == TerminationReason.BUDGET_EXHAUSTED
    out = capsys.readouterr().out
    assert out.startswith("USER ID: user-42")
    assert "HEARTBEAT" in out


def test_stop_accepts_structured_reason(tmp_path) -> None:
    """stop() accepts a structured termination reason without raising."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    supervisor.stop(reason=TerminationReason.INTERRUPTED)


def test_budgets_accessor_raises_before_run(tmp_path) -> None:
    """budgets() is unavailable until run() starts."""
    supervisor = Supervisor(_config(tmp_path))
    with pytest.raises(RuntimeError):
        supervisor.budgets()


def test_run_exposes_budgets(tmp_path) -> None:
    """run() installs a budget tracker queryable via budgets()."""
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=1))
    asyncio.run(supervisor.run())
    assert supervisor.budgets().is_runtime_exhausted()


def test_run_raises_config_error_when_dirs_unwritable(tmp_path) -> None:
    """A failure to initialize runtime directories fails loudly."""
    # Point artifact_dir inside a regular file so mkdir raises OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad = OzzGraphConfig(
        hal_user_id="user-42",
        state_dir=tmp_path / "state",
        artifact_dir=blocker / "artifacts",
    )
    with pytest.raises(OSError):
        Supervisor(bad).start()


def _read_records(tmp_path: Path) -> list[dict[str, object]]:
    """Parse every line of the supervisor's run log."""
    path = tmp_path / "state" / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_writes_bootstrap_and_termination_events(tmp_path) -> None:
    """run() records a bootstrap event and ends with a termination event."""
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=1))
    asyncio.run(supervisor.run())
    records = _read_records(tmp_path)
    bootstraps = [r for r in records if r["event_type"] == BOOTSTRAP]
    assert len(bootstraps) == 1
    assert bootstraps[0]["producer"] == "supervisor"
    assert bootstraps[0]["run_id"] == supervisor.run_id
    payload = bootstraps[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["hal_user_id"] == "user-42"
    assert "state_dir" in payload
    assert "artifact_dir" in payload
    assert isinstance(payload["budget"], dict)
    assert records[-1]["event_type"] == TERMINATION
    assert records[-1]["payload"] == {"reason": "budget_exhausted"}


def test_run_creates_the_state_graph_database(tmp_path) -> None:
    """run() opens the authoritative SQLite graph at state_dir/graph.db
    and the runner seeds it (V01: the runner drives the graph)."""
    supervisor = Supervisor(_config(tmp_path, max_runtime_s=1))
    asyncio.run(supervisor.run())
    assert (tmp_path / "state" / "graph.db").is_file()


def test_run_id_fixed_at_construction_and_read_only(tmp_path) -> None:
    """run_id is minted once per supervisor and cannot be reassigned."""
    supervisor = Supervisor(_config(tmp_path))
    run_id = supervisor.run_id
    assert supervisor.run_id == run_id
    assert run_id != Supervisor(_config(tmp_path)).run_id
    with pytest.raises(AttributeError):
        # Plain assignment would be a static type error, so probe via setattr.
        setattr(supervisor, "run_id", "other")  # noqa: B010 - deliberate read-only check


def test_stop_writes_termination_event_with_reason(tmp_path) -> None:
    """stop(reason) records a termination event carrying the reason."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    supervisor.stop(reason=TerminationReason.BUDGET_EXHAUSTED)
    records = _read_records(tmp_path)
    assert [r["event_type"] for r in records] == [BOOTSTRAP, TERMINATION]
    assert records[1]["payload"] == {"reason": "budget_exhausted"}
    assert records[1]["run_id"] == supervisor.run_id


def test_stop_before_start_writes_no_event(tmp_path) -> None:
    """stop() before start() is a no-op that writes no event log."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.stop(reason=TerminationReason.INTERRUPTED)
    assert not (tmp_path / "state" / "actions.jsonl").exists()


# ---------------------------------------------------------------------------
# PR22: supervisor-only flag submission
# ---------------------------------------------------------------------------


class _PrivilegedSubmitFake:
    """Minimal privileged submit surface (structurally satisfies the protocol)."""

    def __init__(self, *, accepted: bool = True) -> None:
        self._accepted = accepted
        self.calls: list[tuple[str, str]] = []

    @property
    def privileged(self) -> bool:
        return True

    async def submit_flag(self, challenge_id: str, flag: str):
        self.calls.append((challenge_id, flag))
        from ozzgraph.hal_client import SubmissionResult

        return SubmissionResult(
            challenge_id=challenge_id,
            accepted=self._accepted,
            message="ok" if self._accepted else "wrong",
            points=100 if self._accepted else 0,
        )


async def _seed_verified_candidate(graph) -> str:
    """Seed observation + evidence + verified candidate (flag-<hash>)."""
    from ozzgraph.environments.halctf import (
        EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
        FIELD_FLAG,
        FIELD_REJECTED,
        FIELD_VERIFIED,
        flag_candidate_id,
    )

    await graph.create_entity("obs-1", "observation", {"summary": "saw flag{supervisor-1}"})
    await graph.create_entity("ev-1", "evidence", {"note": "parsed"})
    await graph.create_edge(
        "ev-1-from-obs-1",
        EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
        "ev-1",
        "obs-1",
    )
    candidate_id = flag_candidate_id("flag{supervisor-1}")
    await graph.create_entity(
        candidate_id,
        "flag_candidate",
        {
            FIELD_FLAG: "flag{supervisor-1}",
            FIELD_VERIFIED: True,
            "source_observation_id": "obs-1",
            "evidence_ids": ["ev-1"],
            FIELD_REJECTED: False,
            "attempts": 0,
        },
    )
    await graph.create_edge(
        f"{candidate_id}-observed-in-ev-1",
        "FLAG_CANDIDATE OBSERVED_IN EVIDENCE",
        candidate_id,
        "ev-1",
    )
    return candidate_id


@pytest.mark.asyncio
async def test_submit_verified_candidate_drives_privileged_coordinator(tmp_path) -> None:
    """The supervisor submits through the coordinator and persists the outcome."""
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    client = _PrivilegedSubmitFake(accepted=True)

    async with StateGraph(":memory:") as graph:
        candidate_id = await _seed_verified_candidate(graph)
        result = await supervisor.submit_verified_candidate(
            graph, challenge_id="web-01", client=client
        )

        assert result.accepted is True
        assert client.calls == [("web-01", "flag{supervisor-1}")]
        submission = await graph.get_entity("submission-1")
        assert submission is not None
        assert submission.data["accepted"] is True
        assert submission.data["candidate_id"] == candidate_id

    records = _read_records(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "submission.attempted" in event_types
    assert "submission.accepted" in event_types


@pytest.mark.asyncio
async def test_submit_verified_candidate_routes_done(tmp_path) -> None:
    """After a supervisor submission, the phase router routes DONE."""
    from ozzgraph.phases import Phase
    from ozzgraph.router import PhaseRouter
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()

    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        await supervisor.submit_verified_candidate(
            graph, challenge_id="web-01", client=_PrivilegedSubmitFake(accepted=True)
        )
        route = await PhaseRouter().route(graph)
        assert route.phase == Phase.DONE
        assert route.predicate == "has_accepted_submission"


@pytest.mark.asyncio
async def test_submit_verified_candidate_refuses_non_privileged_client(
    tmp_path,
) -> None:
    """Only the supervisor path may submit: a non-privileged client is refused."""
    from ozzgraph.environments.halctf import SubmissionPrivilegeError
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()

    class _NonPrivilegedSubmitFake(_PrivilegedSubmitFake):
        @property
        def privileged(self) -> bool:
            return False

    client = _NonPrivilegedSubmitFake()
    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        with pytest.raises(SubmissionPrivilegeError, match="supervisor-only"):
            await supervisor.submit_verified_candidate(graph, challenge_id="web-01", client=client)
        assert client.calls == []


@pytest.mark.asyncio
async def test_submit_verified_candidate_missing_challenge_id_raises(tmp_path, monkeypatch) -> None:
    """Without a challenge id, submission is refused loudly (ConfigError)."""
    from ozzgraph.config import ConfigError
    from ozzgraph.state_graph import StateGraph

    monkeypatch.delenv("OZZGRAPH_CHALLENGE_ID", raising=False)
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()

    async with StateGraph(":memory:") as graph:
        await _seed_verified_candidate(graph)
        with pytest.raises(ConfigError, match="challenge id"):
            await supervisor.submit_verified_candidate(graph, client=_PrivilegedSubmitFake())


# ---------------------------------------------------------------------------
# PR23: supervisor-only paid hints (docs/TESTING_AND_QA.md scenario 8)
# ---------------------------------------------------------------------------


class _PrivilegedHintFake:
    """Minimal privileged hint surface (structurally satisfies the protocol)."""

    def __init__(self, *, privileged: bool = True) -> None:
        self._privileged = privileged
        self.calls: list[tuple[str, int]] = []

    @property
    def privileged(self) -> bool:
        return self._privileged

    async def request_hint(self, challenge_id: str, index: int):
        from ozzgraph.hal_client import HintResult

        self.calls.append((challenge_id, index))
        return HintResult(
            challenge_id=challenge_id, index=index, hint="try sqlmap --level 2", paid=True
        )

    async def aclose(self) -> None:
        """No-op: the fake owns no connection (protocol conformance)."""


async def _seed_hint_ready_graph(graph, *, recommendations: int = 2) -> None:
    """Minimal graph satisfying every paid-hint gate rule (or fewer recs).

    A two-step plan with six bound attempts (both steps attempted,
    gain = 1.0), two evaluations as the information-gain anchor, and
    the requested number of distinct evaluator recommendations.
    """
    from datetime import UTC, datetime

    from ozzgraph.environments.halctf import HintPolicy
    from ozzgraph.evaluator import ENTITY_EVALUATION
    from ozzgraph.executor import ENTITY_ACTION, ENTITY_PLAN, ENTITY_PLAN_STEP

    at = datetime(2026, 1, 1, tzinfo=UTC)
    plan = "plan-exp-1"
    await graph.create_entity(plan, ENTITY_PLAN, {"phase": "EXPLOITATION", "step_count": 2}, at=at)
    for n in (1, 2):
        await graph.create_entity(
            f"{plan}-step-{n}", ENTITY_PLAN_STEP, {"objective": f"objective {n}"}, at=at
        )
        for i in range(3):
            await graph.create_entity(
                f"action-{n}-{i}",
                ENTITY_ACTION,
                {"plan_id": plan, "plan_step_id": f"{plan}-step-{n}"},
                at=at,
            )
    for seq in (1, 2):
        await graph.create_entity(
            f"eval-{plan}-{seq}",
            ENTITY_EVALUATION,
            {
                "plan_id": plan,
                "step_outcomes": [
                    {"step_id": f"{plan}-step-1", "outcome": "pending"},
                    {"step_id": f"{plan}-step-2", "outcome": "pending"},
                ],
            },
            at=at,
        )
    policy = HintPolicy()
    for seq in range(1, recommendations + 1):
        await policy.record_evaluator_recommendation(graph, f"eval-{plan}-{seq}")


@pytest.mark.asyncio
async def test_request_paid_hint_drives_privileged_coordinator(tmp_path) -> None:
    """The supervisor buys through the coordinator and persists the purchase."""
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    client = _PrivilegedHintFake()

    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        result = await supervisor.request_paid_hint(graph, 1, challenge_id="web-01", client=client)

        assert result.paid is True
        assert client.calls == [("web-01", 1)]
        purchase = await graph.get_entity("hint-purchase-1")
        assert purchase is not None
        assert purchase.data["index"] == 1
        assert purchase.data["paid"] is True

    records = _read_records(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "hint.policy_approved" in event_types
    assert "hint.purchase_attempted" in event_types
    assert "hint.purchase_succeeded" in event_types


@pytest.mark.asyncio
async def test_request_paid_hint_blocked_end_to_end(tmp_path) -> None:
    """TESTING_AND_QA scenario 8: a paid hint the gate denies never reaches the wire."""
    from ozzgraph.environments.halctf import HintPolicyDeniedError
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    client = _PrivilegedHintFake()

    async with StateGraph(":memory:") as graph:
        # A graph the evaluator never recommended a hint for: the gate
        # denies (fewer than two recommendations) before any wire call.
        await _seed_hint_ready_graph(graph, recommendations=1)
        with pytest.raises(HintPolicyDeniedError, match="recommendations"):
            await supervisor.request_paid_hint(graph, 1, challenge_id="web-01", client=client)

        assert client.calls == []
        assert await graph.list_entities("hint_purchase") == []

    records = _read_records(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "hint.policy_denied" in event_types
    assert "hint.purchase_attempted" not in event_types


@pytest.mark.asyncio
async def test_request_paid_hint_refuses_non_privileged_client(tmp_path) -> None:
    """Only the supervisor path may buy paid hints: non-privileged is refused."""
    from ozzgraph.environments.halctf import HintPrivilegeError
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    client = _PrivilegedHintFake(privileged=False)

    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        with pytest.raises(HintPrivilegeError, match="supervisor-only"):
            await supervisor.request_paid_hint(graph, 1, challenge_id="web-01", client=client)
        assert client.calls == []


@pytest.mark.asyncio
async def test_request_paid_hint_requires_index_at_least_one(tmp_path) -> None:
    """The paid-hint surface never requests hint zero (bootstrap owns it)."""
    from ozzgraph.state_graph import StateGraph

    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()

    async with StateGraph(":memory:") as graph:
        with pytest.raises(ValueError, match=">= 1"):
            await supervisor.request_paid_hint(
                graph, 0, challenge_id="web-01", client=_PrivilegedHintFake()
            )


@pytest.mark.asyncio
async def test_request_paid_hint_missing_challenge_id_raises(tmp_path, monkeypatch) -> None:
    """Without a challenge id, a hint purchase is refused loudly (ConfigError)."""
    from ozzgraph.config import ConfigError
    from ozzgraph.state_graph import StateGraph

    monkeypatch.delenv("OZZGRAPH_CHALLENGE_ID", raising=False)
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()

    async with StateGraph(":memory:") as graph:
        await _seed_hint_ready_graph(graph)
        with pytest.raises(ConfigError, match="challenge id"):
            await supervisor.request_paid_hint(graph, 1, client=_PrivilegedHintFake())


# ---------------------------------------------------------------------------
# V01: environment selection + local scope print + runner status mapping
# ---------------------------------------------------------------------------


def test_make_environment_defaults_to_local(tmp_path, monkeypatch) -> None:
    """Without OZZGRAPH_CHALLENGE_ID the run is a local assessment."""
    from ozzgraph.environments import LocalEnvironment

    monkeypatch.delenv("OZZGRAPH_CHALLENGE_ID", raising=False)
    supervisor = Supervisor(_config(tmp_path))
    environment = supervisor._make_environment()
    assert isinstance(environment, LocalEnvironment)


def test_make_environment_uses_halctf_when_challenge_id_set(tmp_path, monkeypatch) -> None:
    """With a HalCTF runtime variable the HalCTF environment is selected."""
    from ozzgraph.environments import HalCTFEnvironment

    monkeypatch.setenv("OZZGRAPH_CHALLENGE_ID", "web-01")
    monkeypatch.setenv("OZZGRAPH_MCP_BASE_URL", "http://127.0.0.1:9000/mcp")
    supervisor = Supervisor(_config(tmp_path))
    environment = supervisor._make_environment()
    assert isinstance(environment, HalCTFEnvironment)
    assert environment.endpoint == "http://127.0.0.1:9000/mcp"


def test_make_environment_uses_halctf_from_hal_vars(tmp_path, monkeypatch) -> None:
    """V09: HAL_* runtime variables select HalCTF mode (HAL_USER_ID alone
    never does — it is identity, required for every run)."""
    from ozzgraph.environments import HalCTFEnvironment, LocalEnvironment

    monkeypatch.delenv("OZZGRAPH_CHALLENGE_ID", raising=False)
    for var in (
        "OZZGRAPH_MCP_BASE_URL",
        "HAL_MCP_ENDPOINT",
        "HAL_ENDPOINT",
        "MCP_ENDPOINT",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    supervisor = Supervisor(_config(tmp_path))
    # HAL_USER_ID alone: local assessment (the default experience).
    monkeypatch.setenv("HAL_USER_ID", "user-42")
    assert isinstance(supervisor._make_environment(), LocalEnvironment)
    # A HAL_* runtime variable selects HalCTF mode; the endpoint is
    # discovered from HAL_ENDPOINT.
    monkeypatch.setenv("HAL_CTF_ID", "web-01")
    monkeypatch.setenv("HAL_ENDPOINT", "http://halctf:9000/mcp")
    environment = supervisor._make_environment()
    assert isinstance(environment, HalCTFEnvironment)
    assert environment.endpoint == "http://halctf:9000/mcp"


def test_make_environment_halctf_without_endpoint(tmp_path, monkeypatch) -> None:
    """HAL-002: HalCTF mode without a discoverable MCP endpoint is NOT
    an error — an env-only detonation builds the HalCTF environment
    with endpoint None (MCP is optional enrichment/fallback)."""
    from ozzgraph.environments import HalCTFEnvironment

    monkeypatch.delenv("OZZGRAPH_CHALLENGE_ID", raising=False)
    monkeypatch.setenv("HAL_CTF_ID", "web-01")
    for var in (
        "OZZGRAPH_MCP_BASE_URL",
        "HAL_MCP_ENDPOINT",
        "HAL_ENDPOINT",
        "MCP_ENDPOINT",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    supervisor = Supervisor(_config(tmp_path))
    environment = supervisor._make_environment()
    assert isinstance(environment, HalCTFEnvironment)
    assert environment.endpoint is None


@pytest.mark.asyncio
async def test_print_local_scope_prints_scope_and_objectives(tmp_path, capsys) -> None:
    """The local-mode bootstrap summary prints scope, targets, and
    objectives; HalCTF mode prints nothing."""
    from ozzgraph.environments import LocalEnvironment

    supervisor = Supervisor(_config(tmp_path, target_allowlist=("127.0.0.1",)))
    await supervisor._print_local_scope(LocalEnvironment(supervisor.config))
    out = capsys.readouterr().out
    assert "SCOPE: local" in out
    assert "TARGETS:" in out
    assert "OBJECTIVES:" in out
    assert "objective-local-1" in out
    assert "CAPABILITIES:" in out

    from ozzgraph.environments import HalCTFEnvironment

    await supervisor._print_local_scope(
        HalCTFEnvironment(
            supervisor.config,
            environ={
                "OZZGRAPH_CHALLENGE_ID": "web-01",
                "OZZGRAPH_MCP_BASE_URL": "http://127.0.0.1:9000/mcp",
            },
        )
    )
    assert capsys.readouterr().out == ""


def test_map_status_matches_termination_reasons() -> None:
    """Every runner status maps to the matching termination reason."""
    from ozzgraph.runner import RunnerStatus

    assert Supervisor._map_status(RunnerStatus.COMPLETED) == TerminationReason.COMPLETED
    assert Supervisor._map_status(RunnerStatus.STOPPED) == TerminationReason.INTERRUPTED
    assert Supervisor._map_status(RunnerStatus.FAILED) == TerminationReason.FAILED
    assert (
        Supervisor._map_status(RunnerStatus.BUDGET_EXHAUSTED) == TerminationReason.BUDGET_EXHAUSTED
    )
