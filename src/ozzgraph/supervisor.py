"""Supervisor kernel (PR2/PR3, V01 generic runtime).

The supervisor owns startup, identity output, runtime-directory
initialization, heartbeat emission, budget enforcement, signal handling, and
clean termination with a structured reason. It must not contain
challenge-category logic (AGENTS.md architecture rule 10).

PR3 turns :meth:`Supervisor.run` into an asyncio loop that emits heartbeats,
enforces budgets, and terminates gracefully on ``SIGTERM``/``SIGINT``. PR4 adds
append-only structured event logging (bootstrap and termination events). PR12
runs the deterministic bootstrap reconnaissance
(:mod:`ozzgraph.bootstrap`) after heartbeat setup and before the main loop,
constructing the supervisor-owned privileged HalClient for it. PR22 adds
:meth:`Supervisor.submit_verified_candidate` — the supervisor-only
submission surface that drives
:class:`~ozzgraph.submissions.SubmissionCoordinator` with a privileged
client. PR23 adds :meth:`Supervisor.request_paid_hint` — the
supervisor-only paid-hint surface that drives
:class:`~ozzgraph.hints.HintCoordinator` with a privileged client.

V01 (docs/adr/0008): :meth:`Supervisor.run` drives the v2 "most important
fix" (docs/CHANGES_v2.md) — instead of the idle
``while ...: await asyncio.sleep(0.25)`` poll, the supervisor builds the
runtime :class:`~ozzgraph.environments.base.EnvironmentAdapter` and hands
it to the :class:`~ozzgraph.runner.AutonomousRunner`, which runs the real
investigate loop (route -> plan -> context -> one model action -> execute
-> persist -> evaluate) until the objectives complete, a budget is
exhausted, or a signal stops the run. Environment selection is
deterministic: HalCTF when ``OZZGRAPH_CHALLENGE_ID`` is configured
(bootstrap stays wired for HalCTF), else the local environment, whose
discovered scope/targets/objectives are printed as the local-mode
bootstrap summary. All supervisor-owned privileged surfaces (bootstrap
client, submission, paid hints) are unchanged.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.bootstrap import BootstrapRunner
from ozzgraph.budgets import Budgets
from ozzgraph.config import ConfigError, OzzGraphConfig
from ozzgraph.environments import (
    EnvironmentAdapter,
    HalCTFEnvironment,
    LocalEnvironment,
)
from ozzgraph.evaluator import Evaluator
from ozzgraph.events import BOOTSTRAP, TERMINATION, Event, EventLog
from ozzgraph.hal_client import HalClient, HintResult, SubmissionResult
from ozzgraph.halctl import CHALLENGE_ID_ENV
from ozzgraph.heartbeat import Heartbeat
from ozzgraph.hints import HintClient, HintCoordinator
from ozzgraph.policy import ScopePolicy
from ozzgraph.runner import AutonomousRunner, RunnerStatus
from ozzgraph.state_graph import StateGraph
from ozzgraph.submissions import SubmissionClient, SubmissionCoordinator


class TerminationReason(str, Enum):
    """Structured reason for a supervisor termination."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class Supervisor:
    """Owns startup, heartbeat, budgets, signal handling, and clean shutdown.

    Args:
        config: Validated runtime configuration.
    """

    def __init__(self, config: OzzGraphConfig) -> None:
        self._config = config
        self._run_id = uuid4().hex
        self._started = False
        self._budgets: Budgets | None = None
        self._event_log: EventLog | None = None
        self._artifact_store: ArtifactStore | None = None

    @property
    def config(self) -> OzzGraphConfig:
        """The validated configuration this supervisor runs with."""
        return self._config

    @property
    def run_id(self) -> str:
        """Unique identifier for this run, minted once at construction."""
        return self._run_id

    def budgets(self) -> Budgets:
        """The active budget tracker, after :meth:`run` has started."""
        if self._budgets is None:
            raise RuntimeError("budgets not initialized; run() not started")
        return self._budgets

    @property
    def artifact_store(self) -> ArtifactStore:
        """The run's artifact store, after :meth:`start` has run."""
        if self._artifact_store is None:
            raise RuntimeError("artifact store not initialized; start() not called")
        return self._artifact_store

    def start(self) -> None:
        """Print identity immediately, then initialize runtime directories.

        The identity line must be the first output of the process so the
        competition platform can attribute the run (TECHNICAL_REQUIREMENTS).
        Directory creation is idempotent. Once the directories exist, a
        ``bootstrap`` event is appended to ``state_dir/actions.jsonl``
        and the run's artifact store is created at ``state_dir/artifacts``.
        """
        print(f"USER ID: {self._config.hal_user_id}", flush=True)
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._config.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._event_log = EventLog.for_run(self._config.state_dir)
        self._artifact_store = ArtifactStore.for_run(self._config.state_dir)
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=BOOTSTRAP,
                producer="supervisor",
                payload={
                    "hal_user_id": self._config.hal_user_id,
                    "state_dir": str(self._config.state_dir),
                    "artifact_dir": str(self._config.artifact_dir),
                    "budget": {
                        "max_tokens": self._config.max_tokens,
                        "max_model_calls": self._config.max_model_calls,
                        "max_tool_calls": self._config.max_tool_calls,
                        "max_workers": self._config.max_workers,
                        "max_hints": self._config.max_hints,
                        "max_runtime_s": self._config.max_runtime_s,
                    },
                },
            )
        )
        self._started = True

    async def run(self) -> TerminationReason:
        """Run the supervisor until a terminal condition.

        Installs ``SIGTERM``/``SIGINT`` handlers, starts the heartbeat,
        runs deterministic bootstrap, then drives the
        :class:`~ozzgraph.runner.AutonomousRunner` investigate loop
        (V01, docs/adr/0008) until it returns a structured
        :class:`~ozzgraph.runner.RunnerStatus`, which is mapped to the
        corresponding :class:`TerminationReason` (COMPLETED, INTERRUPTED
        on a signal stop, FAILED on a loud kernel failure, or
        BUDGET_EXHAUSTED).

        Signal handlers are installed before :meth:`start` so a signal that
        arrives immediately after the identity line is still caught gracefully
        rather than killing the process with the default disposition.

        Returns:
            The structured reason for termination.
        """
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_signal() -> None:
            stop_event.set()

        installed_signals: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal)
                installed_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                # Signal handling requires a running main-thread loop.
                pass

        self.start()
        cfg = self._config
        budgets = Budgets(
            max_tokens=cfg.max_tokens,
            max_model_calls=cfg.max_model_calls,
            max_tool_calls=cfg.max_tool_calls,
            max_workers=cfg.max_workers,
            max_hints=cfg.max_hints,
            max_runtime_s=float(cfg.max_runtime_s),
        )
        self._budgets = budgets
        heartbeat = Heartbeat(
            float(cfg.heartbeat_interval_s),
            summary=lambda: f"runtime_left={budgets.remaining_runtime():.0f}s",
        )

        heartbeat_task = asyncio.create_task(heartbeat.run())
        try:
            bootstrap_reason = await self._run_bootstrap()
            if bootstrap_reason is not None:
                return bootstrap_reason
            environment = self._make_environment()
            try:
                await self._print_local_scope(environment)
                assert self._event_log is not None  # start() set it
                assert self._artifact_store is not None  # start() set it
                policy = ScopePolicy(
                    max_command_length=cfg.max_command_length,
                    target_allowlist=cfg.target_allowlist,
                    allowed_command_families=cfg.allowed_command_families,
                )
                async with StateGraph(cfg.state_dir / "graph.db") as graph:
                    # V02: the evaluator is wired into the loop so a
                    # validated hypothesis (COMPLETE verdict) completes
                    # the objectives and produces a Finding — without
                    # it, a default run ends on budget exhaustion
                    # (docs/adr/0008, "Harder"). Deterministic-only:
                    # no model fallback client, no extra model calls.
                    evaluator = Evaluator(run_id=self._run_id, event_log=self._event_log)
                    runner = AutonomousRunner(
                        config=cfg,
                        graph=graph,
                        event_log=self._event_log,
                        artifacts=self._artifact_store,
                        budgets=budgets,
                        environment=environment,
                        stop_event=stop_event,
                        run_id=self._run_id,
                        policy=policy,
                        evaluator=evaluator,
                    )
                    try:
                        status = await runner.run()
                    finally:
                        await runner.aclose()
                return self.stop(reason=self._map_status(status))
            finally:
                await environment.aclose()
        finally:
            heartbeat.stop()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            for sig in installed_signals:
                loop.remove_signal_handler(sig)

    def _make_environment(self) -> EnvironmentAdapter:
        """Build the runtime environment adapter for this run.

        Deterministic selection (docs/adr/0008): the HalCTF environment
        is used when ``OZZGRAPH_CHALLENGE_ID`` is configured (bootstrap
        stays wired for HalCTF); otherwise the run is a local
        assessment and the :class:`~ozzgraph.environments.local.LocalEnvironment`
        is used.
        """
        if os.environ.get(CHALLENGE_ID_ENV, "").strip():
            return HalCTFEnvironment(self._config)
        return LocalEnvironment(self._config)

    async def _print_local_scope(self, environment: EnvironmentAdapter) -> None:
        """Print the local-mode bootstrap summary (scope/objectives).

        The v1 HalCTF bootstrap prints challenge status through the
        privileged client; the local environment has no such surface, so
        its deterministic bootstrap summary IS the discovered scope,
        targets, objectives, and capabilities — printed before the
        investigate loop starts so the operator sees what the run is
        authorized to assess. HalCTF mode prints nothing here (the
        HalCTF bootstrap already owns its output).
        """
        if not isinstance(environment, LocalEnvironment):
            return
        scope = await environment.discover_scope()
        targets = await environment.discover_targets()
        objectives = await environment.discover_objectives()
        capabilities = await environment.discover_capabilities()
        print(f"SCOPE: {scope.name}", flush=True)
        if scope.hosts:
            print(f"  hosts: {', '.join(scope.hosts)}", flush=True)
        if scope.urls:
            print(f"  urls: {', '.join(scope.urls)}", flush=True)
        if scope.networks:
            print(f"  networks: {', '.join(scope.networks)}", flush=True)
        print("TARGETS:", flush=True)
        for target in targets:
            print(f"  - {target.id} ({target.type}) {target.address}", flush=True)
        if not targets:
            print("  (none)", flush=True)
        print("OBJECTIVES:", flush=True)
        for objective in objectives:
            print(f"  - {objective.id}: {objective.description}", flush=True)
        print(f"CAPABILITIES: {', '.join(sorted(capabilities))}", flush=True)

    @staticmethod
    def _map_status(status: RunnerStatus) -> TerminationReason:
        """Map the runner's structured status to a termination reason."""
        if status is RunnerStatus.COMPLETED:
            return TerminationReason.COMPLETED
        if status is RunnerStatus.STOPPED:
            return TerminationReason.INTERRUPTED
        if status is RunnerStatus.BUDGET_EXHAUSTED:
            return TerminationReason.BUDGET_EXHAUSTED
        return TerminationReason.FAILED

    async def _run_bootstrap(self) -> TerminationReason | None:
        """Run deterministic bootstrap reconnaissance before the main loop.

        The privileged HalClient is constructed here — the supervisor is
        the only component that may own one (AGENTS.md invariant 5) — and
        handed to the bootstrap runner for status retrieval, smoke-flag
        submission, and the free hint. A bootstrap configuration error
        (malformed target variables, unknown namespace, smoke flag
        without a challenge id) terminates the run with ``FAILED`` so the
        failure is structured and loud; Hal service failures are recorded
        as events by the runner and are not fatal.

        Returns:
            The termination reason when bootstrap aborted the run, or
            None when it completed and the main loop may start.
        """
        assert self._event_log is not None  # start() sets it before _started
        client = HalClient(privileged=True, event_log=self._event_log, run_id=self._run_id)
        try:
            runner = BootstrapRunner(
                config=self._config,
                run_id=self._run_id,
                event_log=self._event_log,
                client=client,
            )
            try:
                await runner.run()
            except ConfigError:
                return self.stop(reason=TerminationReason.FAILED)
        finally:
            await client.aclose()
        return None

    async def submit_verified_candidate(
        self,
        graph: StateGraph,
        challenge_id: str | None = None,
        *,
        client: SubmissionClient | None = None,
    ) -> SubmissionResult:
        """Submit the graph's verified flag candidate (supervisor-only).

        The minimal PR22 integration surface, following the repo
        convention that the executor/evaluator are NOT yet wired into
        the supervisor idle loop: this method exists so a future loop
        driver can submit without ever giving a worker or model a
        privileged client. The supervisor constructs the supervisor-owned
        privileged :class:`HalClient` (the ``_run_bootstrap`` pattern,
        AGENTS.md invariant 5) unless a client is injected (tests), and
        drives the :class:`~ozzgraph.submissions.SubmissionCoordinator`,
        which enforces the provenance, privilege, and attempt-budget
        invariants.

        Args:
            graph: The authoritative state graph holding the verified
                flag candidate.
            challenge_id: The challenge to submit to; defaults to the
                ``OZZGRAPH_CHALLENGE_ID`` environment variable.
            client: Optional submission client to drive (must be
                privileged); when ``None`` the supervisor constructs and
                closes its own privileged HalClient.

        Raises:
            ConfigError: If no challenge id is configured.
            ozzgraph.submissions.SubmissionError: For every refusal the
                coordinator raises (privilege, limits, rejection — see
                :class:`~ozzgraph.submissions.SubmissionCoordinator`).
            ozzgraph.router.MissingRequiredStateError: If the graph has
                no verified, provenance-backed flag candidate.

        Returns:
            The platform's accepted :class:`SubmissionResult`; the phase
            router's ``has_accepted_submission`` predicate then routes
            the graph to DONE.
        """
        if challenge_id is None:
            resolved = os.environ.get(CHALLENGE_ID_ENV, "")
            if resolved.strip() == "":
                raise ConfigError(f"missing challenge id for submission: set {CHALLENGE_ID_ENV}")
            challenge_id = resolved
        assert self._event_log is not None  # start() sets it before _started
        owned = client is None
        resolved_client = (
            client
            if client is not None
            else HalClient(privileged=True, event_log=self._event_log, run_id=self._run_id)
        )
        try:
            coordinator = SubmissionCoordinator(
                client=resolved_client,
                run_id=self._run_id,
                challenge_id=challenge_id,
                event_log=self._event_log,
                max_submissions=self._config.max_submissions,
            )
            return await coordinator.submit_verified_candidate(graph)
        finally:
            if owned:
                await resolved_client.aclose()

    async def request_paid_hint(
        self,
        graph: StateGraph,
        index: int,
        challenge_id: str | None = None,
        *,
        client: HintClient | None = None,
    ) -> HintResult:
        """Request one paid hint through the deterministic gate (supervisor-only).

        The PR23 integration surface, mirroring :meth:`submit_verified_candidate`:
        this method exists so a future loop driver can buy a paid hint
        without ever giving a worker or model a privileged client. The
        supervisor constructs the supervisor-owned privileged
        :class:`HalClient` (the ``_run_bootstrap`` pattern, AGENTS.md
        invariant 5) unless a client is injected (tests), and drives the
        :class:`~ozzgraph.hints.HintCoordinator`, which evaluates the
        deterministic paid-hint gate (budget, no recent information
        gain, exhausted low-cost actions, two evaluator
        recommendations, sufficient expected-value improvement — every
        rule fail-closed), then requests, persists, and records the
        purchase only when ALL conditions hold.

        Args:
            graph: The authoritative state graph the gate evaluates on
                and the purchase persists in.
            index: The paid hint index (>= 1). Hint zero is free and
                owned by bootstrap — the paid-hint surface never
                requests it.
            challenge_id: The challenge to buy the hint for; defaults to
                the ``OZZGRAPH_CHALLENGE_ID`` environment variable.
            client: Optional hint client to drive (must be privileged);
                when ``None`` the supervisor constructs and closes its
                own privileged HalClient.

        Raises:
            ValueError: If ``index`` is less than 1.
            ConfigError: If no challenge id is configured.
            ozzgraph.hints.HintError: For every refusal the coordinator
                raises (privilege, policy denial — see
                :class:`~ozzgraph.hints.HintCoordinator`).
            ozzgraph.hal_client.HalServiceError: If the platform call
                fails after bounded retries.

        Returns:
            The platform's paid :class:`HintResult`.
        """
        if index < 1:
            raise ValueError(
                f"paid hint index must be >= 1, got {index}; "
                "hint zero is free and requested by bootstrap"
            )
        if challenge_id is None:
            resolved = os.environ.get(CHALLENGE_ID_ENV, "")
            if resolved.strip() == "":
                raise ConfigError(f"missing challenge id for hint purchase: set {CHALLENGE_ID_ENV}")
            challenge_id = resolved
        assert self._event_log is not None  # start() sets it before _started
        owned = client is None
        resolved_client = (
            client
            if client is not None
            else HalClient(privileged=True, event_log=self._event_log, run_id=self._run_id)
        )
        try:
            coordinator = HintCoordinator(
                client=resolved_client,
                run_id=self._run_id,
                challenge_id=challenge_id,
                event_log=self._event_log,
                max_hints=self._config.max_hints,
            )
            return await coordinator.check_then_request(graph, index)
        finally:
            if owned:
                await resolved_client.aclose()

    def stop(self, reason: TerminationReason = TerminationReason.INTERRUPTED) -> TerminationReason:
        """Terminate cleanly with a structured reason.

        Once started, appends a ``termination`` event carrying the reason to
        the run log before clearing the started flag, so both ``run()``
        terminal paths (budget exhausted, interrupted) end with a structured
        termination record. Stopping before :meth:`start` writes no event.

        Args:
            reason: Why the run ended.

        Returns:
            The reason passed in, so callers can chain ``run()`` -> reason.
        """
        if not self._started:
            return reason
        assert self._event_log is not None  # start() sets it before _started
        self._event_log.append(
            Event(
                run_id=self._run_id,
                timestamp=datetime.now(UTC),
                event_type=TERMINATION,
                producer="supervisor",
                payload={"reason": reason.value},
            )
        )
        self._started = False
        return reason
