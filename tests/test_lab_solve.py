"""Integration solve tests for the synthetic lab (PR27).

Drives the REAL harness code paths against a live synthetic HTTP
target: deterministic bootstrap probes (policy-gated ``curl`` through
the bounded shell runner), the executor loop (one bounded action per
turn through the scope policy), observation parsing, provenance-backed
flag extraction, and supervisor-only submission — ending in the phase
router's DONE state, exactly as docs/TESTING_AND_QA.md scenario 1
("Smoke-test flag discovered and submitted") intends.

The OZ{...} lab flags require ``flag_pattern`` to match them
(docs/SYNTHETIC_LAB.md), mirroring how an operator would point the
harness at the lab with ``OZZGRAPH_FLAG_PATTERN``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.bootstrap import BootstrapRunner
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.events import BOOTSTRAP_REACHABILITY, EventLog
from ozzgraph.executor import Executor
from ozzgraph.flags import (
    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
    FlagCandidateExtractor,
)
from ozzgraph.hal_client import HalClient, SubmissionResult
from ozzgraph.lab import get_target
from ozzgraph.observations import ShellTextParser
from ozzgraph.phases import Phase
from ozzgraph.policy import ScopePolicy
from ozzgraph.router import PhaseRouter
from ozzgraph.shell import ShellRunner
from ozzgraph.state_graph import StateGraph
from ozzgraph.supervisor import Supervisor, TerminationReason

#: The lab's flag envelope; mirrors docs/SYNTHETIC_LAB.md.
LAB_FLAG_PATTERN = r"OZ\{[^{}\s]+\}"


class _PrivilegedSubmitFake:
    """Minimal privileged submit surface (structurally satisfies the
    SubmissionClient protocol), mirroring tests/test_supervisor.py's fake."""

    def __init__(self, *, accepted: bool = True) -> None:
        self._accepted = accepted
        self.calls: list[tuple[str, str]] = []

    @property
    def privileged(self) -> bool:
        return True

    async def submit_flag(self, challenge_id: str, flag: str) -> SubmissionResult:
        self.calls.append((challenge_id, flag))
        return SubmissionResult(
            challenge_id=challenge_id,
            accepted=self._accepted,
            message="ok" if self._accepted else "wrong",
            points=100 if self._accepted else 0,
        )

    async def aclose(self) -> None:
        return None


def _config(tmp_path: Path) -> OzzGraphConfig:
    """Config pointed at the lab target with the lab flag pattern."""
    return OzzGraphConfig(
        hal_user_id="user-42",
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "state" / "artifacts",
        target_allowlist=("127.0.0.1",),
        flag_pattern=LAB_FLAG_PATTERN,
        max_runtime_s=120,
        heartbeat_interval_s=30,
    )


@pytest.mark.asyncio
async def test_bootstrap_probes_reach_the_lab_target(tmp_path: Path) -> None:
    """Deterministic bootstrap probes reach a live lab HTTP target.

    The probe (``curl -I`` via the bounded ShellRunner through the
    policy gate with ``127.0.0.1`` allowlisted) records a reachable
    status — the real code path against the synthetic target.
    """
    with get_target("http-recon") as target:
        url = target.target_value
        config = _config(tmp_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        event_log = EventLog.for_run(config.state_dir)
        runner = BootstrapRunner(
            config=config,
            run_id="run-1",
            event_log=event_log,
            client=HalClient(
                base_url="http://127.0.0.1:1",  # no challenge id: never used
                privileged=True,
                event_log=event_log,
                run_id="run-1",
            ),
            environ={"OZZGRAPH_TARGET": url},
        )
        await runner.run()

    records = [
        __import__("json").loads(line)
        for line in (tmp_path / "state" / "actions.jsonl").read_text().splitlines()
    ]
    reachability = [r for r in records if r["event_type"] == BOOTSTRAP_REACHABILITY]
    assert len(reachability) == 1
    assert reachability[0]["payload"]["status"] == "reachable"
    assert reachability[0]["payload"]["target"] == url
    assert reachability[0]["payload"]["category"] == "http"


@pytest.mark.asyncio
async def test_full_solve_hidden_routes_through_harness(tmp_path: Path) -> None:
    """A full harness solve: bootstrap + executor + flag discovery + submission.

    Exercises the real code paths end to end against the lab's
    hidden-routes target: the executor approves one bounded ``curl``
    action (policy-gated, fingerprinted, loopback allowlisted), the
    shell runner fetches ``/admin``, the observation parser turns the
    output into a graph observation, the flag extractor produces a
    provenance-backed candidate, and the supervisor submits it through
    the privileged client — routing the graph to DONE.
    """
    with get_target("hidden-routes") as target:
        config = _config(tmp_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        event_log = EventLog.for_run(config.state_dir)
        artifact_store = ArtifactStore.for_run(config.state_dir)

        supervisor = Supervisor(config)
        supervisor.start()

        # bootstrap: probes the live target through the real shell runner
        bootstrap = BootstrapRunner(
            config=config,
            run_id="run-1",
            event_log=event_log,
            client=HalClient(
                base_url="http://127.0.0.1:1",  # no challenge id: never used
                privileged=True,
                event_log=event_log,
                run_id="run-1",
            ),
            environ={"OZZGRAPH_TARGET": target.target_value},
        )
        await bootstrap.run()

        async with StateGraph(":memory:") as graph:
            # seed the RECON baseline (run + unconfirmed target)
            await graph.create_entity("run-1", "run")
            await graph.create_entity("tgt-1", "target", {"confirmed": False})

            executor = Executor(
                budgets=Budgets(
                    max_tokens=1000,
                    max_model_calls=10,
                    max_tool_calls=20,
                    max_workers=2,
                    max_hints=1,
                    max_runtime_s=120.0,
                ),
                run_id="run-1",
                event_log=event_log,
                policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
            )

            # turn 1: the model proposes the /admin fetch (recon skill)
            turn = await executor.turn(
                graph,
                {
                    "action": f"curl -sS --max-time 5 {target.target_value}/admin",
                    "skill_id": "recon_http_fingerprint",
                },
            )
            assert turn.phase == Phase.RECON
            assert turn.action.fingerprint  # policy-gated, fingerprinted action

            # the tool plane runs the bounded action through ShellRunner
            result = await ShellRunner().run(
                command=turn.action.action,
                timeout_seconds=turn.action.timeout_seconds,
                stdout_limit=turn.action.output_limit,
                stderr_limit=turn.action.output_limit,
                working_directory=config.state_dir,
            )
            assert target.flag in result.stdout

            # observation parser -> graph observation (real code path)
            observation = ShellTextParser().parse(result)
            await graph.create_entity("obs-1", "observation", observation.model_dump())
            await graph.create_entity("ev-1", "evidence", {"note": "parsed"})
            await graph.create_edge(
                "ev-1-from-obs-1",
                EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
                "ev-1",
                "obs-1",
            )

            # provenance-backed flag extraction (real code path)
            extractor = FlagCandidateExtractor(
                run_id="run-1",
                event_log=event_log,
                pattern=LAB_FLAG_PATTERN,
                artifact_store=artifact_store,
            )
            candidates = await extractor.extract(graph)
            assert len(candidates) == 1
            assert candidates[0].flag == target.flag

            # supervisor-only submission (real coordinator, fake platform)
            submit_client = _PrivilegedSubmitFake(accepted=True)
            submission = await supervisor.submit_verified_candidate(
                graph, challenge_id="lab-01", client=submit_client
            )
            assert submission.accepted is True
            assert submit_client.calls == [("lab-01", target.flag)]

            # evaluator/router acceptance: the graph routes to DONE
            route = await PhaseRouter().route(graph)
            assert route.phase == Phase.DONE
            assert route.predicate == "has_accepted_submission"

        # the run terminates cleanly with a structured reason
        supervisor.stop(reason=TerminationReason.COMPLETED)

        records = [
            __import__("json").loads(line)
            for line in (tmp_path / "state" / "actions.jsonl").read_text().splitlines()
        ]
        event_types = [record["event_type"] for record in records]
        assert "executor.action_attempted" in event_types
        assert "flags.candidate_found" in event_types
        assert "submission.accepted" in event_types
        assert "termination" in event_types
        assert records[-1]["payload"] == {"reason": "completed"}


@pytest.mark.asyncio
async def test_full_solve_auth_logic_flag_after_credentials(tmp_path: Path) -> None:
    """The executor loop solves the auth target: 401 first, then the flag.

    Two turns against the auth-logic target: the unauthenticated
    probe yields no flag, and the credentialed probe produces the
    candidate that routes the graph to DONE — proving the flag is
    gated by the authentication logic, not present in the surface.
    """
    with get_target("auth-logic") as target:
        config = _config(tmp_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        event_log = EventLog.for_run(config.state_dir)

        async with StateGraph(":memory:") as graph:
            await graph.create_entity("run-1", "run")
            await graph.create_entity("tgt-1", "target", {"confirmed": False})

            executor = Executor(
                budgets=Budgets(
                    max_tokens=1000,
                    max_model_calls=10,
                    max_tool_calls=20,
                    max_workers=2,
                    max_hints=1,
                    max_runtime_s=120.0,
                ),
                run_id="run-1",
                event_log=event_log,
                policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
            )

            # turn 1: unauthenticated /admin — no flag anywhere
            turn1 = await executor.turn(
                graph,
                {
                    "action": f"curl -sS --max-time 5 {target.target_value}/admin",
                    "skill_id": "recon_http_fingerprint",
                },
            )
            result1 = await ShellRunner().run(
                command=turn1.action.action,
                timeout_seconds=turn1.action.timeout_seconds,
                stdout_limit=turn1.action.output_limit,
                stderr_limit=turn1.action.output_limit,
                working_directory=config.state_dir,
            )
            assert target.flag not in result1.stdout

            # turn 2: credentialed — the flag appears
            turn2 = await executor.turn(
                graph,
                {
                    "action": (
                        f"curl -sS --max-time 5 -u admin:labpass {target.target_value}/admin"
                    ),
                    "skill_id": "recon_http_fingerprint",
                },
            )
            result2 = await ShellRunner().run(
                command=turn2.action.action,
                timeout_seconds=turn2.action.timeout_seconds,
                stdout_limit=turn2.action.output_limit,
                stderr_limit=turn2.action.output_limit,
                working_directory=config.state_dir,
            )
            assert target.flag in result2.stdout

            for index, result in enumerate((result1, result2)):
                observation = ShellTextParser().parse(result)
                await graph.create_entity(
                    f"obs-{index + 1}", "observation", observation.model_dump()
                )
                await graph.create_entity(f"ev-{index + 1}", "evidence", {"note": "parsed"})
                await graph.create_edge(
                    f"ev-{index + 1}-from-obs-{index + 1}",
                    EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
                    f"ev-{index + 1}",
                    f"obs-{index + 1}",
                )

            extractor = FlagCandidateExtractor(
                run_id="run-1",
                event_log=event_log,
                pattern=LAB_FLAG_PATTERN,
            )
            candidates = await extractor.extract(graph)
            assert len(candidates) == 1
            assert candidates[0].flag == target.flag
