"""HAL-005 tests: flag extraction + submission wired into the active loop.

Covers the "last two arrows" (docs/adr/0011, docs/CHANGES_v2.md
milestone 9): ``src/ozzgraph/runner.py`` invokes the supervisor-owned
flag hook after every executed turn's persistence, and
``src/ozzgraph/supervisor.py`` owns the hook
(``FlagCandidateExtractor.extract`` -> ``submit_verified_candidate``
through the privileged sidecar transport) plus the COMPLETED-run
best-effort ``/done``.

The integration tests drive the REAL loop with the REAL supervisor hook
against a scripted plain-HTTP sidecar (the HAL-004 wire shape), proving:

- an observation containing a flag is extracted and submitted with ZERO
  LLM calls between seeing it and submitting it (exactly one model call
  for the observing turn, none for the extraction -> submission path),
- an accepted submission completes ``objective-halctf-flag`` and the run
  terminates COMPLETED,
- a platform rejection marks the candidate ``rejected: true`` and it is
  never re-submitted (the loop continues, never fatal),
- a transient platform failure leaves the candidate verified and the
  next turn's hook retries it,
- a COMPLETED run fires the sidecar ``/done`` best-effort.

Style mirrors tests/test_runner.py (loop harness) and
tests/test_sidecar.py (coordinator integration through the sidecar
transport).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import httpx
import pytest

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.environments import (
    EnvironmentAdapter,
    HalCTFEnvironment,
    Objective,
    Scope,
    Target,
)
from ozzgraph.environments.halctf import (
    FIELD_ATTEMPTS,
    FIELD_FLAG,
    FIELD_REJECTED,
    SIDECAR_DONE_EVENT,
    SIDECAR_DONE_FAILED_EVENT,
)
from ozzgraph.events import (
    FLAGS_CANDIDATE_FOUND,
    SUBMISSION_ACCEPTED,
    SUBMISSION_REJECTED,
    EventLog,
)
from ozzgraph.model_client import ModelClient, ModelService
from ozzgraph.policy import ScopePolicy
from ozzgraph.profiles import GPT_PROFILE
from ozzgraph.router import ENTITY_SUBMISSION
from ozzgraph.runner import (
    RUNNER_FLAG_PROCESSING_FAILED,
    RUNNER_TERMINATED,
    AutonomousRunner,
    RunnerStatus,
)
from ozzgraph.shell import ShellRunner, ToolResult, TruncationState
from ozzgraph.state_graph import StateGraph
from ozzgraph.supervisor import (
    SUPERVISOR_DONE_FAILED,
    SUPERVISOR_FLAG_SUBMISSION_FAILED,
    Supervisor,
)
from ozzgraph.toolplane import ToolInventory

RUN = "run-test-flag-loop"
CHALLENGE = "web-01"
FLAG = "flag{loop-accepted-1}"
COMMAND = "curl -sS --max-time 5 http://127.0.0.1:3000/admin"
#: A second distinct command for multi-turn loops — the executor's
#: fingerprint store rejects a re-proposed duplicate action, so a second
#: executed turn must propose a different command.
COMMAND_B = "curl -sS --max-time 5 http://127.0.0.1:3000/robots.txt"


class FakeEnvironment:
    """Deterministic environment: one target, one incomplete objective."""

    async def discover_scope(self) -> Scope:
        return Scope(name="fake", urls=("http://127.0.0.1:3000",))

    async def discover_targets(self) -> list[Target]:
        return [Target(id="target-fake-1", type="url", address="http://127.0.0.1:3000")]

    async def discover_objectives(self) -> list[Objective]:
        return [Objective(id="objective-fake-1", description="Complete the fake assessment")]

    async def discover_capabilities(self) -> set[str]:
        return {"http.request"}

    async def verdict_satisfies_objectives(self, graph: StateGraph) -> bool:
        # Local semantics: the evaluator COMPLETE verdict satisfies the
        # objective unconditionally (HAL-006 keeps this behavior).
        return True

    async def aclose(self) -> None:
        pass


class FakeShell(ShellRunner):
    """Deterministic shell double: canned ToolResults keyed by command."""

    def __init__(self, results: dict[str, ToolResult]) -> None:
        super().__init__()
        self._results = results

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult:
        try:
            return self._results[command]
        except KeyError:
            raise AssertionError(f"unexpected command: {command!r}") from None


def _config(tmp_path: Path, **overrides) -> OzzGraphConfig:
    base = {
        "hal_user_id": "user-42",
        "state_dir": tmp_path / "state",
        "artifact_dir": tmp_path / "state" / "artifacts",
        "target_allowlist": ("127.0.0.1",),
    }
    base.update(overrides)
    return OzzGraphConfig(**base)  # type: ignore[arg-type] - test helper


def _budgets(**overrides) -> Budgets:
    base = {
        "max_tokens": 0,
        "max_model_calls": 5,
        "max_tool_calls": 5,
        "max_workers": 4,
        "max_hints": 1,
        "max_runtime_s": 60.0,
    }
    base.update(overrides)
    return Budgets(**base)  # type: ignore[arg-type] - test helper


def _completion(content: str) -> dict[str, object]:
    """One normalized chat-completion response body."""
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "created": 1,
    }


def _transport(contents: list[str]) -> httpx.MockTransport:
    """A transport returning one completion per request (cycled)."""
    index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal index
        content = contents[index % len(contents)]
        index += 1
        return httpx.Response(200, json=_completion(content))

    return httpx.MockTransport(handler)


def _tool_result(stdout: str, *, command: str = COMMAND) -> ToolResult:
    """One successful canned action result carrying ``stdout``."""
    return ToolResult(
        action_id="a" * 32,
        command=command,
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration=0.01,
        timeout_state=False,
        truncation_state=TruncationState(),
    )


class _SidecarServer:
    """A real plain-HTTP sidecar stub (the HAL-004 wire shape: /submit + /done).

    Serves on an ephemeral loopback port; every ``POST /submit`` body is
    recorded in :attr:`submits` and answered with the configured verdict,
    every ``POST /done`` body in :attr:`dones``.
    """

    def __init__(
        self,
        *,
        verdict: str = "correct",
        points: int = 50,
        fail_first: bool = False,
    ) -> None:
        self._verdict = verdict
        self._points = points
        self._fail_first = fail_first
        self.submits: list[dict[str, object]] = []
        self.dones: list[dict[str, object]] = []
        self._server = _ThreadedServer(("127.0.0.1", 0), _sidecar_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class _ThreadedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _sidecar_handler(server: _SidecarServer) -> type[BaseHTTPRequestHandler]:
    """A handler recording /submit and /done POSTs against ``server``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            if self.path == "/submit":
                server.submits.append(payload)
                if server._fail_first and len(server.submits) == 1:
                    self._reply(404, {"error": {"message": "not found"}})
                    return
                accepted = server._verdict != "wrong"
                self._reply(
                    200,
                    {
                        "status": server._verdict,
                        "points_awarded": server._points,
                        "message": "ok" if accepted else "Nope",
                    },
                )
            elif self.path == "/done":
                server.dones.append(payload)
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def _reply(self, status: int, body: dict[str, object]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, msg: str, *args: object) -> None:
            return  # silence the stub's request logging

    return _Handler


def _halctf_supervisor(tmp_path: Path, sidecar_url: str) -> Supervisor:
    """A started supervisor whose environment is a HalCTF environment.

    The environment's injected environ carries the challenge id and the
    sidecar base URL, so the supervisor's flag hook drives the REAL
    sidecar transport against the scripted server.
    """
    config = _config(tmp_path, max_submissions=3)
    supervisor = Supervisor(config)
    supervisor.start()
    supervisor._environment = HalCTFEnvironment(  # type: ignore[attr-defined]
        config,
        environ={
            "HAL_CHALLENGE_ID": CHALLENGE,
            "OZZGRAPH_SIDECAR_BASE_URL": sidecar_url,
        },
    )
    return supervisor


def _runner(
    tmp_path: Path,
    graph: StateGraph,
    *,
    environment: EnvironmentAdapter | None = None,
    hook: Callable[[StateGraph], Awaitable[None]] | None = None,
    model_service: ModelClient | None = None,
    budgets: Budgets | None = None,
    shell: ShellRunner | FakeShell | None = None,
) -> AutonomousRunner:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    log = EventLog.for_run(state)
    budgets = budgets or _budgets()
    return AutonomousRunner(
        config=_config(tmp_path),
        graph=graph,
        event_log=log,
        artifacts=ArtifactStore(state / "artifacts"),
        budgets=budgets,
        environment=environment if environment is not None else FakeEnvironment(),
        run_id=RUN,
        model_id="test-model",
        profile=GPT_PROFILE,
        model_service=model_service,
        policy=ScopePolicy(target_allowlist=("127.0.0.1",)),
        shell=shell if shell is not None else ShellRunner(),
        # Hermetic tool plane: an empty search path finds no tools, so
        # no version probe ever spawns a subprocess (deterministic).
        inventory=ToolInventory(paths=()),
        flag_submitter=hook,
    )


def _read_events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "state" / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# runner wiring: the hook runs after every executed turn's persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_invokes_flag_hook_after_persistence(tmp_path: Path) -> None:
    """The runner calls the flag hook after every executed turn's persistence.

    The hook receives the graph AFTER the observation/evidence are
    durable (the recording hook asserts they exist), runs with ZERO LLM
    calls, and a hook success emits no failure event. The run proceeds
    to BUDGET_EXHAUSTED unchanged — the hook is non-intrusive.
    """
    canned = _tool_result(f"Welcome! Your flag: {FLAG}")
    seen: list[dict[str, int]] = []

    async def hook(graph: StateGraph) -> None:
        observations = await graph.list_entities("observation")
        evidence = await graph.list_entities("evidence")
        seen.append({"observations": len(observations), "evidence": len(evidence)})

    model = ModelService(
        transport=_transport([json.dumps({"kind": "run", "payload": COMMAND})]),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        runner = _runner(
            tmp_path,
            graph,
            hook=hook,
            model_service=model,
            budgets=budgets,
            shell=FakeShell({COMMAND: canned}),
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED

    assert seen == [{"observations": 1, "evidence": 1}]
    events = _read_events(tmp_path)
    assert not any(e["event_type"] == RUNNER_FLAG_PROCESSING_FAILED for e in events)
    terminated = [e for e in events if e["event_type"] == RUNNER_TERMINATED][-1]
    assert terminated["payload"]["model_calls"] == 1


@pytest.mark.asyncio
async def test_runner_records_hook_failure_loudly_and_continues(tmp_path: Path) -> None:
    """A raising flag hook is recorded loudly and never fails the loop."""
    canned = _tool_result("no flag here")

    async def hook(graph: StateGraph) -> None:
        raise RuntimeError("hook exploded")

    model = ModelService(
        transport=_transport([json.dumps({"kind": "run", "payload": COMMAND})]),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=1, max_tool_calls=1)
    async with StateGraph(":memory:") as graph:
        runner = _runner(
            tmp_path,
            graph,
            hook=hook,
            model_service=model,
            budgets=budgets,
            shell=FakeShell({COMMAND: canned}),
        )
        try:
            status = await runner.run()
        finally:
            await runner.aclose()
        assert status is RunnerStatus.BUDGET_EXHAUSTED  # not FAILED

    events = _read_events(tmp_path)
    failures = [e for e in events if e["event_type"] == RUNNER_FLAG_PROCESSING_FAILED]
    assert len(failures) == 1
    assert failures[0]["payload"]["error_type"] == "RuntimeError"
    assert "hook exploded" in failures[0]["payload"]["message"]


# ---------------------------------------------------------------------------
# the full loop: observation flag -> extractor -> accepted submission -> DONE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_extracts_and_submits_flag_with_zero_llm_calls(tmp_path: Path) -> None:
    """Observation flag -> extractor -> accepted submission -> objective completed.

    One model turn observes the flag; the supervisor-owned hook extracts
    the candidate and submits it through the REAL sidecar transport
    (scripted plain-HTTP server); the next iteration routes DONE, the
    objective completes, and the run terminates COMPLETED with exactly
    ONE model call — zero LLM calls between seeing the flag and
    submitting it (acceptance 2).
    """
    canned = _tool_result(f"Welcome! Your flag: {FLAG}")
    model = ModelService(
        transport=_transport([json.dumps({"kind": "run", "payload": COMMAND})]),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=2, max_tool_calls=2)
    sidecar = _SidecarServer(verdict="correct", points=50)
    with sidecar:
        supervisor = _halctf_supervisor(tmp_path, sidecar.base_url)
        async with StateGraph(":memory:") as graph:
            runner = _runner(
                tmp_path,
                graph,
                environment=supervisor._environment,  # type: ignore[attr-defined]
                hook=supervisor._submit_flag_candidates,
                model_service=model,
                budgets=budgets,
                shell=FakeShell({COMMAND: canned}),
            )
            try:
                status = await runner.run()
            finally:
                await runner.aclose()
            assert status is RunnerStatus.COMPLETED

            objective = await graph.get_entity("objective-halctf-flag")
            assert objective is not None
            assert objective.data["completed"] is True

            candidates = await graph.list_entities("flag_candidate")
            assert len(candidates) == 1
            assert candidates[0].data[FIELD_FLAG] == FLAG
            assert candidates[0].data[FIELD_REJECTED] is False

            submissions = await graph.list_entities(ENTITY_SUBMISSION)
            assert len(submissions) == 1
            assert submissions[0].data["accepted"] is True
            assert submissions[0].data["flag"] == FLAG
            assert submissions[0].data["challenge_id"] == CHALLENGE

    # The supervisor-owned sidecar transport carried exactly one /submit.
    assert sidecar.submits == [{"challenge_id": CHALLENGE, "flag": FLAG}]
    # /done is fired by supervisor.run() at COMPLETED, not by runner.run().
    assert sidecar.dones == []

    events = _read_events(tmp_path)
    types = [e["event_type"] for e in events]
    assert FLAGS_CANDIDATE_FOUND in types
    assert "submission.attempted" in types
    assert SUBMISSION_ACCEPTED in types
    assert "runner.objective_completed" in types
    assert RUNNER_FLAG_PROCESSING_FAILED not in types
    assert SUPERVISOR_FLAG_SUBMISSION_FAILED not in types
    terminated = [e for e in events if e["event_type"] == RUNNER_TERMINATED][-1]
    assert terminated["payload"]["status"] == "completed"
    assert terminated["payload"]["model_calls"] == 1


@pytest.mark.asyncio
async def test_rejected_flag_is_marked_and_never_resubmitted(tmp_path: Path) -> None:
    """A platform rejection marks the candidate; the loop keeps investigating.

    The sidecar rejects the flag; the coordinator marks the candidate
    ``rejected: true`` and increments its attempts (never re-submitted),
    the hook continues non-fatally, and the run proceeds under the
    budgets to BUDGET_EXHAUSTED — never FAILED.
    """
    rejected_flag = "flag{loop-rejected-1}"
    canned = _tool_result(f"Your flag: {rejected_flag}")
    canned_b = _tool_result("robots.txt: /admin", command=COMMAND_B)
    model = ModelService(
        transport=_transport(
            [
                json.dumps({"kind": "run", "payload": COMMAND}),
                json.dumps({"kind": "run", "payload": COMMAND_B}),
            ]
        ),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=3, max_tool_calls=3)
    sidecar = _SidecarServer(verdict="wrong", points=0)
    with sidecar:
        supervisor = _halctf_supervisor(tmp_path, sidecar.base_url)
        async with StateGraph(":memory:") as graph:
            runner = _runner(
                tmp_path,
                graph,
                environment=supervisor._environment,  # type: ignore[attr-defined]
                hook=supervisor._submit_flag_candidates,
                model_service=model,
                budgets=budgets,
                shell=FakeShell({COMMAND: canned, COMMAND_B: canned_b}),
            )
            try:
                status = await runner.run()
            finally:
                await runner.aclose()
            assert status is RunnerStatus.BUDGET_EXHAUSTED

            candidates = await graph.list_entities("flag_candidate")
            assert len(candidates) == 1
            assert candidates[0].data[FIELD_FLAG] == rejected_flag
            assert candidates[0].data[FIELD_REJECTED] is True
            assert candidates[0].data[FIELD_ATTEMPTS] == 1

            submissions = await graph.list_entities(ENTITY_SUBMISSION)
            assert len(submissions) == 1
            assert submissions[0].data["accepted"] is False

    # Exactly one wire attempt: the rejected flag is never re-submitted.
    assert sidecar.submits == [{"challenge_id": CHALLENGE, "flag": rejected_flag}]

    events = _read_events(tmp_path)
    types = [e["event_type"] for e in events]
    assert SUBMISSION_REJECTED in types
    assert RUNNER_FLAG_PROCESSING_FAILED not in types
    assert SUPERVISOR_FLAG_SUBMISSION_FAILED not in types
    assert "runner.terminated" in types


@pytest.mark.asyncio
async def test_transient_submission_failure_is_retried_next_turn(tmp_path: Path) -> None:
    """A terminal platform failure leaves the candidate verified and retried.

    The first /submit fails with a non-retryable 404 (HalServiceError,
    recorded as ``supervisor.flag_submission_failed``); the candidate
    stays verified, so the next turn's hook submits it again — accepted —
    and the run completes. The retry is bounded by the submission budget
    and the loop budgets.
    """
    canned = _tool_result(f"Your flag: {FLAG}")
    canned_b = _tool_result("robots.txt: /admin", command=COMMAND_B)
    model = ModelService(
        transport=_transport(
            [
                json.dumps({"kind": "run", "payload": COMMAND}),
                json.dumps({"kind": "run", "payload": COMMAND_B}),
            ]
        ),
        max_retries=0,
        event_log=EventLog.for_run(tmp_path / "state"),
        run_id=RUN,
    )
    budgets = _budgets(max_model_calls=3, max_tool_calls=3)
    sidecar = _SidecarServer(verdict="correct", points=50, fail_first=True)
    with sidecar:
        supervisor = _halctf_supervisor(tmp_path, sidecar.base_url)
        async with StateGraph(":memory:") as graph:
            runner = _runner(
                tmp_path,
                graph,
                environment=supervisor._environment,  # type: ignore[attr-defined]
                hook=supervisor._submit_flag_candidates,
                model_service=model,
                budgets=budgets,
                shell=FakeShell({COMMAND: canned, COMMAND_B: canned_b}),
            )
            try:
                status = await runner.run()
            finally:
                await runner.aclose()
            assert status is RunnerStatus.COMPLETED

            objective = await graph.get_entity("objective-halctf-flag")
            assert objective is not None
            assert objective.data["completed"] is True
            submissions = await graph.list_entities(ENTITY_SUBMISSION)
            assert len(submissions) == 1
            assert submissions[0].data["accepted"] is True

    assert len(sidecar.submits) == 2  # the failed 404 attempt + the accepted retry
    assert sidecar.submits[-1] == {"challenge_id": CHALLENGE, "flag": FLAG}

    events = _read_events(tmp_path)
    types = [e["event_type"] for e in events]
    assert SUBMISSION_ACCEPTED in types
    failed = [e for e in events if e["event_type"] == SUPERVISOR_FLAG_SUBMISSION_FAILED]
    assert len(failed) == 1
    assert failed[0]["payload"]["error_type"] == "HalServiceError"


# ---------------------------------------------------------------------------
# the supervisor hook: local mode and empty-graph no-ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_hook_is_noop_without_candidate(tmp_path: Path) -> None:
    """No flag in the graph -> no wire call, no events (never fatal)."""
    sidecar = _SidecarServer()
    with sidecar:
        supervisor = _halctf_supervisor(tmp_path, sidecar.base_url)
        async with StateGraph(":memory:") as graph:
            await supervisor._submit_flag_candidates(graph)  # type: ignore[attr-defined]

    assert sidecar.submits == []
    assert sidecar.dones == []
    events = _read_events(tmp_path)
    assert not any(e["event_type"] == SUPERVISOR_FLAG_SUBMISSION_FAILED for e in events)


@pytest.mark.asyncio
async def test_flag_hook_is_noop_in_local_mode(tmp_path: Path) -> None:
    """Local mode has no HalCTF submission surface: the hook is a no-op."""
    supervisor = Supervisor(_config(tmp_path))
    supervisor.start()
    async with StateGraph(":memory:") as graph:
        await supervisor._submit_flag_candidates(graph)  # type: ignore[attr-defined]
        await supervisor._notify_platform_done()  # type: ignore[attr-defined]

    events = _read_events(tmp_path)
    assert not any(
        e["event_type"] in (SUPERVISOR_FLAG_SUBMISSION_FAILED, SUPERVISOR_DONE_FAILED)
        for e in events
    )


# ---------------------------------------------------------------------------
# /done — best-effort, fires for a COMPLETED HalCTF run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_platform_done_fires_after_completed(tmp_path: Path) -> None:
    """A COMPLETED HalCTF run fires the sidecar /done once, with the run id."""
    sidecar = _SidecarServer()
    with sidecar:
        supervisor = _halctf_supervisor(tmp_path, sidecar.base_url)
        await supervisor._notify_platform_done(reason="completed")  # type: ignore[attr-defined]

    assert sidecar.dones == [{"run_id": supervisor.run_id, "reason": "completed"}]
    events = _read_events(tmp_path)
    assert SIDECAR_DONE_EVENT in [e["event_type"] for e in events]


@pytest.mark.asyncio
async def test_notify_platform_done_unreachable_is_best_effort(tmp_path: Path) -> None:
    """An unreachable sidecar never fails the run; done_failed is recorded."""
    supervisor = _halctf_supervisor(tmp_path, "http://127.0.0.1:1")
    await supervisor._notify_platform_done()  # type: ignore[attr-defined] - must not raise

    events = _read_events(tmp_path)
    assert SIDECAR_DONE_FAILED_EVENT in [e["event_type"] for e in events]


@pytest.mark.asyncio
async def test_notify_platform_done_construction_failure_is_recorded(tmp_path: Path) -> None:
    """A bad sidecar URL fails /done construction loudly, never fatally."""
    supervisor = _halctf_supervisor(tmp_path, "not-a-url")
    await supervisor._notify_platform_done()  # type: ignore[attr-defined] - must not raise

    events = _read_events(tmp_path)
    done_failed = [e for e in events if e["event_type"] == SUPERVISOR_DONE_FAILED]
    assert len(done_failed) == 1
    assert done_failed[0]["payload"]["error_type"] == "ValueError"
