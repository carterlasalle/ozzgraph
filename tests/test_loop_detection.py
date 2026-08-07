"""Loop and timeout detection tests for the OzzGraph harness (PR29).

Proves the docs/TESTING_AND_QA.md "Chaos Tests" loop/timeout behaviors:

- repetition detection: a looping model's repeated proposals are rejected
  by the executor (fingerprint store, never executed twice) and by the
  matrix layer (repetition-rate metric), and a plan whose every step
  failed is abandoned (:class:`PlanExhaustedError`) — never retried
  forever;
- action-budget abandonment: a plan that exhausts its model-call budget
  is abandoned/re-planned by the evaluator, never looped;
- timeout recovery: the bounded shell runner kills the hanging process
  group, the parser carries the timeout into the observation, the failed
  action is fed back, and the executor skips the dead step and continues
  with the next one — no hang, no infinite retry.

Every test is local: in-memory SQLite graphs and the loopback lab target
for the matrix episode (the only live endpoint allowed).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest

from ozzgraph.budgets import Budgets
from ozzgraph.evaluator import (
    MAX_MODEL_CALLS_PER_PLAN,
    Evaluator,
    PlanVerdict,
)
from ozzgraph.events import EventLog
from ozzgraph.executor import (
    DuplicateFingerprintError,
    Executor,
    FailedAction,
    PlanExhaustedError,
)
from ozzgraph.lab import get_target
from ozzgraph.matrix import evaluate_model
from ozzgraph.observations import SHELL_TEXT_PARSER
from ozzgraph.planner import Plan, Planner
from ozzgraph.policy import ScopePolicy, fingerprint_command
from ozzgraph.router import EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, PhaseRouter
from ozzgraph.shell import ShellRunner
from ozzgraph.state_graph import StateGraph
from ozzgraph.traces import TraceMetrics

TARGET = "http-recon"

_URL_RE = re.compile(r"http://127\.0\.0\.1:\d+")


async def _entity(
    graph: StateGraph,
    entity_id: str,
    entity_type: str,
    data: dict[str, object] | None = None,
) -> None:
    """Create one entity."""
    await graph.create_entity(entity_id, entity_type, data)


async def _edge(
    graph: StateGraph,
    edge_id: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
) -> None:
    """Create one typed edge."""
    await graph.create_edge(edge_id, edge_type, src_id, dst_id)


async def _seed_baseline(graph: StateGraph) -> None:
    """Seed a run with a confirmed target and a characterized service."""
    await _entity(graph, "run-1", "run")
    await _entity(graph, "tgt-1", "target", {"confirmed": True})
    await _entity(graph, "svc-1", "service", {"characterized": True})


async def _seed_branching_exploitation(graph: StateGraph) -> None:
    """Seed a branching exploitation graph: two evidenced hypotheses."""
    await _seed_baseline(graph)
    await _entity(graph, "hyp-a", "hypothesis", {"exploitable": True, "confidence": 0.8})
    await _entity(graph, "ev-a1", "evidence", {"note": "banner"})
    await _edge(
        graph,
        "ev-a1->hyp-a",
        EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
        "ev-a1",
        "hyp-a",
    )
    await _entity(graph, "hyp-b", "hypothesis", {"exploitable": True, "confidence": 0.9})
    await _entity(graph, "ev-b1", "evidence", {"note": "route"})
    await _edge(
        graph,
        "ev-b1->hyp-b",
        EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
        "ev-b1",
        "hyp-b",
    )


async def _seed_recon(graph: StateGraph) -> None:
    """Seed the RECON state: a run with an unconfirmed target."""
    await _entity(graph, "run-1", "run")
    await _entity(graph, "tgt-1", "target", {"confirmed": False})


def _budgets(**overrides) -> Budgets:
    base = {
        "max_tokens": 100,
        "max_model_calls": 10,
        "max_tool_calls": 20,
        "max_workers": 2,
        "max_hints": 1,
        "max_runtime_s": 100.0,
    }
    base.update(overrides)
    return Budgets(**base)


def _executor(*, event_log: EventLog | None = None, **overrides) -> Executor:
    return Executor(
        budgets=overrides.pop("budgets", _budgets()),
        run_id="run-1",
        event_log=event_log,
        policy=overrides.pop("policy", ScopePolicy()),
        **overrides,
    )


async def _attempt_action(
    graph: StateGraph,
    plan_id: str,
    step_id: str,
    *,
    index: int,
) -> None:
    """Record one attempted action bound to a plan step (executor shape)."""
    fingerprint = f"{index:064x}"
    at = datetime.now(UTC)
    entity_id = f"action-{fingerprint}"
    payload: dict[str, object] = {
        "command": f"echo attempt-{index}",
        "skill_id": "recon_dns_enum",
        "timeout_seconds": 10,
        "output_limit": 4096,
        "fingerprint": fingerprint,
        "phase": "EXPLOITATION",
        "plan_id": plan_id,
        "plan_step_id": step_id,
        "hypothesis_id": "hyp-a",
    }
    await graph.create_entity(entity_id, "action", payload, at=at)


# ---------------------------------------------------------------------------
# repetition detection: the executor never executes the same action twice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_proposal_rejected_across_turns() -> None:
    """A looping model's identical proposal is rejected on the second turn.

    The first proposal is recorded (fingerprint store + action entity); the
    identical proposal never executes again — the loop is broken loudly.
    """
    async with StateGraph(":memory:") as graph:
        await _seed_recon(graph)
        executor = _executor()
        first = await executor.turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})
        assert first.action.fingerprint == fingerprint_command("echo probe")[1]

        with pytest.raises(DuplicateFingerprintError, match="already recorded"):
            await executor.turn(graph, {"action": "echo probe", "skill_id": "recon_dns_enum"})

        # Only one action entity was ever recorded: nothing executed twice.
        actions = await graph.list_entities("action")
        assert len(actions) == 1


@pytest.mark.asyncio
async def test_plan_abandoned_when_every_step_failed() -> None:
    """A plan whose every step has a failed attempt is abandoned loudly.

    Feeding failed actions for every step raises PlanExhaustedError — the
    harness never retries a dead plan into an infinite loop.
    """
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        failed_actions = [
            FailedAction(
                fingerprint=fingerprint_command(f"echo stale-{index}")[1],
                reason="timeout",
                plan_step_id=step.id,
            )
            for index, step in enumerate(plan.steps)
        ]
        with pytest.raises(PlanExhaustedError, match="refusing"):
            await _executor().turn(
                graph,
                {"action": "echo probe", "skill_id": plan.steps[0].skill_id},
                failed_actions=failed_actions,
            )


# ---------------------------------------------------------------------------
# action-budget abandonment: the evaluator never loops a budget-exhausted plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_model_call_budget_abandons_looping_plan() -> None:
    """A plan at its model-call budget is abandoned/re-planned, never continued.

    Each approved executor turn records one action entity (one model call);
    once the plan's budget is exhausted the evaluator stops the loop.
    """
    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan)
        # One approved executor turn: persists the plan and records one action.
        turn = await _executor().turn(
            graph, {"action": "echo probe", "skill_id": plan.steps[0].skill_id}
        )
        assert turn.action.plan_id == plan.id
        # Burn the rest of the plan's model-call budget with bound actions.
        for index in range(MAX_MODEL_CALLS_PER_PLAN - 1):
            await _attempt_action(graph, plan.id, plan.steps[0].id, index=index)

        evaluation = await Evaluator(
            max_attempts_per_step=100,  # isolate the plan budget from the step budget
            max_model_calls_per_plan=MAX_MODEL_CALLS_PER_PLAN,
        ).decide_plan(graph)

    assert evaluation.verdict in (PlanVerdict.ABANDON, PlanVerdict.REPLAN)
    assert "model-call budget" in evaluation.reason
    assert evaluation.verdict is not PlanVerdict.CONTINUE


# ---------------------------------------------------------------------------
# matrix repetition detection: a pure looping model is measured and bounded
# ---------------------------------------------------------------------------


class _LoopingModel:
    """A degenerate model: emits the same run command on every turn."""

    def __init__(self) -> None:
        self._target: str | None = None
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._target is None:
            match = _URL_RE.search(prompt)
            if match is not None:
                self._target = match.group(0)
        return (
            '{"kind": "run", "payload": "curl ' + (self._target or "http://127.0.0.1:9") + '", '
            '"rationale": "same action again"}'
        )


def test_matrix_repetition_rate_detects_looping_model() -> None:
    """A looping model's repeats are flagged and bounded by max_turns."""

    async def _run() -> TraceMetrics:
        with get_target(TARGET) as target:
            assert target is not None
            model = _LoopingModel()
            report = await evaluate_model(
                model,
                model_id="looping-model",
                targets=(TARGET,),
                protocols=("json",),
                max_turns=5,
                flag_pattern=r"OZ\{[^{}\s]+\}",
            )
            row = report.row_for("json")
            assert row is not None
            assert row.steps == 5  # bounded: the episode did not hang or loop past the cap
            for index, step in enumerate(row.interactions):
                assert step.kind == "run"
                assert step.repeated is (index > 0)  # identical fingerprint from turn 2 on
            return row.metrics

    metrics = asyncio.run(_run())
    assert metrics.repetition_rate == 0.8  # 4 repeated runs / 5 run actions


# ---------------------------------------------------------------------------
# timeout recovery: kill, record, feed back, continue — never hang, never retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_recovery_chain(tmp_path) -> None:
    """A hanging action: killed by the runner, labeled by the parser, skipped
    by the executor — the loop continues with the next plan step.

    This is the full recovery chain for one timed-out action: the bounded
    shell runner kills the process group (``timeout_state``), the parser
    carries the timeout into the observation summary, the failed action is
    fed back with ``reason=\"timeout\"``, and the executor selects the next
    plan step instead of retrying the dead one.
    """
    runner = ShellRunner()
    hung = await runner.run(
        command="sleep 30",
        timeout_seconds=0.2,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert hung.timeout_state  # the runner killed it, did not hang

    obs = SHELL_TEXT_PARSER.parse(hung)
    assert "timed out" in obs.summary  # the parser labels the timeout

    async with StateGraph(":memory:") as graph:
        await _seed_branching_exploitation(graph)
        route = await PhaseRouter().route(graph)
        plan = await Planner().plan(graph, route)
        assert isinstance(plan, Plan) and len(plan.steps) >= 2
        failed = FailedAction(
            fingerprint=fingerprint_command("sleep 30")[1],
            reason="timeout",
            plan_step_id=plan.steps[0].id,
        )
        turn = await _executor().turn(
            graph,
            {"action": "echo fresh", "skill_id": plan.steps[1].skill_id},
            failed_actions=[failed],
        )
        assert turn.action.plan_step_id == plan.steps[1].id  # skipped the dead step
        assert turn.action.plan_step_id != plan.steps[0].id  # never retried it
