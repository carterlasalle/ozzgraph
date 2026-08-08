"""AutonomousRunner — the generic investigate loop for OzzGraph (V01).

Implements the v2 "most important fix" (docs/CHANGES_v2.md): the
supervisor actually DRIVES the agent. :class:`AutonomousRunner.run` is
the real investigate loop — route -> check objectives -> plan (only
when the graph is branching) -> compile context -> ONE bounded model
action -> execute -> persist raw output + observation/evidence ->
evaluate -> repeat — until the objectives are met, a budget is
exhausted, or the supervisor's stop event is set.

The loop is a pragmatic minimal-but-real V01 slice (V02 is the true
process-level end-to-end slice): it reuses the existing v1 components
unchanged — :class:`~ozzgraph.router.PhaseRouter`,
:class:`~ozzgraph.planner.Planner`, :class:`~ozzgraph.context`
compiler, :class:`~ozzgraph.adapters` model adapters,
:class:`~ozzgraph.executor.Executor`,
:class:`~ozzgraph.evaluator.Evaluator`, the
:class:`~ozzgraph.policy.ScopePolicy` gate, the bounded
:class:`~ozzgraph.shell.ShellRunner`, and the
:class:`~ozzgraph.artifacts.ArtifactStore`. NOTHING is rewritten here.

Design rules:

- Real work, never sleep-wait (docs/CHANGES_v2.md): each iteration
  routes, plans, compiles context, calls the model, and — when the
  model proposes a bounded ``run`` action — executes it through the
  policy gate and shell runner. The loop contains no idle sleep; the
  only awaits are the component calls themselves.
- One bounded action per turn (AGENTS.md rule #4): the executor
  (PR20) already enforces the strict one-action-per-turn contract; the
  runner feeds it exactly one parsed model action per iteration and
  executes exactly one command. The model's output is parsed through
  the existing adapters (the model never speaks raw MCP, AGENTS.md
  rule #5).
- Fail loudly, never silently (AGENTS.md rule #9): every model
  failure, adapter parse failure, executor refusal, policy rejection,
  and shell failure is recorded as a structured ``runner.*`` event.
  Well-understood model/execution failures are recorded and the loop
  continues (the budget bounds them, mirroring the bootstrap's
  non-fatal Hal-service convention); budget exhaustion, a supervisor
  stop, all-objectives-completed, and unexpected kernel errors
  terminate with a structured :class:`RunnerStatus`.
- Authoritative state stays in the graph (AGENTS.md rule #1): the
  environment adapter's discoveries are SEEDED into the SQLite graph
  as entities (``run``, ``scope``, ``target``, ``objective``) with
  mirrored ``graph.*`` events, and every subsequent check (objectives
  complete, routing, plan state) reads the graph — never the model,
  never the adapter's in-memory view.
- Generic DONE (docs/adr/0008): the loop terminates COMPLETED when
  every seeded ``objective`` entity is ``completed: true`` (flipped
  only through deterministic paths — an accepted submission or an
  evaluator COMPLETE verdict) or when the router reaches DONE (the
  HalCTF accepted-submission terminal signal).
- Deterministic: seeding is idempotent (entity ids derive from
  addresses/objective ids), observation/evidence ids derive from the
  action fingerprint, and plan/step selection reuses the executor's
  deterministic rules.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ozzgraph.adapters import (
    ADAPTERS,
    AdapterParseError,
    ModelAdapter,
    ParsedAction,
    adapter_for,
)
from ozzgraph.artifacts import ArtifactStore
from ozzgraph.budgets import BudgetExceeded, Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.context import CompiledContext, ContextRequest, compile_context
from ozzgraph.environments import EnvironmentAdapter, Objective, Scope
from ozzgraph.evaluator import (
    Evaluator,
    HypothesisVerdict,
    NoPlanError,
    PlanEvaluation,
    PlanVerdict,
)
from ozzgraph.events import (
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    GRAPH_ENTITY_UPDATED,
    Event,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    GraphEntityUpdated,
    graph_event,
)
from ozzgraph.executor import (
    ENTITY_ACTION,
    Executor,
    ExecutorError,
    ExecutorTurn,
    FailedAction,
)
from ozzgraph.findings import (
    DEFAULT_FINDING_CWE,
    EDGE_FINDING_VALIDATES_HYPOTHESIS,
    ENTITY_FINDING,
    REPRODUCTION_LIMIT,
    Finding,
    FindingStore,
    ImpactCIA,
    ImpactLevel,
)
from ozzgraph.model_client import (
    DEFAULT_MODEL_BASE_URL,
    ModelMessage,
    ModelRequest,
    ModelService,
    ModelServiceError,
)
from ozzgraph.observations import observation_for_result
from ozzgraph.phases import Phase
from ozzgraph.planner import (
    EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS,
    NoPlanDecision,
    Plan,
    Planner,
)
from ozzgraph.policy import ScopePolicy, ScopeViolationError
from ozzgraph.profiles import ModelProfile, probe_protocol, profile_for_model_id
from ozzgraph.router import (
    EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
    ENTITY_HYPOTHESIS,
    ENTITY_OBJECTIVE,
    FIELD_COMPLETED,
    PhaseRoute,
    PhaseRouter,
)
from ozzgraph.shell import ShellRunner, ToolResult
from ozzgraph.skills import SkillRegistry
from ozzgraph.state_graph import EntityRecord, StateGraph
from ozzgraph.toolplane import ToolInventory

#: Producer name on every runner event.
RUNNER_PRODUCER = "runner"

#: Run-log events emitted by the runner loop.
RUNNER_STARTED = "runner.started"
RUNNER_TURN = "runner.turn"
RUNNER_MODEL_FAILURE = "runner.model_failure"
RUNNER_ACTION_EXECUTED = "runner.action_executed"
RUNNER_ACTION_FAILED = "runner.action_failed"
RUNNER_EVALUATED = "runner.evaluated"
RUNNER_OBJECTIVE_COMPLETED = "runner.objective_completed"
RUNNER_TERMINATED = "runner.terminated"
RUNNER_FINDING_CREATED = "runner.finding_created"

#: Entity types the runner seeds (docs/DATA_STRATEGY.md, lowercase by
#: convention).
ENTITY_RUN = "run"
ENTITY_SCOPE = "scope"
ENTITY_TARGET = "target"
ENTITY_OBSERVATION = "observation"
ENTITY_EVIDENCE = "evidence"

#: Edge types the runner writes.
EDGE_ACTION_PRODUCED_OBSERVATION = "ACTION PRODUCED OBSERVATION"
EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION = "EVIDENCE EXTRACTED_FROM OBSERVATION"

#: Deterministic confidence stamped on a hypothesis formed from a
#: successful observation (V02 minimal; the security-brain milestone
#: replaces the heuristic with measured reasoning).
HYPOTHESIS_CONFIDENCE = 0.6

#: Confidence stamped on a hypothesis whose observation output matched
#: the configured flag pattern — the deterministic sensitive-data
#: signal (``config.flag_pattern``, the same pattern the flag candidate
#: extractor scans with).
HYPOTHESIS_FLAG_CONFIDENCE = 0.9

#: Deterministic impact labels for a finding whose validated evidence
#: exposed the target's sensitive data (matched the flag pattern).
_FINDING_EXPOSED_IMPACT: dict[str, ImpactLevel] = {
    "confidentiality": "high",
    "integrity": "low",
    "availability": "none",
}

#: Impact labels for a finding validated without the sensitive-data
#: signal: the conservative unknown axes (never inflated).
_FINDING_DEFAULT_IMPACT: dict[str, ImpactLevel] = {
    "confidentiality": "medium",
    "integrity": "unknown",
    "availability": "unknown",
}

#: Environment variable holding the model identifier the runner calls.
MODEL_ID_ENV = "OZZGRAPH_MODEL_ID"

#: Model identifier used when ``OZZGRAPH_MODEL_ID`` is unset. Maps to
#: the conservative fallback profile (terminal protocol only).
DEFAULT_MODEL_ID = "default"

#: Per-stream output cap for executed actions (mirrors the executor's
#: default output limit; the executor's ActionRequest carries the
#: authoritative value).
DEFAULT_OUTPUT_LIMIT = 65536

#: Bounded mission text: at most this many characters of the scope /
#: objectives / capabilities summary enter model context.
_MISSION_LIMIT = 2000

#: Bounded per-line summary for observation entities.
_SUMMARY_LIMIT = 200

#: The strict output contract the runner advertises to the model. This
#: is the executor's :class:`~ozzgraph.executor.ModelAction` contract —
#: the model proposes ONE bounded action plus the skill it selected.
OUTPUT_CONTRACT = (
    "Propose exactly ONE bounded action as a single JSON object:\n"
    '  {"action": "<one shell command line>", "skill_id": "<skill id>"}\n'
    "The action must be a single command (never a multi-command plan or a "
    "loop), must stay within the authorized scope, and must select one of "
    "the advertised skills. If you have no action to take, respond with "
    "plain reasoning instead."
)


class RunnerStatus(str, Enum):
    """Structured outcome of one :meth:`AutonomousRunner.run` call.

    Attributes:
        COMPLETED: Every objective completed (or the router reached
            DONE via an accepted submission) — the run's success path.
        STOPPED: The supervisor's stop event was set (signal).
        FAILED: An unexpected kernel error aborted the run loudly.
        BUDGET_EXHAUSTED: A bounded budget dimension was exhausted.
    """

    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class RunnerError(RuntimeError):
    """Base error for the runner layer (AGENTS.md rule #9)."""


class RunnerStateError(RunnerError):
    """The runner was used with an invalid graph or component state.

    Raised when the injected graph is closed, an environment discovery
    raises a non-configuration error during seeding, or the runner is
    mis-wired (e.g. no evaluator when one was promised).
    """


class RunnerStatusEvent(BaseModel):
    """Structured payload for the runner's terminal event.

    Attributes:
        status: The :class:`RunnerStatus` the run ended with.
        turns: Number of loop iterations executed.
        model_calls: Model calls consumed (from the budget tracker).
        tool_calls: Tool calls consumed (from the budget tracker).
        reason: Human-readable justification for the outcome.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    turns: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    reason: str = Field(min_length=1)


class AutonomousRunner:
    """The generic investigate loop the supervisor drives (V01).

    Args:
        config: The validated runtime configuration (scope policy
            construction, working directory for executed actions).
        graph: The open authoritative SQLite state graph; the runner
            seeds the environment into it and reads it every iteration.
        event_log: Append-only log every ``runner.*`` and mirrored
            ``graph.*`` event is written to.
        artifacts: The artifact store raw action output is persisted
            into BEFORE any summary enters the graph (AGENTS.md rule
            #1).
        budgets: The budget tracker checked each iteration and consumed
            by the executor per approved turn.
        environment: The runtime environment adapter (the v2 pivot) —
            its discoveries seed the graph.
        stop_event: Optional supervisor stop signal; when set the loop
            terminates with :attr:`RunnerStatus.STOPPED`.
        run_id: Run identifier recorded on every event; defaults to
            ``config.hal_user_id`` when empty.
        model_id: Model identifier called for completions; defaults to
            ``OZZGRAPH_MODEL_ID`` or :data:`DEFAULT_MODEL_ID`.
        profile: Model profile bounding context and protocol selection;
            defaults to :func:`~ozzgraph.profiles.profile_for_model_id`
            over ``model_id`` (unknown ids map to the conservative
            fallback profile).
        model_service: Model client; defaults to a runner-owned
            :class:`~ozzgraph.model_client.ModelService` (closed by
            :meth:`aclose`).
        router: Graph-driven phase router; defaults to a
            :class:`PhaseRouter`.
        planner: Deterministic planner; defaults to a :class:`Planner`.
        executor: One-action-per-turn executor; defaults to an
            :class:`Executor` wired over the injected router/planner/
            policy/budgets/event log.
        evaluator: PR21 plan evaluator; when ``None`` evaluation is
            skipped (no COMPLETE verdicts, objectives only complete via
            the accepted-submission DONE path).
        policy: Scope policy gate for EXECUTION; defaults to a
            fail-closed :class:`ScopePolicy`.
        shell: Bounded shell runner for execution; defaults to a
            :class:`ShellRunner`.
        inventory: The V03 tool-plane inventory (docs/CHANGES_v2.md
            milestone 3); defaults to a fresh :class:`ToolInventory`
            probed against the environment's PATH at startup. Its
            available capabilities bound the model context: the model
            only hears about capabilities backed by installed tools.
    """

    def __init__(
        self,
        *,
        config: OzzGraphConfig,
        graph: StateGraph,
        event_log: EventLog,
        artifacts: ArtifactStore,
        budgets: Budgets,
        environment: EnvironmentAdapter,
        stop_event: asyncio.Event | None = None,
        run_id: str = "",
        model_id: str | None = None,
        profile: ModelProfile | None = None,
        model_service: ModelService | None = None,
        router: PhaseRouter | None = None,
        planner: Planner | None = None,
        executor: Executor | None = None,
        evaluator: Evaluator | None = None,
        policy: ScopePolicy | None = None,
        shell: ShellRunner | None = None,
        inventory: ToolInventory | None = None,
    ) -> None:
        self._config = config
        self._graph = graph
        self._event_log = event_log
        self._artifacts = artifacts
        self._budgets = budgets
        self._environment = environment
        self._stop_event = stop_event if stop_event is not None else asyncio.Event()
        self._run_id = run_id if run_id else config.hal_user_id
        self._model_id = (
            model_id
            if model_id is not None
            else os.environ.get(MODEL_ID_ENV, "") or DEFAULT_MODEL_ID
        )
        self._profile = profile if profile is not None else profile_for_model_id(self._model_id)
        self._policy = policy if policy is not None else ScopePolicy()
        self._shell = shell if shell is not None else ShellRunner()
        # V03 tool plane: the startup inventory runs HERE, before any
        # turn, so the model context can only ever advertise
        # capabilities backed by installed tools (docs/CHANGES_v2.md
        # milestone 3). Probing is deterministic and failure-tolerant
        # (a probe failure records version=None, never raises).
        self._inventory = inventory if inventory is not None else ToolInventory()
        self._inventory.run()
        self._router = router if router is not None else PhaseRouter()
        self._planner = planner if planner is not None else Planner()
        self._evaluator = evaluator
        self._owned_model_service = model_service is None
        self._model_service = (
            model_service
            if model_service is not None
            else ModelService(event_log=event_log, run_id=self._run_id)
        )
        self._registry = SkillRegistry()
        self._executor = (
            executor
            if executor is not None
            else Executor(
                budgets=budgets,
                run_id=self._run_id,
                event_log=event_log,
                registry=self._registry,
                router=self._router,
                planner=self._planner,
                policy=self._policy,
                evaluator=evaluator,
            )
        )
        self._failed_actions: list[FailedAction] = []
        self._turns = 0
        self._adapter_cache: dict[str, ModelAdapter] = {}

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------

    async def run(self) -> RunnerStatus:
        """Seed the environment, then run the investigate loop.

        The loop: check stop/budget/objectives, then one full turn
        (route -> plan -> compile context -> one model action ->
        execute -> persist -> evaluate). Terminates with a structured
        :class:`RunnerStatus`; the terminal reason is also recorded as
        a ``runner.terminated`` event.

        Returns:
            The structured outcome of the run.
        """
        await self._seed_environment()
        self._append(
            RUNNER_STARTED,
            {
                "run_id": self._run_id,
                "model_id": self._model_id,
                "profile_family": self._profile.family,
                "base_url": DEFAULT_MODEL_BASE_URL,
            },
        )
        while True:
            if self._stop_event.is_set():
                return self._terminate(RunnerStatus.STOPPED, "supervisor stop event set")
            if self._budgets.is_exhausted():
                return self._terminate(RunnerStatus.BUDGET_EXHAUSTED, "budget exhausted")
            if await self._objectives_complete():
                return self._terminate(RunnerStatus.COMPLETED, "all objectives completed")
            outcome = await self._one_turn()
            if outcome is not None:
                return outcome

    async def aclose(self) -> None:
        """Close runner-owned resources (the model service, if owned).

        The environment adapter is owned by the caller (the supervisor)
        and closed there; this closes only what the runner constructed.
        """
        if self._owned_model_service:
            await self._model_service.aclose()

    # ------------------------------------------------------------------
    # the investigate loop
    # ------------------------------------------------------------------

    async def _one_turn(self) -> RunnerStatus | None:
        """One full investigate iteration; None means continue.

        Returns:
            A terminal :class:`RunnerStatus` when this turn ended the
            run (DONE phase, budget, unexpected kernel error), else
            None to continue the loop.
        """
        self._turns += 1
        try:
            route = await self._router.route(self._graph)
        except Exception as exc:  # noqa: BLE001 - structured failure, rule #9
            return self._record_turn_failure("route", exc)

        if route.phase is Phase.DONE:
            # The router's DONE: an accepted submission (HalCTF path)
            # or all objectives completed. Mark any stragglers complete
            # so the graph agrees with the terminal signal.
            await self._complete_objectives()
            return self._terminate(
                RunnerStatus.COMPLETED,
                f"router reached DONE via {route.predicate}",
            )

        plan_decision = await self._planner.plan(self._graph, route)
        plan_id = plan_decision.id if isinstance(plan_decision, Plan) else None

        compiled = await self._compile_context(route)
        if compiled is None:
            # Context compilation failed loudly; the failure is already
            # recorded. Continue — the budget bounds the loop.
            return None

        parsed = await self._propose_action(compiled, route.phase)
        if parsed is None:
            return None

        if parsed.kind != "run":
            # The model reasoned or proposed a privileged/unknown kind.
            # Privileged kinds (submit/hint/exit) are supervisor-owned
            # (AGENTS.md rule #5) and are NEVER executed by the runner.
            self._append(
                RUNNER_TURN,
                {
                    "phase": route.phase.value,
                    "predicate": route.predicate,
                    "plan_id": plan_id,
                    "action_kind": parsed.kind,
                    "rationale": _bounded(parsed.rationale or "", 500),
                    "executed": False,
                    "reason": (
                        "privileged or non-action kind; supervisor-owned, not executed"
                        if parsed.kind not in ("think",)
                        else "reasoning only"
                    ),
                },
            )
            return None

        proposed_skill = self._proposed_skill(route, plan_decision)
        if proposed_skill is None:
            self._append(
                RUNNER_TURN,
                {
                    "phase": route.phase.value,
                    "predicate": route.predicate,
                    "plan_id": plan_id,
                    "action_kind": parsed.kind,
                    "executed": False,
                    "reason": "no skill available for the routed phase; turn skipped",
                },
            )
            return None
        if not (parsed.payload or "").strip():
            self._append(
                RUNNER_MODEL_FAILURE,
                {
                    "phase": route.phase.value,
                    "reason": "run action with an empty payload",
                },
            )
            return None

        turn = await self._executor_turn(route, parsed, proposed_skill, plan_id)
        if turn is None:
            return None
        if isinstance(turn, RunnerStatus):
            return turn

        executed = await self._execute_action(turn)
        if executed is None:
            return None
        if isinstance(executed, RunnerStatus):
            return executed
        await self._persist_execution(turn, executed)
        await self._evaluate(route, plan_id)
        return None

    async def _executor_turn(
        self,
        route: PhaseRoute,
        parsed: ParsedAction,
        proposed_skill: str,
        plan_id: str | None,
    ) -> ExecutorTurn | RunnerStatus | None:
        """Feed the parsed action through the executor's strict contract.

        Returns an :class:`ExecutorTurn` on success, a terminal
        :class:`RunnerStatus` on budget exhaustion or an unexpected
        error, or None when the refusal was recorded and the loop
        should continue.
        """
        model_output: dict[str, str] = {
            "action": parsed.payload or "",
            "skill_id": proposed_skill,
        }
        try:
            return await self._executor.turn(
                self._graph, model_output, failed_actions=self._failed_actions
            )
        except BudgetExceeded as exc:
            return self._terminate(RunnerStatus.BUDGET_EXHAUSTED, f"budget exhausted: {exc}")
        except (
            ExecutorError,
            ScopeViolationError,
            ValidationError,
        ) as exc:
            self._record_turn_failure("executor", exc, phase=route.phase.value, plan_id=plan_id)
            return None
        except Exception as exc:  # noqa: BLE001 - unexpected kernel error, rule #9
            return self._record_turn_failure(
                "executor", exc, phase=route.phase.value, plan_id=plan_id
            )

    async def _execute_action(self, turn: ExecutorTurn) -> RunnerStatus | ToolResult | None:
        """Gate and execute exactly one bounded action.

        The executor already validated the proposal; this SECOND gate
        (mirroring the worker/bootstrap pattern) is the pre-execution
        enforcement of AGENTS.md Security Boundaries before the shell
        runs anything. Returns None when the refusal was recorded and
        the loop should continue.
        """
        request = turn.action
        try:
            self._policy.check(request.action, phase=request.phase.value)
        except ScopeViolationError as exc:
            self._record_turn_failure(
                "policy", exc, phase=request.phase.value, plan_id=request.plan_id
            )
            self._failed_actions.append(
                FailedAction(
                    fingerprint=request.fingerprint,
                    reason="scope_violation",
                    plan_step_id=request.plan_step_id,
                )
            )
            return None
        try:
            return await self._shell.run(
                command=request.action,
                timeout_seconds=float(request.timeout_seconds),
                stdout_limit=request.output_limit,
                stderr_limit=request.output_limit,
                working_directory=self._config.state_dir,
            )
        except Exception as exc:  # noqa: BLE001 - structured failure, rule #9
            return self._record_turn_failure(
                "shell", exc, phase=request.phase.value, plan_id=request.plan_id
            )

    async def _persist_execution(self, turn: ExecutorTurn, result: ToolResult) -> None:
        """Persist raw output, then update the graph (evidence chain).

        Order per AGENTS.md rule #1 — the RAW-first invariant
        (docs/CHANGES_v2.md milestone 4): the raw output lands in the
        artifact store FIRST, then the semantic parser selected by the
        producing command (:func:`ozzgraph.observations.parser_for_command`)
        normalizes it into a typed observation (source/kind/data per
        tool: nmap hosts+ports, nuclei findings, trivy vulnerabilities,
        ...; the generic shell text parser is the fallback), and the
        observation references the stored artifact. The observation
        entity (referencing the producing ``action`` entity) and an
        ``evidence`` entity (referencing the observation) follow —
        satisfying the data invariants \"every Observation references an
        Action\" and \"every Evidence references an Observation or
        artifact\". Every mutation is mirrored as a ``graph.*`` event
        with the same timestamp.
        """
        request = turn.action
        action_id = f"{ENTITY_ACTION}-{request.fingerprint}"
        truncated = (
            result.truncation_state.stdout_truncated or result.truncation_state.stderr_truncated
        )
        artifact = await self._artifacts.put(
            source=result.stdout.encode("utf-8", errors="replace"),
            source_action=action_id,
            target=request.hypothesis_id,
            truncated=truncated,
        )
        result.artifact_ids = [artifact.artifact_id]
        # Parse AFTER the raw output is persisted: the observation may
        # reference the artifact even when the parse itself fails
        # (malformed=True), so raw output never depends on parsing.
        observation = observation_for_result(result, artifact_id=artifact.artifact_id)
        summary = _bounded(observation.summary, _SUMMARY_LIMIT)
        at = datetime.now(UTC)

        observation_id = f"observation-{request.fingerprint}"
        evidence_id = f"evidence-{request.fingerprint}"
        if await self._graph.get_entity(observation_id) is None:
            await self._create_entity(
                observation_id,
                ENTITY_OBSERVATION,
                {
                    "action_id": action_id,
                    "source": observation.source,
                    "kind": observation.kind,
                    "summary": summary,
                    "data": observation.data,
                    "artifact_id": artifact.artifact_id,
                    "artifact_ids": observation.artifact_ids,
                    "exit_code": result.exit_code,
                    "ok": observation.ok,
                    "malformed": observation.malformed,
                    "parse_error": observation.parse_error,
                },
                at=at,
            )
            await self._create_edge(
                f"{action_id}-produced-{observation_id}",
                EDGE_ACTION_PRODUCED_OBSERVATION,
                action_id,
                observation_id,
                at=at,
            )
        if await self._graph.get_entity(evidence_id) is None:
            await self._create_entity(
                evidence_id,
                ENTITY_EVIDENCE,
                {
                    "note": summary,
                    "artifact_id": artifact.artifact_id,
                    "observation_id": observation_id,
                },
                at=at,
            )
            await self._create_edge(
                f"{evidence_id}-from-{observation_id}",
                EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
                evidence_id,
                observation_id,
                at=at,
            )

        # V02: form/link hypotheses from the new evidence (the
        # discover -> ... -> hypothesis step of the vertical slice).
        await self._update_hypotheses(turn, result, evidence_id, at)

        failed = result.exit_code != 0 or result.timeout_state
        if failed:
            self._failed_actions.append(
                FailedAction(
                    fingerprint=request.fingerprint,
                    reason=("timeout" if result.timeout_state else f"exit_code={result.exit_code}"),
                    plan_step_id=request.plan_step_id,
                )
            )
        self._append(
            RUNNER_ACTION_EXECUTED,
            {
                "phase": request.phase.value,
                "plan_id": request.plan_id,
                "plan_step_id": request.plan_step_id,
                "hypothesis_id": request.hypothesis_id,
                "action": _bounded(request.action, 256),
                "fingerprint": request.fingerprint,
                "exit_code": result.exit_code,
                "timeout": result.timeout_state,
                "duration": round(result.duration, 3),
                "artifact_id": artifact.artifact_id,
                "observation_id": observation_id,
                "evidence_id": evidence_id,
            },
        )

    async def _evaluate(self, route: PhaseRoute, plan_id: str | None) -> None:
        """Consult the evaluator when a plan exists (PR21).

        The evaluator's deterministic verdicts drive objective
        completion: a COMPLETE verdict marks every objective completed
        (the generic DONE path). No plan -> nothing to evaluate;
        evaluator failures are recorded and the loop continues.
        """
        if self._evaluator is None:
            return
        try:
            evaluation = await self._evaluator.decide_plan(self._graph)
        except NoPlanError:
            return
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            self._append(
                RUNNER_EVALUATED,
                {
                    "phase": route.phase.value,
                    "plan_id": plan_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return
        self._append(
            RUNNER_EVALUATED,
            {
                "plan_id": evaluation.plan_id,
                "verdict": evaluation.verdict.value,
                "reason": _bounded(evaluation.reason, 500),
            },
        )
        if evaluation.verdict is PlanVerdict.COMPLETE:
            await self._complete_objectives()
            # V02: a COMPLETE verdict means a ranked hypothesis was
            # validated — produce the evidence-backed Finding.
            await self._produce_findings(evaluation)

    # ------------------------------------------------------------------
    # hypotheses + findings (V02: discover -> ... -> validate -> Finding)
    # ------------------------------------------------------------------

    async def _update_hypotheses(
        self,
        turn: ExecutorTurn,
        result: ToolResult,
        evidence_id: str,
        at: datetime,
    ) -> None:
        """Form or link hypotheses from one new evidence entity.

        Two deterministic cases:

        - The turn served a plan step (``request.hypothesis_id`` set):
          the new evidence is linked to that hypothesis — ``EVIDENCE
          SUPPORTS HYPOTHESIS`` when the action succeeded, ``EVIDENCE
          CONTRADICTS HYPOTHESIS`` when it failed. The evaluator reads
          exactly these edges to confirm/refute hypotheses and complete
          plan steps (its \"new supporting evidence\" signal).
        - No plan step (pre-branching graph): a successful observation
          FORMS a new hypothesis — a deterministic ``hypothesis``
          entity derived from the action's fingerprint, stamped with a
          deterministic confidence (boosted when the output matched the
          configured flag pattern) and linked to the new evidence.

        Idempotent: ids derive from fingerprints/evidence ids, so
        re-persistence writes nothing new.
        """
        request = turn.action
        ok = result.exit_code == 0 and not result.timeout_state
        edge_type = (
            EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS if ok else EDGE_EVIDENCE_CONTRADICTS_HYPOTHESIS
        )
        if request.hypothesis_id is not None:
            hypothesis_id = request.hypothesis_id
            direction = "supports" if ok else "contradicts"
            edge_id = f"{evidence_id}-{direction}-{hypothesis_id}"
            if await self._graph.get_edge(edge_id) is None:
                await self._create_edge(edge_id, edge_type, evidence_id, hypothesis_id, at=at)
            return

        if not ok:
            # A failed probe forms no hypothesis — there is nothing to
            # claim yet (AGENTS.md rule #3).
            return
        hypothesis_id = f"hypothesis-{request.fingerprint[:12]}"
        if await self._graph.get_entity(hypothesis_id) is None:
            flag_matched = self._flag_matches(result)
            payload: dict[str, object] = {
                "confidence": (
                    HYPOTHESIS_FLAG_CONFIDENCE if flag_matched else HYPOTHESIS_CONFIDENCE
                ),
                "objective": _bounded(
                    f"{request.action} -> {_summarize(result)}",
                    300,
                ),
                "exploitation_direction": _bounded(request.action, 200),
            }
            if flag_matched:
                payload["cwe"] = DEFAULT_FINDING_CWE
            await self._create_entity(hypothesis_id, ENTITY_HYPOTHESIS, payload, at=at)
        edge_id = f"{evidence_id}-supports-{hypothesis_id}"
        if await self._graph.get_edge(edge_id) is None:
            await self._create_edge(
                edge_id,
                EDGE_EVIDENCE_SUPPORTS_HYPOTHESIS,
                evidence_id,
                hypothesis_id,
                at=at,
            )

    def _flag_matches(self, result: ToolResult) -> bool:
        """True when the raw action output matched the flag pattern.

        The deterministic sensitive-data signal: the configured
        ``flag_pattern`` (the same pattern the flag candidate extractor
        scans with) appearing in the raw stdout/stderr. ``config``
        validates the pattern at load time, so a compile failure here
        is a defensive no-match.
        """
        try:
            pattern = re.compile(self._config.flag_pattern)
        except re.error:
            return False  # pragma: no cover - config validates at load
        haystack = (result.stdout or "") + "\n" + (result.stderr or "")
        return pattern.search(haystack) is not None

    async def _produce_findings(self, evaluation: PlanEvaluation) -> None:
        """Persist one evidence-backed Finding for a confirmed hypothesis.

        Called only on an evaluator COMPLETE verdict: the first
        hypothesis with a CONFIRMED verdict (new supporting evidence,
        no contradictions) becomes a ``finding`` graph entity carrying
        the CHANGES_v2 Findings model (CWE classification, affected
        assets, preconditions, evidence ids, reproduction, impact CIA,
        confidence), linked to the hypothesis it validates, mirrored as
        a ``graph.*`` event, and rendered to ``findings.json``.

        Idempotent: the finding id derives from the hypothesis id.
        """
        confirmed = next(
            (
                outcome
                for outcome in evaluation.hypothesis_outcomes
                if outcome.verdict is HypothesisVerdict.CONFIRMED
            ),
            None,
        )
        if confirmed is None:
            return
        hypothesis_id = confirmed.hypothesis_id
        finding_id = f"finding-{hypothesis_id}"
        if await self._graph.get_entity(finding_id) is not None:
            return

        confidence, cwe = await self._hypothesis_characteristics(hypothesis_id)
        targets = await self._graph.list_entities(ENTITY_TARGET)
        target_id = targets[0].id if targets else None
        exposed = cwe == DEFAULT_FINDING_CWE
        impact = ImpactCIA(**(_FINDING_EXPOSED_IMPACT if exposed else _FINDING_DEFAULT_IMPACT))
        finding = Finding(
            id=finding_id,
            cwe=cwe,
            affected_assets=tuple(record.id for record in targets),
            preconditions=("authorized assessment scope",),
            evidence_ids=confirmed.supporting_evidence,
            reproduction=await self._reproduction_steps(confirmed.supporting_evidence),
            impact=impact,
            confidence=confidence,
            hypothesis_id=hypothesis_id,
            target_id=target_id,
        )
        at = datetime.now(UTC)
        await self._create_entity(
            finding_id,
            ENTITY_FINDING,
            finding.model_dump(mode="json"),
            at=at,
        )
        await self._create_edge(
            f"{finding_id}-validates-{hypothesis_id}",
            EDGE_FINDING_VALIDATES_HYPOTHESIS,
            finding_id,
            hypothesis_id,
            at=at,
        )
        self._append(
            RUNNER_FINDING_CREATED,
            {
                "finding_id": finding_id,
                "cwe": finding.cwe,
                "hypothesis_id": hypothesis_id,
                "evidence_ids": list(finding.evidence_ids),
                "confidence": finding.confidence,
            },
        )
        FindingStore.for_run(self._config.state_dir).save(finding)

    async def _hypothesis_characteristics(self, hypothesis_id: str) -> tuple[float, str]:
        """The validated hypothesis's confidence and CWE classification.

        Reads the ``hypothesis`` entity payload deterministically: the
        confidence the runner stamped at formation, and the CWE the
        runner recorded when the evidence matched the flag pattern
        (else the conservative default). A missing or foreign-typed
        payload field falls back to the conservative defaults — the
        finding must never fail because a payload is sparse.
        """
        record = await self._graph.get_entity(hypothesis_id)
        confidence = HYPOTHESIS_CONFIDENCE
        cwe = DEFAULT_FINDING_CWE
        if record is not None:
            raw_confidence = record.data.get("confidence")
            if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
                confidence = float(raw_confidence)
            raw_cwe = record.data.get("cwe")
            if isinstance(raw_cwe, str) and raw_cwe:
                cwe = raw_cwe
        return confidence, cwe

    async def _reproduction_steps(self, evidence_ids: tuple[str, ...]) -> str:
        """The bounded reproduction steps behind one evidence set.

        Walks the provenance chain the runner persisted (evidence ->
        observation -> action, via the payload ids) and joins the
        action commands — the deterministic steps that produced the
        supporting evidence. Bounded by :data:`REPRODUCTION_LIMIT`.
        """
        steps: list[str] = []
        for evidence_id in evidence_ids:
            record = await self._graph.get_entity(evidence_id)
            if record is None:
                continue
            observation_id = record.data.get("observation_id")
            if not isinstance(observation_id, str):
                continue
            observation = await self._graph.get_entity(observation_id)
            if observation is None:
                continue
            action_id = observation.data.get("action_id")
            if not isinstance(action_id, str):
                continue
            action = await self._graph.get_entity(action_id)
            if action is None:
                continue
            command = action.data.get("command")
            if isinstance(command, str) and command:
                steps.append(command)
        return _bounded("; ".join(steps), REPRODUCTION_LIMIT)

    # ------------------------------------------------------------------
    # context + model
    # ------------------------------------------------------------------

    async def _compile_context(self, route: PhaseRoute) -> CompiledContext | None:
        """Compile the bounded context for one turn (PR16).

        The mission layer is the environment's authorized scope plus
        its objectives — the immutable mission context (context layer
        1). Anchors are the seeded target entities; the phase sweep
        pulls the routed phase's tagged entities. The V03 tool plane
        bounds the advertisement: the compiled context lists ONLY the
        capabilities the startup inventory found installed, and only
        skills whose required capabilities are all available reach the
        model (docs/CHANGES_v2.md milestone 3).
        """
        try:
            scope = await self._environment.discover_scope()
            objectives = await self._environment.discover_objectives()
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            self._append(
                RUNNER_MODEL_FAILURE,
                {
                    "phase": route.phase.value,
                    "reason": f"environment discovery failed: {type(exc).__name__}: {exc}",
                },
            )
            return None
        capabilities = tuple(sorted(self._inventory.capabilities.available()))
        mission = self._mission_text(scope, objectives)
        target_records = await self._graph.list_entities(ENTITY_TARGET)
        target_ids = tuple(record.id for record in target_records)
        available_skill_ids = frozenset(
            summary.skill_id for summary in self._registry.list_available(capabilities)
        )
        skills = tuple(
            summary.skill_id for summary in route.skills if summary.skill_id in available_skill_ids
        )
        request = ContextRequest(
            mission=mission,
            target_ids=target_ids,
            phase=route.phase.value,
            transcript_tail=self._transcript_tail(),
            skills=skills,
            capabilities=capabilities,
            output_contract=OUTPUT_CONTRACT,
        )
        try:
            return await compile_context(self._graph, self._profile, request)
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            self._append(
                RUNNER_MODEL_FAILURE,
                {
                    "phase": route.phase.value,
                    "reason": f"context compilation failed: {type(exc).__name__}: {exc}",
                },
            )
            return None

    async def _propose_action(self, compiled: CompiledContext, phase: Phase) -> ParsedAction | None:
        """One bounded model completion parsed through the adapters.

        The model is called once per turn with the compiled context;
        the completion is probed and parsed with the adapter matching
        the profile's protocols (one repair attempt on parse failure,
        per the adapter failure policy). Failures are recorded as
        ``runner.model_failure`` events and return None (continue).
        """
        adapter = self._adapter_for_prompt()
        prompt = adapter.compile_prompt(
            mission=compiled.mission,
            graph_summary=compiled.graph_summary,
            transcript_tail=compiled.transcript_tail,
            skills=compiled.skills,
            output_contract=compiled.output_contract,
        )
        request = ModelRequest(
            model=self._model_id,
            messages=[ModelMessage(role="user", content=prompt)],
            temperature=self._profile.temperature,
            max_tokens=self._profile.output_token_limit,
        )
        try:
            response = await self._model_service.complete(request)
        except ModelServiceError as exc:
            self._append(
                RUNNER_MODEL_FAILURE,
                {
                    "phase": phase.value,
                    "reason": f"model call failed: {exc.message}",
                },
            )
            return None
        raw = response.choices[0].message.content or ""
        adapter = self._adapter_for(raw)
        try:
            return adapter.parse(raw)
        except AdapterParseError as parse_error:
            repaired = adapter.repair(raw, parse_error)
            if repaired is not None and repaired != raw:
                try:
                    return adapter.parse(repaired)
                except AdapterParseError:
                    pass
            self._append(
                RUNNER_MODEL_FAILURE,
                {
                    "phase": phase.value,
                    "reason": f"unparseable model output: {parse_error.detail}",
                },
            )
            return None

    def _adapter_for_prompt(self) -> ModelAdapter:
        """The prompt-compiling adapter: the profile's primary protocol.

        The completion protocol is only known AFTER the model call, so
        the prompt is compiled with the profile's most conservative
        declared protocol (``terminal`` when declared, else the first
        in sorted order) — the same protocol :meth:`_adapter_for` falls
        back to for an unprobeable completion.
        """
        protocol = self._fallback_protocol()
        cached = self._adapter_cache.get(protocol)
        if cached is not None:
            return cached
        try:
            adapter_type = adapter_for(protocol)
        except Exception:  # noqa: BLE001 - pragma: no cover, registry is static
            adapter_type = ADAPTERS["terminal"]
        instance = adapter_type(self._profile)
        self._adapter_cache[protocol] = instance
        return instance

    def _adapter_for(self, raw: str) -> ModelAdapter:
        """The adapter for one model completion.

        Deterministic protocol selection: the completion is probed
        (:func:`~ozzgraph.profiles.probe_protocol`) and the probed
        protocol is used when the profile declares it; otherwise the
        profile's most conservative declared protocol (``terminal``
        when declared, else the first in sorted order) is used —
        mirroring the v1 probe-then-adapter convention. Instances are
        cached per protocol family.
        """
        probed = probe_protocol(raw)
        protocol = probed if probed in self._profile.protocols else self._fallback_protocol()
        cached = self._adapter_cache.get(protocol)
        if cached is not None:
            return cached
        try:
            adapter_type = adapter_for(protocol)
        except Exception:  # noqa: BLE001 - pragma: no cover, registry is static
            adapter_type = ADAPTERS["terminal"]
        instance = adapter_type(self._profile)
        self._adapter_cache[protocol] = instance
        return instance

    def _fallback_protocol(self) -> str:
        """The profile's most conservative declared protocol."""
        protocols = sorted(self._profile.protocols)
        if not protocols:  # pragma: no cover - profiles declare >= 1
            return "terminal"
        if "terminal" in protocols:
            return "terminal"
        return protocols[0]

    # ------------------------------------------------------------------
    # graph seeding + objectives
    # ------------------------------------------------------------------

    async def _seed_environment(self) -> None:
        """Seed the environment adapter's discoveries into the graph.

        Idempotent: entity ids are deterministic (target ids derive
        from addresses, objective ids from the environment), so a
        re-run of seeding writes nothing new. Every mutation is
        mirrored as a ``graph.*`` event with the same timestamp
        (replay reconstructs the identical graph hash).
        """
        try:
            scope = await self._environment.discover_scope()
            targets = await self._environment.discover_targets()
            objectives = await self._environment.discover_objectives()
            capabilities = await self._environment.discover_capabilities()
        except Exception as exc:
            raise RunnerStateError(
                f"environment discovery failed during seeding: {type(exc).__name__}: {exc}"
            ) from exc

        at = datetime.now(UTC)
        run_id = f"run-{self._run_id}"
        if await self._graph.get_entity(run_id) is None:
            await self._create_entity(
                run_id,
                ENTITY_RUN,
                {"environment": scope.name, "model_id": self._model_id},
                at=at,
            )
        if await self._graph.get_entity("scope-1") is None:
            await self._create_entity(
                "scope-1",
                ENTITY_SCOPE,
                {
                    "name": scope.name,
                    "hosts": list(scope.hosts),
                    "urls": list(scope.urls),
                    "networks": list(scope.networks),
                    "credentials": list(scope.credentials),
                    "capabilities": sorted(capabilities),
                },
                at=at,
            )
        for target in targets:
            if await self._graph.get_entity(target.id) is None:
                await self._create_entity(
                    target.id,
                    ENTITY_TARGET,
                    {
                        "address": target.address,
                        "type": target.type,
                        "confirmed": False,
                        "metadata": target.metadata,
                    },
                    at=at,
                )
        for objective in objectives:
            if await self._graph.get_entity(objective.id) is None:
                await self._create_entity(
                    objective.id,
                    ENTITY_OBJECTIVE,
                    {
                        "description": objective.description,
                        "success_hint": objective.success_hint,
                        "completed": False,
                    },
                    at=at,
                )

    async def _objectives_complete(self) -> bool:
        """True when every seeded objective entity is completed.

        Authoritative state is the graph (AGENTS.md rule #1); a graph
        with no objectives is never "complete" (a run without
        objectives is not a completed run).
        """
        objectives = await self._graph.list_entities(ENTITY_OBJECTIVE)
        if not objectives:
            return False
        return all(_payload_bool(record, FIELD_COMPLETED) for record in objectives)

    async def _complete_objectives(self) -> None:
        """Mark every objective completed (deterministic DONE path).

        Called only on a deterministic terminal signal (accepted
        submission routed DONE, or an evaluator COMPLETE verdict) —
        never because a model claimed completion.
        """
        at = datetime.now(UTC)
        for record in await self._graph.list_entities(ENTITY_OBJECTIVE):
            if _payload_bool(record, FIELD_COMPLETED):
                continue
            payload = dict(record.data)
            payload[FIELD_COMPLETED] = True
            payload["completed_at"] = at.isoformat()
            await self._graph.update_entity(record.id, payload, at=at)
            if self._event_log is not None:
                self._event_log.append(
                    graph_event(
                        GRAPH_ENTITY_UPDATED,
                        self._run_id,
                        RUNNER_PRODUCER,
                        GraphEntityUpdated(
                            entity_id=record.id,
                            data=payload,
                            at=at,
                        ),
                    )
                )
            self._append(
                RUNNER_OBJECTIVE_COMPLETED,
                {"objective_id": record.id},
            )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _mission_text(self, scope: Scope, objectives: Sequence[Objective]) -> str:
        """The bounded immutable mission layer for model context.

        Scope surface and objectives only: capability advertisement is
        the V03 tool plane's job (``compile_context`` renders the
        ``AVAILABLE CAPABILITIES`` block from the inventory), so the
        mission never lists a capability that no installed tool backs.
        """
        lines = [
            f"Authorized assessment scope: {scope.name}",
        ]
        if scope.hosts:
            lines.append(f"Hosts: {', '.join(scope.hosts)}")
        if scope.urls:
            lines.append(f"URLs: {', '.join(scope.urls)}")
        if scope.networks:
            lines.append(f"Networks: {', '.join(scope.networks)}")
        if scope.credentials:
            lines.append(f"Credential references: {', '.join(scope.credentials)}")
        lines.append("Objectives:")
        for objective in objectives:
            lines.append(f"- {objective.id}: {objective.description}")
        return _bounded("\n".join(lines), _MISSION_LIMIT)

    def _transcript_tail(self) -> str:
        """A bounded tail of the last executed actions (context layer 4).

        V01 keeps this deliberately small: the most recent executed
        action's summary per turn. The full transcript lives in the
        event log, never in model context (AGENTS.md rule #2).
        """
        return ""  # V01: the graph projection already carries the state

    def _proposed_skill(
        self, route: PhaseRoute, plan_decision: Plan | NoPlanDecision
    ) -> str | None:
        """The deterministic skill the executor will bind this turn.

        With a plan, the executor binds the first plan step with no
        failed attempt — mirror that rule so the proposal can never be
        rejected as a plan-step mismatch. Without a plan, the first
        advertised skill in registry order.
        """
        if isinstance(plan_decision, Plan):
            failed_step_ids = frozenset(
                failed.plan_step_id
                for failed in self._failed_actions
                if failed.plan_step_id is not None
            )
            for step in plan_decision.steps:
                if step.id not in failed_step_ids:
                    return step.skill_id
            return None  # every step failed -> the executor raises PlanExhaustedError
        if route.skills:
            return route.skills[0].skill_id
        return None

    def _terminate(self, status: RunnerStatus, reason: str) -> RunnerStatus:
        """Record the structured terminal event and return ``status``."""
        self._append(
            RUNNER_TERMINATED,
            RunnerStatusEvent(
                status=status.value,
                turns=self._turns,
                model_calls=self._budgets.model_calls_used(),
                tool_calls=self._budgets.tool_calls_used(),
                reason=reason,
            ).model_dump(),
        )
        return status

    def _record_turn_failure(
        self,
        stage: str,
        exc: BaseException,
        *,
        phase: str | None = None,
        plan_id: str | None = None,
    ) -> RunnerStatus | None:
        """Record a loud, structured failure for one turn stage.

        Returns a terminal :class:`RunnerStatus.FAILED` for unexpected
        kernel errors (fail loudly, AGENTS.md rule #9) and None for the
        well-understood failures the loop continues past.
        """
        self._append(
            RUNNER_ACTION_FAILED,
            {
                "stage": stage,
                "phase": phase,
                "plan_id": plan_id,
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        if isinstance(exc, (ExecutorError, ScopeViolationError, ValidationError)):
            return None
        return RunnerStatus.FAILED

    def _append(self, event_type: str, payload: dict[str, object]) -> None:
        """Append one runner event when an event log is configured."""
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=event_type,
                producer=RUNNER_PRODUCER,
                payload=payload,
            )
        )

    async def _create_entity(
        self, entity_id: str, entity_type: str, data: dict[str, object], *, at: datetime
    ) -> None:
        """Create one entity and mirror the mutation to the event log."""
        await self._graph.create_entity(entity_id, entity_type, data, at=at)
        self._event_log.append(
            graph_event(
                GRAPH_ENTITY_CREATED,
                self._run_id,
                RUNNER_PRODUCER,
                GraphEntityCreated(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    data=data,
                    at=at,
                ),
            )
        )

    async def _create_edge(
        self, edge_id: str, edge_type: str, src_id: str, dst_id: str, *, at: datetime
    ) -> None:
        """Create one edge and mirror the mutation to the event log."""
        await self._graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
        self._event_log.append(
            graph_event(
                GRAPH_EDGE_CREATED,
                self._run_id,
                RUNNER_PRODUCER,
                GraphEdgeCreated(
                    edge_id=edge_id,
                    edge_type=edge_type,
                    src_id=src_id,
                    dst_id=dst_id,
                    at=at,
                ),
            )
        )


def _payload_bool(record: EntityRecord, key: str) -> bool:
    """Read a strict-boolean payload field, defaulting to False.

    Mirrors :func:`ozzgraph.router._payload_bool`: a present non-bool
    value is invalid graph state and fails loudly (AGENTS.md rule #9).
    """
    value = record.data.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RunnerStateError(
            f"entity {record.id!r} payload field {key!r} must be a bool, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def _bounded(text: str, limit: int) -> str:
    """Deterministic truncation for summaries and error messages."""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _summarize(result: ToolResult) -> str:
    """A compact, bounded summary of one tool result.

    Derives from the exit code, timeout state, duration, and the first
    non-empty output line — never free-form prose (AGENTS.md rule #3).
    """
    detail = f"exit={result.exit_code}"
    if result.timeout_state:
        detail = "timeout"
    output = (result.stdout or result.stderr or "").strip().splitlines()
    sample = _bounded(output[0], 160) if output else "(no output)"
    return f"{detail} duration={result.duration:.2f}s sample={sample}"
