"""Chaos tests for the OzzGraph harness (PR29).

Injects the failures docs/TESTING_AND_QA.md "Chaos Tests" catalogues —
model timeout, model server 500, MCP timeout, malformed MCP response,
process hang, worker crash, disk full, SQLite lock, partial artifact
write, heartbeat failure, and termination signals — and proves every
one fails loudly (structured error or event, never silent corruption,
never a hang).

Injection mechanics (per the acceptance criteria): monkeypatches and
fakes only. No network is touched — model/MCP failures ride
``httpx.MockTransport`` handlers; the only live endpoints ever used are
the loopback commands the bounded shell runner executes (``sleep``,
``echo``, marker files) and in-memory / temporary SQLite and JSONL
state.

The typed-failure assertions mirror the harness's contract: no bare
``sqlite3`` exception ever escapes :class:`~ozzgraph.state_graph.StateGraph`,
no bare ``OSError`` is silently swallowed by the artifact store or event
log, and every provider failure is a ``ModelServiceError`` /
``HalServiceError`` plus a ``model_failure`` / ``hal_failure`` event.
"""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ozzgraph.artifacts import ArtifactIndexError, ArtifactStore, ArtifactStoreError
from ozzgraph.config import OzzGraphConfig
from ozzgraph.events import (
    BOOTSTRAP,
    GRAPH_ENTITY_CREATED,
    TERMINATION,
    Event,
    EventLog,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.hal_client import HAL_FAILURE_EVENT, HalClient, HalServiceError
from ozzgraph.heartbeat import Heartbeat
from ozzgraph.model_client import (
    MODEL_FAILURE_EVENT,
    ModelMessage,
    ModelRequest,
    ModelService,
    ModelServiceError,
)
from ozzgraph.replay import ReplayMalformedEventError, replay_graph
from ozzgraph.scheduler import (
    Scheduler,
    Task,
    TaskDAG,
    TaskOutcome,
    WorkerRunStatus,
)
from ozzgraph.shell import ShellRunner
from ozzgraph.state_graph import StateGraph, StateGraphError
from ozzgraph.supervisor import Supervisor, TerminationReason


async def _no_sleep(_: float) -> None:
    """Deterministic backoff no-op for bounded-retry tests."""


def _model_request() -> ModelRequest:
    return ModelRequest(
        model="chaos-model",
        messages=[ModelMessage(role="user", content="hello")],
    )


def _event_log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


def _read_events(log: EventLog) -> list[dict[str, object]]:
    lines = log.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# model timeout / model server 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_timeout_surfaces_typed_error_and_event(
    tmp_path: Path,
) -> None:
    """A model transport timeout raises ModelServiceError and logs the failure."""
    log = _event_log(tmp_path)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("model server unreachable", request=request)

    service = ModelService(
        base_url="http://127.0.0.1:9/v1",
        max_retries=0,
        transport=httpx.MockTransport(_handler),
        sleeper=_no_sleep,
        event_log=log,
        run_id="run-1",
    )
    with pytest.raises(ModelServiceError) as exc:
        await service.complete(_model_request())
    await service.aclose()

    error = exc.value
    assert error.status_code is None
    assert error.retryable is True
    assert "transport failure" in error.message
    assert error.provider == "openai-compatible"

    failures = [e for e in _read_events(log) if e["event_type"] == MODEL_FAILURE_EVENT]
    assert len(failures) == 1
    assert failures[0]["producer"] == "model_client"
    assert failures[0]["payload"]["status"] is None
    assert failures[0]["payload"]["attempts"] == 1


@pytest.mark.asyncio
async def test_model_server_500_retries_bounded_then_raises(tmp_path: Path) -> None:
    """A 500 is retried with backoff, then raised as a typed error + event."""
    log = _event_log(tmp_path)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "overloaded"}})

    service = ModelService(
        base_url="http://127.0.0.1:9/v1",
        max_retries=1,
        transport=httpx.MockTransport(_handler),
        sleeper=_no_sleep,
        event_log=log,
        run_id="run-1",
    )
    with pytest.raises(ModelServiceError) as exc:
        await service.complete(_model_request())
    await service.aclose()

    error = exc.value
    assert error.status_code == 500
    assert error.retryable is True
    assert "HTTP 500" in error.message

    failures = [e for e in _read_events(log) if e["event_type"] == MODEL_FAILURE_EVENT]
    assert len(failures) == 1
    assert failures[0]["payload"]["attempts"] == 2  # 1 initial + 1 retry, then stop


# ---------------------------------------------------------------------------
# MCP timeout / malformed MCP response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_timeout_surfaces_typed_error_and_event(tmp_path: Path) -> None:
    """An MCP transport timeout raises HalServiceError and logs hal_failure."""
    log = _event_log(tmp_path)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("MCP server hung", request=request)

    client = HalClient(
        base_url="http://127.0.0.1:9/mcp",
        max_retries=0,
        transport=httpx.MockTransport(_handler),
        sleeper=_no_sleep,
        event_log=log,
        run_id="run-1",
    )
    with pytest.raises(HalServiceError) as exc:
        await client.get_status("ch-1")
    await client.aclose()

    error = exc.value
    assert error.status_code is None
    assert error.retryable is True
    assert "transport failure" in error.message

    failures = [e for e in _read_events(log) if e["event_type"] == HAL_FAILURE_EVENT]
    assert len(failures) == 1
    assert failures[0]["producer"] == "hal_client"
    assert failures[0]["payload"]["status"] is None


@pytest.mark.asyncio
async def test_mcp_malformed_response_fails_loudly(tmp_path: Path) -> None:
    """A non-JSON MCP body raises a non-retryable HalServiceError + event."""
    log = _event_log(tmp_path)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json-rpc at all</html>")

    client = HalClient(
        base_url="http://127.0.0.1:9/mcp",
        max_retries=0,
        transport=httpx.MockTransport(_handler),
        sleeper=_no_sleep,
        event_log=log,
        run_id="run-1",
    )
    with pytest.raises(HalServiceError) as exc:
        await client.get_status("ch-1")
    await client.aclose()

    error = exc.value
    assert error.status_code == 200
    assert error.retryable is False  # garbage is never retried
    assert "unparseable provider response" in error.message

    failures = [e for e in _read_events(log) if e["event_type"] == HAL_FAILURE_EVENT]
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_mcp_malformed_result_shape_fails_loudly(tmp_path: Path) -> None:
    """A JSON-RPC result of the wrong shape is a loud parse failure."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"not": "a status"}},
        )

    client = HalClient(
        base_url="http://127.0.0.1:9/mcp",
        max_retries=0,
        transport=httpx.MockTransport(_handler),
        sleeper=_no_sleep,
        event_log=_event_log(tmp_path),
        run_id="run-1",
    )
    with pytest.raises(HalServiceError, match="invalid challenge.status result"):
        await client.get_status("ch-1")
    await client.aclose()


# ---------------------------------------------------------------------------
# process hang: the bounded shell runner kills the process group, no orphan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_hang_killed_no_orphan_grandchild(tmp_path: Path) -> None:
    """A hanging process is killed; a background grandchild cannot outlive it."""
    marker = tmp_path / "marker"
    result = await ShellRunner().run(
        command=f"sh -c '(sleep 0.8; touch {marker}) & sleep 30'",
        timeout_seconds=0.2,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )

    assert result.timeout_state
    assert result.duration < 5
    await asyncio.sleep(1.5)
    assert not marker.exists()  # the whole process group died with the action


@pytest.mark.asyncio
async def test_runner_recovers_after_timeout(tmp_path: Path) -> None:
    """After a timeout kill the runner is reusable: the next run completes."""
    runner = ShellRunner()
    hung = await runner.run(
        command="sleep 30",
        timeout_seconds=0.2,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert hung.timeout_state

    healthy = await runner.run(
        command="echo recovered",
        timeout_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        working_directory=tmp_path,
    )
    assert healthy.exit_code == 0
    assert "recovered" in healthy.stdout
    assert not healthy.timeout_state


# ---------------------------------------------------------------------------
# worker crash: structured failure, the run continues, no hang
# ---------------------------------------------------------------------------


class _ChaosCrashRunner:
    """Crashes task ``a`` with an exception; succeeds everything else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_task(self, task: Task) -> TaskOutcome:
        self.calls.append(task.id)
        if task.id == "a":
            raise RuntimeError("worker crashed mid-run")
        return TaskOutcome(task_id=task.id, status=WorkerRunStatus.SUCCEEDED)


@pytest.mark.asyncio
async def test_worker_crash_becomes_structured_failure_and_run_continues() -> None:
    """A raising worker becomes a failed worker_run; the DAG still completes."""
    dag = TaskDAG([Task(id="a"), Task(id="b", depends_on=("a",))])
    runner = _ChaosCrashRunner()
    async with StateGraph(":memory:") as graph:
        result = await Scheduler(
            dag=dag,
            runner=runner,  # type: ignore[arg-type]
            max_workers=2,
            run_id="run-1",
        ).run(graph)

    assert result.failed == 1
    assert result.succeeded == 1
    crashed = next(run for run in result.worker_runs if run.task_id == "a")
    assert crashed.status is WorkerRunStatus.FAILED
    assert crashed.error is not None
    assert "worker crashed" in crashed.error
    # the dependent still ran and the scheduler returned: no hang, no silent swallow
    assert runner.calls == ["a", "b"]


# ---------------------------------------------------------------------------
# disk full (ENOSPC)
# ---------------------------------------------------------------------------


def test_artifact_store_disk_full_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    """An ENOSPC during artifact content write is a loud ArtifactStoreError."""
    store_root = tmp_path / "artifacts"
    store = ArtifactStore(store_root)
    real_write_bytes = Path.write_bytes

    def _enospc_write(path: Path, data: bytes) -> None:
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", _enospc_write)

    with pytest.raises(ArtifactStoreError, match="failed to write artifact content"):
        asyncio.run(store.put(source=b"payload bytes"))


def test_event_log_disk_full_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    """An ENOSPC on the event log propagates: no silent event loss."""
    log = _event_log(tmp_path)
    log.append(
        Event(run_id="run-1", timestamp=datetime.now(UTC), event_type="bootstrap", producer="x")
    )
    real_open = Path.open

    def _enospc_open(path: Path, *args, **kwargs):
        if path == log.path:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _enospc_open)
    with pytest.raises(OSError) as exc:
        log.append(
            Event(
                run_id="run-1", timestamp=datetime.now(UTC), event_type="termination", producer="x"
            )
        )
    assert exc.value.errno == errno.ENOSPC


# ---------------------------------------------------------------------------
# SQLite lock: the locked-DB error path wraps into StateGraphError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_lock_on_write_wrapped_as_state_graph_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A locked database surfaces as StateGraphError, never a bare sqlite error."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        conn = graph._connection()
        real_execute = conn.execute

        async def _locked_execute(sql: str, *args, **kwargs):
            if sql.lstrip().upper().startswith("INSERT"):
                raise sqlite3.OperationalError("database is locked")
            return await real_execute(sql, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", _locked_execute)

        with pytest.raises(StateGraphError) as exc:
            await graph.create_entity("e-1", "entity", {})
    assert "database is locked" in str(exc.value)
    assert not isinstance(exc.value, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_sqlite_lock_on_begin_wrapped_as_state_graph_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A lock on BEGIN surfaces as a loud StateGraphError (transaction path)."""
    async with StateGraph(tmp_path / "graph.db") as graph:
        conn = graph._connection()
        real_execute = conn.execute

        async def _locked_begin(sql: str, *args, **kwargs):
            if sql == "BEGIN":
                raise sqlite3.OperationalError("database is locked")
            return await real_execute(sql, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", _locked_begin)

        with pytest.raises(StateGraphError, match="could not begin transaction"):
            async with graph.transaction():
                await graph.create_entity("e-1", "entity", {})


# ---------------------------------------------------------------------------
# partial artifact write: truncated JSONL / corrupt index fail loudly
# ---------------------------------------------------------------------------


def test_truncated_event_log_fails_replay_loudly(tmp_path: Path) -> None:
    """A torn JSONL line aborts replay: no silent corruption, no skipping."""
    at = datetime.now(UTC)
    log = _event_log(tmp_path)
    log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            "run-1",
            "probe",
            GraphEntityCreated(entity_id="e-1", entity_type="entity", data={}, at=at),
        )
    )
    # Simulate a crash mid-append: a partial JSON line with no newline.
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "run-1", "event_type": "graph.ent')

    with pytest.raises(ReplayMalformedEventError, match="line 2"):
        asyncio.run(replay_graph(log.path, tmp_path / "replay.db"))


@pytest.mark.asyncio
async def test_corrupt_artifact_index_fails_loudly(tmp_path: Path) -> None:
    """A torn artifact index is a loud ArtifactIndexError, never rebuilt."""
    store = ArtifactStore(tmp_path / "artifacts")
    await store.put(source=b"payload bytes")
    store.index_path.write_text('{"art-1": {', encoding="utf-8")  # torn write

    with pytest.raises(ArtifactIndexError, match="corrupt"):
        await store.get("art-1")
    # Even a put (which tolerates a MISSING index) refuses a corrupt one loudly.
    with pytest.raises(ArtifactIndexError, match="corrupt"):
        await store.put(source=b"more bytes")


# ---------------------------------------------------------------------------
# heartbeat failure: the emitter fails loudly instead of silently stopping
# ---------------------------------------------------------------------------


def test_heartbeat_summary_failure_propagates() -> None:
    """A failing summary callable raises out of run(): never silently swallowed."""

    async def _noop_sleep(_: float) -> None:
        return None

    def _broken_summary() -> str:
        raise RuntimeError("summary callback crashed")

    heartbeat = Heartbeat(1.0, summary=_broken_summary, sleeper=_noop_sleep)
    with pytest.raises(RuntimeError, match="summary callback crashed"):
        asyncio.run(heartbeat.run())


def test_heartbeat_sleeper_failure_propagates() -> None:
    """A failing sleeper raises out of run(): the failure is loud, not silent."""

    async def _broken_sleep(_: float) -> None:
        raise RuntimeError("sleeper crashed")

    heartbeat = Heartbeat(1.0, sleeper=_broken_sleep)
    with pytest.raises(RuntimeError, match="sleeper crashed"):
        asyncio.run(heartbeat.run())


# ---------------------------------------------------------------------------
# termination signals: graceful structured termination event
# ---------------------------------------------------------------------------


def _supervisor_config(tmp_path: Path) -> OzzGraphConfig:
    return OzzGraphConfig(
        hal_user_id="user-42",
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "state" / "artifacts",
    )


def test_sigterm_style_stop_records_structured_termination_event(tmp_path: Path) -> None:
    """stop(INTERRUPTED) — the SIGTERM/SIGINT path — appends a termination event.

    The subprocess-level signal delivery (SIGTERM/SIGINT -> exit 130) is
    covered by tests/test_signals.py; this proves the structured record
    the harness writes on that path.
    """
    supervisor = Supervisor(_supervisor_config(tmp_path))
    supervisor.start()
    reason = supervisor.stop(TerminationReason.INTERRUPTED)

    assert reason is TerminationReason.INTERRUPTED
    events = _read_events(EventLog.for_run(tmp_path / "state"))
    assert [event["event_type"] for event in events] == [BOOTSTRAP, TERMINATION]
    termination = events[1]
    assert termination["producer"] == "supervisor"
    assert termination["payload"]["reason"] == "interrupted"


def test_stop_before_start_writes_no_termination_event(tmp_path: Path) -> None:
    """Stopping before start() is a no-op: no event, no state dir."""
    supervisor = Supervisor(_supervisor_config(tmp_path))

    assert supervisor.stop(TerminationReason.INTERRUPTED) is TerminationReason.INTERRUPTED
    assert not (tmp_path / "state" / "actions.jsonl").exists()
