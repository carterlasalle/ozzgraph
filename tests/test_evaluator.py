"""Tests for the deterministic evaluator, model fallback, and replanning (PR21).

Covers the deterministic completion rules (every step completed -> plan
complete; a ranked hypothesis confirmed with new supporting evidence and
no contradictions -> plan complete), hypothesis abandonment (new
contradicting evidence, exhausted step attempt budget), plan abandonment
and autonomous replanning (every hypothesis refuted; plan model-call
budget exhausted; the graph no longer routes to the plan's phase) with
the new plan persisted and the old one superseded, the model fallback
invoked ONLY for undetermined hypotheses under the strict typed verdict
contract (with the adapters' JSON repair and the model-call budget),
loop recovery (repeated failed attempts abandon the step instead of
looping), the executor's consult surface, replay consistency of the
evaluator's graph mutations, and the typed error hierarchy (AGENTS.md
rule #9).

Every test uses its own in-memory SQLite graph (\":memory:\"). Plan
entities are seeded with explicit timestamps mirroring the executor's
persistence exactly (payload shapes and ``PLANSTEP TESTS HYPOTHESIS``
edges), so \"new evidence\" is unambiguous: an evidence edge created
after the plan entity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ozzgraph.budgets import BudgetExceeded, Budgets
from ozzgraph.evaluator import (
    EDGE_PLAN_SUPERSEDES_PLAN,
    ENTITY_EVALUATION,
    EVALUATOR_PLAN_EVALUATED,
    EVALUATOR_PLAN_REPLANNED,
    EVALUATOR_PRODUCER,
    MAX_ATTEMPTS_PER_STEP,
    MAX_MODEL_CALLS_PER_PLAN,
    Evaluator,
    EvaluatorError,
    HypothesisEvaluation,
    HypothesisVerdict,
    InvalidPlanStateError,
    MalformedEvaluatorOutputError,
    NoPlanError,
    PlanEvaluation,
    PlanVerdict,
    StepEvaluation,
    StepOutcome,
)
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.executor import (
    EDGE_PLANSTEP_TESTS_HYPOTHESIS,
    ENTITY_ACTION,
    ENTITY_PLAN,
    ENTITY_PLAN_STEP,
    Executor,
)
from ozzgraph.model_client import ModelChoice, ModelMessage, ModelRequest, ModelResponse, ModelUsage
from ozzgraph.planner import EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS, Plan, Planner
from ozzgraph.replay import replay_graph
from ozzgraph.router import EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, PhaseRouter
from ozzgraph.state_graph import StateGraph

#: Fixed timestamps: seeds (old), the plan entity, then NEW evidence —
#: real wall-clock time during the test is always after these, so
#: \"new evidence\" comparisons (edge created after the plan entity) and
#: the \"latest plan\" selection are deterministic.
OLD_AT = datetime(2020, 1, 1, tzinfo=UTC)
PLAN_AT = datetime(2020, 1, 1, 0, 0, 1, tzinfo=UTC)
NEW_AT = datetime(2020, 1, 1, 0, 0, 2, tzinfo=UTC)


class FakeModelClient:
    """A scripted ModelService stand-in that records every request."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            id="mock-1",
            model=request.model,
            created=0,
            choices=[
                ModelChoice(
                    index=0,
                    message=ModelMessage(role="assistant", content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


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


def _evaluator(*, event_log: EventLog | None = None, **overrides) -> Evaluator:
    return Evaluator(run_id="run-1", event_log=event_log, **overrides)


async def _persist_plan_at(
    graph: StateGraph,
    plan: Plan,
    at: datetime,
    *,
    event_log: EventLog | None = None,
) -> None:
    """Persist ``plan`` exactly as the executor does, at a fixed timestamp.

    Mirrors :meth:`ozzgraph.executor.Executor._persist_plan` (payload
    shapes and ``PLANSTEP TESTS HYPOTHESIS`` edges) so the evaluator
    reads a realistic persisted plan; optionally mirrors every mutation
    to the event log for replay tests.
    """
    payload = {
        "phase": plan.phase.value,
        "step_count": len(plan.steps),
        "hypotheses": [hypothesis.id for hypothesis in plan.hypotheses],
        "completion_conditions": list(plan.completion_conditions),
        "abandonment_conditions": [
            condition.model_dump(mode="json") for condition in plan.abandonment_conditions
        ],
    }
    await graph.create_entity(plan.id, ENTITY_PLAN, payload, at=at)
    if event_log is not None:
        event_log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                "run-1",
                "test",
                GraphEntityCreated(entity_id=plan.id, entity_type=ENTITY_PLAN, data=payload, at=at),
            )
        )
    for step in plan.steps:
        step_payload = {
            "hypothesis_id": step.hypothesis_id,
            "objective": step.objective,
            "skill_id": step.skill_id,
            "completion_condition": step.completion_condition,
            "abandon_condition": step.abandon_condition.model_dump(mode="json"),
        }
        await graph.create_entity(step.id, ENTITY_PLAN_STEP, step_payload, at=at)
        if event_log is not None:
            event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    "run-1",
                    "test",
                    GraphEntityCreated(
                        entity_id=step.id, entity_type=ENTITY_PLAN_STEP, data=step_payload, at=at
                    ),
                )
            )
        if step.hypothesis_id is not None:
            edge_id = f"{step.id}-tests-{step.hypothesis_id}"
            await graph.create_edge(
                edge_id, EDGE_PLANSTEP_TESTS_HYPOTHESIS, step.id, step.hypothesis_id, at=at
            )
            if event_log is not None:
                event_log.append(
                    graph_event(
                        GRAPH_EDGE_CREATED,
                        "run-1",
                        "test",
                        GraphEdgeCreated(
                            edge_id=edge_id,
                            edge_type=EDGE_PLANSTEP_TESTS_HYPOTHESIS,
                            src_id=step.id,
                            dst_id=step.hypothesis_id,
                            at=at,
                        ),
                    )
                )


async def _seed_plan_graph(
    graph: StateGraph,
    *,
    event_log: EventLog | None = None,
) -> Plan:
    """Seed a branching EXPLOITATION graph with a persisted, realistic plan.

    Baseline plus two evidenced hypotheses (hyp-a confidence 0.8 ranks
    first, hyp-b 0.7 second), then the plan entities persisted at
    :data:`PLAN_AT` with every mutation mirrored to ``event_log`` when
    provided.
    """
    seeds = [
        ("run-1", "run", {}),
        ("tgt-1", "target", {"confirmed": True}),
        ("svc-1", "service", {"characterized": True}),
        ("hyp-a", "hypothesis", {"exploitable": True, "confidence": 0.8}),
        ("hyp-b", "hypothesis", {"exploitable": True, "confidence": 0.7}),
        ("ev-a1", "evidence", {}),
        ("ev-b1", "evidence", {}),
    ]
    for entity_id, entity_type, data in seeds:
        await graph.create_entity(entity_id, entity_type, data, at=OLD_AT)
        if event_log is not None:
            event_log.append(
                graph_event(
                    GRAPH_ENTITY_CREATED,
                    "run-1",
                    "test",
                    GraphEntityCreated(
                        entity_id=entity_id, entity_type=entity_type, data=data, at=OLD_AT
                    ),
                )
            )
    for edge_id, src_id, dst_id in (
        ("ev-a1->hyp-a", "ev-a1", "hyp-a"),
        ("ev-b1->hyp-b", "ev-b1", "hyp-b"),
    ):
        await graph.create_edge(
            edge_id, EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS, src_id, dst_id, at=OLD_AT
        )
        if event_log is not None:
            event_log.append(
                graph_event(
                    GRAPH_EDGE_CREATED,
                    "run-1",
                    "test",
                    GraphEdgeCreated(
                        edge_id=edge_id,
                        edge_type=EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
                        src_id=src_id,
                        dst_id=dst_id,
                        at=OLD_AT,
                    ),
                )
            )
    route = await PhaseRouter().route(graph)
    plan = await Planner().plan(graph, route)
    assert isinstance(plan, Plan)
    await _persist_plan_at(graph, plan, PLAN_AT, event_log=event_log)
    return plan


async def _new_evidence(
    graph: StateGraph,
    hypothesis_id: str,
    evidence_id: str,
    *,
    supports: bool = True,
    event_log: EventLog | None = None,
) -> None:
    """Attach NEW evidence (created after the plan) to ``hypothesis_id``."""
    evidence_entity_id = f"ev-{evidence_id}"
    edge_type = (
        EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS if supports else EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
    )
    await graph.create_entity(evidence_entity_id, "evidence", {}, at=NEW_AT)
    edge_id = f"{evidence_entity_id}->{hypothesis_id}"
    await graph.create_edge(edge_id, edge_type, evidence_entity_id, hypothesis_id, at=NEW_AT)
    if event_log is not None:
        event_log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                "run-1",
                "test",
                GraphEntityCreated(
                    entity_id=evidence_entity_id, entity_type="evidence", data={}, at=NEW_AT
                ),
            )
        )
        event_log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                "run-1",
                "test",
                GraphEdgeCreated(
                    edge_id=edge_id,
                    edge_type=edge_type,
                    src_id=evidence_entity_id,
                    dst_id=hypothesis_id,
                    at=NEW_AT,
                ),
            )
        )


async def _attempt_action(
    graph: StateGraph,
    plan_id: str,
    step_id: str,
    *,
    index: int,
    event_log: EventLog | None = None,
) -> None:
    """Record one attempted action bound to a plan step (executor shape)."""
    fingerprint = f"{index:064x}"
    entity_id = f"action-{fingerprint}"
    payload = {
        "command": "echo probe",
        "skill_id": "exploit_parameter_injection",
        "timeout_seconds": 60,
        "output_limit": 65536,
        "fingerprint": fingerprint,
        "phase": "EXPLOITATION",
        "plan_id": plan_id,
        "plan_step_id": step_id,
        "hypothesis_id": None,
    }
    await graph.create_entity(entity_id, ENTITY_ACTION, payload, at=NEW_AT)
    if event_log is not None:
        event_log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                "run-1",
                "test",
                GraphEntityCreated(
                    entity_id=entity_id, entity_type=ENTITY_ACTION, data=payload, at=NEW_AT
                ),
            )
        )


def _step_outcomes(evaluation: PlanEvaluation) -> dict[str, StepEvaluation]:
    return {outcome.step_id: outcome for outcome in evaluation.step_outcomes}


def _hypothesis_outcomes(evaluation: PlanEvaluation) -> dict[str, HypothesisEvaluation]:
    return {outcome.hypothesis_id: outcome for outcome in evaluation.hypothesis_outcomes}


# ---------------------------------------------------------------------------
# no plan -> no evaluation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_plan_entity_raises() -> None:
    """A graph without a persisted plan cannot be evaluated (fail loudly)."""
    async with StateGraph(":memory:") as graph:
        await graph.create_entity("run-1", "run", {})
        with pytest.raises(NoPlanError, match="no plan entity"):
            await Evaluator().decide_plan(graph)


# ---------------------------------------------------------------------------
# deterministic completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_steps_complete_plan_complete() -> None:
    """Every step completed -> the plan is complete."""
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        await _new_evidence(graph, "hyp-a", "a2")
        await _new_evidence(graph, "hyp-b", "b2")
        evaluation = await Evaluator().decide_plan(graph)
    assert evaluation.verdict == PlanVerdict.COMPLETE
    assert evaluation.superseded_by is None
    assert "completion" in evaluation.reason
    assert all(outcome.outcome == StepOutcome.COMPLETED for outcome in evaluation.step_outcomes)
    assert all(
        outcome.verdict == HypothesisVerdict.CONFIRMED for outcome in evaluation.hypothesis_outcomes
    )


@pytest.mark.asyncio
async def test_hypothesis_confirmed_completes_plan_early() -> None:
    """A ranked hypothesis confirmed with new support and no contradictions."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        await _new_evidence(graph, "hyp-a", "a2")
        evaluation = await Evaluator().decide_plan(graph)
    assert evaluation.verdict == PlanVerdict.COMPLETE
    assert "new supporting evidence" in evaluation.reason
    hypotheses = _hypothesis_outcomes(evaluation)
    assert hypotheses["hyp-a"].verdict == HypothesisVerdict.CONFIRMED
    assert "ev-a2" in hypotheses["hyp-a"].supporting_evidence
    assert hypotheses["hyp-b"].verdict == HypothesisVerdict.UNDETERMINED
    steps = _step_outcomes(evaluation)
    # hyp-a's step completed; hyp-b's pending step is rendered moot
    assert steps[plan.steps[0].id].outcome == StepOutcome.COMPLETED
    assert steps[plan.steps[1].id].outcome == StepOutcome.BLOCKED


# ---------------------------------------------------------------------------
# hypothesis abandonment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_abandoned_when_contradicted() -> None:
    """New contradicting evidence refutes the hypothesis and fails its step."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        await _new_evidence(graph, "hyp-a", "ac1", supports=False)
        evaluation = await Evaluator().decide_plan(graph)
    # hyp-b is still undetermined, so the plan continues on it
    assert evaluation.verdict == PlanVerdict.CONTINUE
    hypotheses = _hypothesis_outcomes(evaluation)
    assert hypotheses["hyp-a"].verdict == HypothesisVerdict.REFUTED
    assert "ev-ac1" in hypotheses["hyp-a"].contradicting_evidence
    assert hypotheses["hyp-b"].verdict == HypothesisVerdict.UNDETERMINED
    steps = _step_outcomes(evaluation)
    assert steps[plan.steps[0].id].outcome == StepOutcome.FAILED
    assert "contradicting" in steps[plan.steps[0].id].reason
    assert steps[plan.steps[1].id].outcome == StepOutcome.PENDING


# ---------------------------------------------------------------------------
# plan abandon + replanning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_replanned_when_all_hypotheses_refuted() -> None:
    """All hypotheses refuted -> the plan is replaced by a NEW persisted plan."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        await _new_evidence(graph, "hyp-a", "ac1", supports=False)
        await _new_evidence(graph, "hyp-b", "bc1", supports=False)
        evaluation = await Evaluator().decide_plan(graph)
        assert evaluation.verdict == PlanVerdict.REPLAN
        assert evaluation.superseded_by is not None
        assert evaluation.superseded_by != plan.id
        assert "every ranked hypothesis is refuted" in evaluation.reason
        # the old plan is superseded, not rewritten: a second plan entity exists
        plans = await graph.list_entities(ENTITY_PLAN)
        assert len(plans) == 2
        # the supersedes edge points from the new plan to the old one
        neighbors = await graph.neighbors(evaluation.superseded_by, EDGE_PLAN_SUPERSEDES_PLAN)
        assert any(edge.dst_id == plan.id for edge in neighbors.outgoing)
        # the new plan is the latest persisted plan (the timeline's head)
        old_record = await graph.get_entity(plan.id)
        new_record = await graph.get_entity(evaluation.superseded_by)
        assert new_record is not None and old_record is not None
        assert (new_record.created_at, new_record.id) > (old_record.created_at, old_record.id)
        # the new plan is evaluable and its steps were persisted
        assert len(await graph.list_entities(ENTITY_PLAN_STEP)) == 4
        latest, _ = await Evaluator()._load_latest_plan(graph)
        assert latest.id == evaluation.superseded_by


@pytest.mark.asyncio
async def test_plan_replanned_when_model_call_budget_exhausted() -> None:
    """A plan that exhausts its model-call budget is abandoned and re-planned."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        # actions on the plan (one per approved executor turn = one model call)
        # never trip the step attempt budget (raised threshold isolates the plan budget)
        for index in range(MAX_MODEL_CALLS_PER_PLAN):
            await _attempt_action(graph, plan.id, plan.steps[0].id, index=index)
        evaluation = await _evaluator(
            max_attempts_per_step=100, max_model_calls_per_plan=MAX_MODEL_CALLS_PER_PLAN
        ).decide_plan(graph)
    assert evaluation.verdict == PlanVerdict.REPLAN
    assert "model-call budget" in evaluation.reason
    assert evaluation.superseded_by is not None


@pytest.mark.asyncio
async def test_plan_abandoned_when_graph_leaves_plan_phase() -> None:
    """The graph no longer routing to the plan's phase abandons it (no replan)."""
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        # no supported exploitable hypothesis remains -> the graph routes to REPLAN,
        # which has no skill packs, so no replacement plan is derivable
        await graph.update_entity("hyp-a", {"exploitable": False, "confidence": 0.8})
        await graph.update_entity("hyp-b", {"exploitable": False, "confidence": 0.7})
        evaluation = await Evaluator().decide_plan(graph)
    assert evaluation.verdict == PlanVerdict.ABANDON
    assert evaluation.superseded_by is None
    assert "no longer routes" in evaluation.reason
    assert "no replacement plan" in evaluation.reason


@pytest.mark.asyncio
async def test_plan_abandoned_when_graph_not_branching() -> None:
    """All hypotheses refuted + a non-branching graph -> abandon, no successor."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        # hyp-a refuted by NEW contradicting evidence
        await _new_evidence(graph, "hyp-a", "ac1", supports=False)
        # hyp-b loses its only evidence ref and burns its attempt budget
        await graph.delete_entity("ev-b1")
        for index in range(MAX_ATTEMPTS_PER_STEP):
            await _attempt_action(graph, plan.id, plan.steps[1].id, index=index)
        evaluation = await Evaluator().decide_plan(graph)
        assert evaluation.verdict == PlanVerdict.ABANDON
        assert evaluation.superseded_by is None
        assert "no replacement plan" in evaluation.reason
        assert len(await graph.list_entities(ENTITY_PLAN)) == 1


# ---------------------------------------------------------------------------
# loop recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_abandoned_after_attempt_threshold_no_loop() -> None:
    """Repeated failed attempts abandon the step; the plan never loops on it."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        for index in range(MAX_ATTEMPTS_PER_STEP):
            await _attempt_action(graph, plan.id, plan.steps[0].id, index=index)
        evaluator = Evaluator()
        evaluation = await evaluator.decide_plan(graph)
        steps = _step_outcomes(evaluation)
        failed = steps[plan.steps[0].id]
        assert failed.outcome == StepOutcome.FAILED
        assert failed.attempts == MAX_ATTEMPTS_PER_STEP
        assert "attempt" in failed.reason
        assert evaluation.verdict == PlanVerdict.CONTINUE  # hyp-b is still alive
        # repeated consultation stays bounded: the step stays failed, never retried
        second = await evaluator.decide_plan(graph)
        assert second.verdict == PlanVerdict.CONTINUE
        assert _step_outcomes(second)[plan.steps[0].id].outcome == StepOutcome.FAILED
        assert _step_outcomes(second)[plan.steps[0].id].attempts == MAX_ATTEMPTS_PER_STEP


# ---------------------------------------------------------------------------
# model fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_fallback_invoked_only_when_deterministic_rules_inconclusive() -> None:
    """Undetermined hypotheses reach the model; decided ones never do."""
    mock = FakeModelClient('{"verdict": "confirmed", "reason": "banner matches the login page"}')
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        # hyp-a is decided deterministically (new supporting evidence);
        # hyp-b is undetermined and must be the ONLY fallback call
        await _new_evidence(graph, "hyp-a", "a2")
        evaluation = await _evaluator(model_client=mock, model_name="mock-model").decide_plan(graph)
        assert len(mock.requests) == 1
        content = mock.requests[0].messages[1].content or ""
        assert "hyp-b" in content
        assert mock.requests[0].temperature == 0.0
        assert mock.requests[0].response_format == {"type": "json_object"}
    hypotheses = _hypothesis_outcomes(evaluation)
    assert hypotheses["hyp-a"].verdict == HypothesisVerdict.CONFIRMED
    assert "model fallback" not in hypotheses["hyp-a"].reason
    assert hypotheses["hyp-b"].verdict == HypothesisVerdict.CONFIRMED
    assert hypotheses["hyp-b"].reason.startswith("model fallback")
    assert evaluation.verdict == PlanVerdict.COMPLETE


@pytest.mark.asyncio
async def test_model_fallback_not_called_when_deterministic_rules_decide() -> None:
    """Both hypotheses decided deterministically -> the model is never called."""
    mock = FakeModelClient('{"verdict": "confirmed", "reason": "unused"}')
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        await _new_evidence(graph, "hyp-a", "a2")
        await _new_evidence(graph, "hyp-b", "b2")
        evaluation = await _evaluator(model_client=mock, model_name="mock-model").decide_plan(graph)
        assert mock.requests == []
        assert evaluation.verdict == PlanVerdict.COMPLETE
        assert all(
            outcome.verdict == HypothesisVerdict.CONFIRMED
            for outcome in evaluation.hypothesis_outcomes
        )


@pytest.mark.asyncio
async def test_model_fallback_fenced_json_is_repaired() -> None:
    """A markdown-fenced fallback completion is repaired via the adapters' strategy."""
    mock = FakeModelClient(
        '```json\n{"verdict": "refuted", "reason": "service is unreachable"}\n```'
    )
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        evaluation = await _evaluator(model_client=mock, model_name="mock-model").decide_plan(graph)
    hypotheses = _hypothesis_outcomes(evaluation)
    assert hypotheses["hyp-a"].verdict == HypothesisVerdict.REFUTED
    assert hypotheses["hyp-a"].reason.startswith("model fallback")


@pytest.mark.asyncio
async def test_model_fallback_malformed_output_raises() -> None:
    """Unrepairable fallback output fails loudly (AGENTS.md rule #9)."""
    mock = FakeModelClient("sure, the hypothesis looks fine to me")
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        with pytest.raises(MalformedEvaluatorOutputError, match="not a JSON object"):
            await _evaluator(model_client=mock, model_name="mock-model").decide_plan(graph)


@pytest.mark.asyncio
async def test_model_fallback_respects_model_call_budget() -> None:
    """An exhausted model-call budget fails loudly instead of calling the model."""
    budgets = _budgets(max_model_calls=1)
    budgets.consume_model_call()
    mock = FakeModelClient('{"verdict": "confirmed", "reason": "x"}')
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        with pytest.raises(BudgetExceeded):
            await _evaluator(
                budgets=budgets, model_client=mock, model_name="mock-model"
            ).decide_plan(graph)
    assert mock.requests == []


@pytest.mark.asyncio
async def test_model_fallback_disabled_per_call() -> None:
    """``use_model_fallback=False`` skips the model even when configured."""
    mock = FakeModelClient('{"verdict": "confirmed", "reason": "x"}')
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        evaluator = _evaluator(model_client=mock, model_name="mock-model")
        evaluation = await evaluator.decide_plan(graph, use_model_fallback=False)
        assert mock.requests == []
    hypotheses = _hypothesis_outcomes(evaluation)
    assert all(outcome.verdict == HypothesisVerdict.UNDETERMINED for outcome in hypotheses.values())


# ---------------------------------------------------------------------------
# persistence and replay consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_decision_persisted_as_entity_and_events(tmp_path) -> None:
    """Every decision is persisted as an evaluation entity plus events."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph, event_log=event_log)
        await _new_evidence(graph, "hyp-a", "a2", event_log=event_log)
        await _evaluator(event_log=event_log).decide_plan(graph)
        records = await graph.list_entities(ENTITY_EVALUATION)
        assert len(records) == 1
        assert records[0].id == f"eval-{plan.id}-1"
        assert records[0].data["plan_id"] == plan.id
        assert records[0].data["verdict"] == "complete"
    events = [json.loads(line) for line in (tmp_path / "actions.jsonl").read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types.count(EVALUATOR_PLAN_EVALUATED) == 1
    evaluated = next(event for event in events if event["event_type"] == EVALUATOR_PLAN_EVALUATED)
    assert evaluated["producer"] == EVALUATOR_PRODUCER
    assert evaluated["payload"]["verdict"] == "complete"


@pytest.mark.asyncio
async def test_replan_persisted_with_replayed_events_reconstructing_hash(tmp_path) -> None:
    """Replaying the evaluator's events reconstructs the identical graph hash."""
    event_log = EventLog(tmp_path / "actions.jsonl")
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph, event_log=event_log)
        await _new_evidence(graph, "hyp-a", "ac1", supports=False, event_log=event_log)
        await _new_evidence(graph, "hyp-b", "bc1", supports=False, event_log=event_log)
        evaluation = await _evaluator(event_log=event_log).decide_plan(graph)
        assert evaluation.verdict == PlanVerdict.REPLAN
        assert evaluation.superseded_by is not None
        live_hash = await graph.graph_hash()
    replayed_hash = await replay_graph(tmp_path / "actions.jsonl", tmp_path / "replay.db")
    assert replayed_hash == live_hash


# ---------------------------------------------------------------------------
# executor integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_consults_evaluator() -> None:
    """The executor can consult the evaluator between turns; None when unset."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        executor = Executor(budgets=_budgets(), run_id="run-1", evaluator=Evaluator())
        evaluation = await executor.consult_evaluator(graph)
        assert isinstance(evaluation, PlanEvaluation)
        assert evaluation.plan_id == plan.id
        assert evaluation.verdict == PlanVerdict.CONTINUE
        bare = Executor(budgets=_budgets(), run_id="run-1")
        assert await bare.consult_evaluator(graph) is None


@pytest.mark.asyncio
async def test_executor_consult_raises_without_plan() -> None:
    """Consulting before any plan exists raises the evaluator's typed NoPlanError."""
    async with StateGraph(":memory:") as graph:
        await graph.create_entity("run-1", "run", {})
        executor = Executor(budgets=_budgets(), run_id="run-1", evaluator=Evaluator())
        with pytest.raises(NoPlanError):
            await executor.consult_evaluator(graph)


# ---------------------------------------------------------------------------
# invalid persisted state (AGENTS.md rule #9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_plan_payload_raises() -> None:
    """A wrong-typed persisted plan payload fails loudly, never coerced."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        await graph.update_entity(plan.id, {})
        with pytest.raises(InvalidPlanStateError, match="phase"):
            await Evaluator().decide_plan(graph)


@pytest.mark.asyncio
async def test_missing_step_entity_raises() -> None:
    """A plan missing a persisted step entity fails loudly."""
    async with StateGraph(":memory:") as graph:
        plan = await _seed_plan_graph(graph)
        await graph.delete_entity(plan.steps[0].id)
        with pytest.raises(InvalidPlanStateError, match="step"):
            await Evaluator().decide_plan(graph)


@pytest.mark.asyncio
async def test_wrong_typed_hypothesis_confidence_raises() -> None:
    """A non-numeric confidence in the graph fails loudly."""
    async with StateGraph(":memory:") as graph:
        await _seed_plan_graph(graph)
        await graph.update_entity("hyp-a", {"exploitable": True, "confidence": "high"})
        with pytest.raises(InvalidPlanStateError, match="confidence"):
            await Evaluator().decide_plan(graph)


# ---------------------------------------------------------------------------
# typed errors and strict schemas
# ---------------------------------------------------------------------------


def test_error_hierarchy_is_typed() -> None:
    """All evaluator error classes derive from EvaluatorError(RuntimeError)."""
    assert issubclass(NoPlanError, EvaluatorError)
    assert issubclass(InvalidPlanStateError, EvaluatorError)
    assert issubclass(MalformedEvaluatorOutputError, EvaluatorError)
    assert issubclass(EvaluatorError, RuntimeError)


def test_schemas_reject_extra_fields() -> None:
    """The evaluation schemas are strict pydantic contracts (extra='forbid')."""
    with pytest.raises(ValidationError):
        StepEvaluation(step_id="s1", outcome=StepOutcome.PENDING, attempts=0, reason="r", bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        HypothesisEvaluation(
            hypothesis_id="h1", verdict=HypothesisVerdict.UNDETERMINED, reason="r", bogus=1
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PlanEvaluation(plan_id="p1", verdict=PlanVerdict.CONTINUE, reason="r", bogus=1)  # type: ignore[call-arg]


def test_module_constants_are_documented() -> None:
    """The loop-recovery threshold and plan budget are explicit module state."""
    assert MAX_ATTEMPTS_PER_STEP == 3
    assert MAX_MODEL_CALLS_PER_PLAN == 10
    assert EVALUATOR_PRODUCER == "evaluator"
    assert EVALUATOR_PLAN_EVALUATED == "evaluator.plan_evaluated"
    assert EVALUATOR_PLAN_REPLANNED == "evaluator.plan_replanned"
    assert EDGE_PLAN_SUPERSEDES_PLAN == "PLAN SUPERSEDES PLAN"
