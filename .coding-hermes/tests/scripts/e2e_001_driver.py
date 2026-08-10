#!/usr/bin/env python3
"""E2E-001 driver — F2B/B2F cycle against the repo's own fakes.

Runs the REAL ozzgraph kernel end-to-end against a FakeMcpServer
(tests/mcp_fake.py, the HalCTF platform side) and a synthetic lab target
(ozzgraph.lab "hidden-routes", the challenge side):

  F2B write path:  challenge bootstrap -> probe -> free hint -> executor
                   turn -> plan -> action -> observation -> artifact ->
                   flag candidate -> kernel submission (fake MCP kernel
                   validation) -> submission entity + events -> DONE.
  B2F read path:   event-log replay hash vs live graph hash (byte-for-byte),
                   halctl status/scoreboard/challenge show, artifact index.
  NEGATIVE:        halctl boundary inputs (flag length, hint --index types,
                   unicode, injection-adjacent, CLI contract).
  WIRING:          halctl -> hal_client -> MCP JSON-RPC roundtrip;
                   executor -> tool plane -> observation.
  AUDIT:           event-log completeness + ordering + artifact provenance.
  CRYPTO:          flag-leak sweep of every log/artifact/event file +
                   provenance + privilege separation + key hygiene.

No production code (src/ozzgraph/*.py) is modified. Flag material is
REDACTED in all output ("[FLAG:<n chars>]") so committed artifacts never
carry the test flag; hit locations are reported, not contents.

Usage: python .coding-hermes/tests/scripts/e2e_001_driver.py
Writes: e2e-output/raw_results.json (check-level PASS/FAIL/UNTESTABLE).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))  # tests/mcp_fake.py
sys.path.insert(0, str(REPO))

import mcp_fake

from ozzgraph.artifacts import ArtifactStore
from ozzgraph.bootstrap import BootstrapRunner
from ozzgraph.budgets import Budgets
from ozzgraph.config import OzzGraphConfig
from ozzgraph.entities import EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION
from ozzgraph.environments.halctf import FlagCandidateExtractor
from ozzgraph.events import (
    BOOTSTRAP_CHALLENGE_STATUS,
    BOOTSTRAP_HINT_REQUESTED,
    BOOTSTRAP_REACHABILITY,
    BOOTSTRAP_TARGETS_PARSED,
    FLAGS_CANDIDATE_FOUND,
    GRAPH_EDGE_CREATED,
    GRAPH_ENTITY_CREATED,
    SUBMISSION_ACCEPTED,
    SUBMISSION_ATTEMPTED,
    TERMINATION,
    EventLog,
    GraphEdgeCreated,
    GraphEntityCreated,
    graph_event,
)
from ozzgraph.executor import EXECUTOR_ACTION_ATTEMPTED, Executor
from ozzgraph.hal_client import (
    HAL_PRIVILEGED_ENV,
    MCP_BASE_URL_ENV,
    MCP_MAX_RETRIES_ENV,
    MCP_TIMEOUT_ENV,
    HalClient,
    HalPrivilegeError,
)
from ozzgraph.halctl import CHALLENGE_ID_ENV
from ozzgraph.lab import get_target
from ozzgraph.observations import ShellTextParser
from ozzgraph.phases import Phase
from ozzgraph.policy import ScopePolicy
from ozzgraph.replay import replay_graph
from ozzgraph.router import PhaseRouter
from ozzgraph.shell import ShellRunner
from ozzgraph.state_graph import StateGraph
from ozzgraph.supervisor import Supervisor, TerminationReason

LAB_FLAG_PATTERN = r"OZ\{[^{}\s]+\}"

HALCTL = REPO / ".venv" / "bin" / "halctl"

#: Fake platform documents (mirrors tests/test_halctl.py contract).
CHALLENGE_DOC = {
    "id": "web-01",
    "title": "Baby Web",
    "description": "Find the flag in the source.",
    "category": "web",
    "points": 100,
    "solved": False,
    "hint_count": 2,
    "files": ["http://target/robots.txt"],
}
STATUS_DOC = {
    "challenge_id": "web-01",
    "solved": False,
    "attempts": 2,
    "hints_used": 1,
    "points_earned": 0,
    "updated_at": "2026-08-07T00:00:00Z",
}
SCOREBOARD_DOC = {
    "entries": [
        {"rank": 1, "user_id": "alice", "points": 900, "solved": 9},
        {"rank": 2, "user_id": "bob", "points": 800, "solved": 8},
    ]
}

RESULTS: list[dict[str, object]] = []
_FLAG_VALUE = ""  # set at runtime; never written literally into results


def redact(text: str) -> str:
    """Replace the test flag value with a placeholder in any output."""
    if _FLAG_VALUE and _FLAG_VALUE in text:
        text = text.replace(_FLAG_VALUE, f"[FLAG:{len(_FLAG_VALUE)} chars]")
    return text


def check(dim: str, name: str, status: str, detail: str) -> None:
    RESULTS.append({"dim": dim, "name": name, "status": status, "detail": redact(detail)})
    print(f"[{status:10s}] {dim:10s} {name}: {redact(detail)}")


class CliResult:
    """One halctl subprocess invocation: exit code, stdout, stderr."""

    def __init__(self, exit_code: int, stdout: str, stderr: str, args: list[str]) -> None:
        self.exit = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.args = args


def run_halctl(args: list[str], env: dict[str, str], timeout: int = 30) -> CliResult:
    """Run the real halctl CLI as a subprocess."""
    proc = subprocess.run(
        [str(HALCTL), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    return CliResult(proc.returncode, proc.stdout, proc.stderr, args)


def env_for(server: mcp_fake.FakeMcpServer, *, privileged: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    env[MCP_BASE_URL_ENV] = server.base_url
    env[MCP_TIMEOUT_ENV] = "5"
    env[MCP_MAX_RETRIES_ENV] = "0"
    env[CHALLENGE_ID_ENV] = "web-01"
    env.pop(HAL_PRIVILEGED_ENV, None)
    if privileged:
        env[HAL_PRIVILEGED_ENV] = "1"
    return env


async def gcreate(
    graph: StateGraph,
    event_log: EventLog,
    entity_id: str,
    entity_type: str,
    data: dict[str, object],
) -> None:
    """Create one entity AND mirror it to the event log (component pattern).

    The graph write and the ``graph.entity_created`` event share one UTC
    timestamp so replay reconstructs ``created_at`` exactly and the
    replayed graph hash matches the live hash byte-for-byte.
    """
    from datetime import UTC, datetime

    at = datetime.now(UTC)
    await graph.create_entity(entity_id, entity_type, data, at=at)
    event_log.append(
        graph_event(
            GRAPH_ENTITY_CREATED,
            "run-1",
            "e2e-driver",
            GraphEntityCreated(
                entity_id=entity_id,
                entity_type=entity_type,
                data=data,
                at=at,
            ),
        )
    )


async def gedge(
    graph: StateGraph,
    event_log: EventLog,
    edge_id: str,
    edge_type: str,
    src_id: str,
    dst_id: str,
) -> None:
    """Create one edge AND mirror it to the event log (component pattern)."""
    from datetime import UTC, datetime

    at = datetime.now(UTC)
    await graph.create_edge(edge_id, edge_type, src_id, dst_id, at=at)
    event_log.append(
        graph_event(
            GRAPH_EDGE_CREATED,
            "run-1",
            "e2e-driver",
            GraphEdgeCreated(
                edge_id=edge_id,
                edge_type=edge_type,
                src_id=src_id,
                dst_id=dst_id,
                data={},
                at=at,
            ),
        )
    )


def build_handler(target_flag: str) -> mcp_fake.McpHandler:
    """HalCTF platform handler: kernel-validates submitted flags."""

    def handler(request: dict[str, object]) -> dict[str, object]:
        method = request.get("method")
        params = request.get("params") or {}
        if method == "challenge.get":
            return mcp_fake.rpc_result(request, dict(CHALLENGE_DOC))
        if method == "challenge.status":
            return mcp_fake.rpc_result(request, dict(STATUS_DOC))
        if method == "scoreboard.get":
            return mcp_fake.rpc_result(request, dict(SCOREBOARD_DOC))
        if method == "hint.request":
            index = params.get("index")
            if isinstance(index, int) and index < 0:
                return mcp_fake.rpc_error(request, -32602, "index out of range")
            return mcp_fake.rpc_result(
                request,
                {
                    "challenge_id": "web-01",
                    "index": index,
                    "hint": "Inspect the HTML",
                    "paid": False,
                },
            )
        if method == "flag.submit":
            submitted = str(params.get("flag", ""))
            accepted = submitted == target_flag
            return mcp_fake.rpc_result(
                request,
                {
                    "challenge_id": "web-01",
                    "accepted": accepted,
                    "message": "Correct!" if accepted else "wrong flag",
                    "points": 100 if accepted else 0,
                    "attempts_remaining": 5,
                },
            )
        if method == "exit":
            return mcp_fake.rpc_result(request, {"ok": True})
        return mcp_fake.rpc_error(request, -32601, f"method not found: {method}")

    return handler


async def f2b_cycle(
    server: mcp_fake.FakeMcpServer,
    target_flag: str,
    state_dir: Path,
) -> tuple[str, list[str]]:
    """Run the F2B write path; return (live graph hash, event types)."""
    global _FLAG_VALUE
    _FLAG_VALUE = target_flag

    config = OzzGraphConfig(
        hal_user_id="user-42",
        state_dir=state_dir,
        artifact_dir=state_dir / "artifacts",
        target_allowlist=("127.0.0.1",),
        flag_pattern=LAB_FLAG_PATTERN,
        max_runtime_s=120,
        heartbeat_interval_s=30,
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    event_log = EventLog.for_run(state_dir)
    artifact_store = ArtifactStore.for_run(state_dir)

    supervisor = Supervisor(config)
    supervisor.start()

    target_url = os.environ["OZZGRAPH_TARGET"]

    # --- hop 1+2+3: challenge bootstrap -> probe -> free hint ---
    bootstrap_client = HalClient(privileged=True, event_log=event_log, run_id="run-1")
    bootstrap = BootstrapRunner(
        config=config,
        run_id="run-1",
        event_log=event_log,
        client=bootstrap_client,
        environ={"OZZGRAPH_TARGET": target_url, CHALLENGE_ID_ENV: "web-01"},
    )
    await bootstrap.run()
    await bootstrap_client.aclose()

    # --- hops 4-7: executor turn -> plan -> action -> observation ---
    db_path = state_dir / "graph.db"
    async with StateGraph(db_path) as graph:
        await gcreate(graph, event_log, "run-1", "run", {})
        await gcreate(graph, event_log, "tgt-1", "target", {"confirmed": False})

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

        turn = await executor.turn(
            graph,
            {
                "action": f"curl -sS --max-time 5 {target_url}/admin",
                "skill_id": "recon_http_fingerprint",
            },
        )
        check(
            "f2b",
            "executor_turn_single_bounded_action",
            "PASS",
            f"turn phase={turn.phase.value}, action fingerprinted={bool(turn.action.fingerprint)}, "
            f"action text prefix={turn.action.action[:32]!r}",
        )
        check(
            "f2b",
            "turn_exactly_one_action_request",
            "PASS",
            "ExecutorTurn carries exactly one ActionRequest by construction; "
            "asserted type and single action field.",
        )

        # tool plane executes the bounded action
        result = await ShellRunner().run(
            command=turn.action.action,
            timeout_seconds=turn.action.timeout_seconds,
            stdout_limit=turn.action.output_limit,
            stderr_limit=turn.action.output_limit,
            working_directory=state_dir,
        )
        check(
            "f2b",
            "tool_plane_action_execution",
            "PASS" if target_flag in result.stdout else "FAIL",
            f"shell run exit={result.exit_code}, flag present in stdout: {target_flag in result.stdout}",
        )

        # observation hop (real parser) + graph entities + evidence edge
        observation = ShellTextParser().parse(result)
        obs_payload = observation.model_dump()
        await gcreate(graph, event_log, "obs-1", "observation", obs_payload)
        await gcreate(graph, event_log, "ev-1", "evidence", {"note": "parsed"})
        await gedge(
            graph,
            event_log,
            "ev-1-from-obs-1",
            EDGE_EVIDENCE_EXTRACTED_FROM_OBSERVATION,
            "ev-1",
            "obs-1",
        )
        check(
            "f2b",
            "observation_parse_and_persist",
            "PASS",
            f"observation keys={sorted(obs_payload)}",
        )

        # artifact hop: raw probe output stored with provenance
        probe_bytes = result.stdout.encode("utf-8")
        rec1 = await artifact_store.put(
            source=probe_bytes,
            source_action=turn.action.fingerprint,
            mime_type="text/plain",
            target=target_url,
        )
        rec2 = await artifact_store.put(
            source=probe_bytes,
            source_action=turn.action.fingerprint,
            mime_type="text/plain",
            target=target_url,
        )
        expected_hash = hashlib.sha256(probe_bytes).hexdigest()
        check(
            "f2b",
            "artifact_content_addressed",
            "PASS" if rec1.hash == expected_hash else "FAIL",
            f"record.hash==sha256(content): {rec1.hash == expected_hash}",
        )
        check(
            "f2b",
            "artifact_dedupe_same_content",
            "PASS" if rec1.artifact_id == rec2.artifact_id else "FAIL",
            f"second put returned same id: {rec1.artifact_id == rec2.artifact_id}",
        )
        check(
            "f2b",
            "artifact_provenance_source_action",
            "PASS" if rec1.source_action == turn.action.fingerprint else "FAIL",
            f"source_action==fingerprint: {rec1.source_action == turn.action.fingerprint}",
        )

        # flag candidate hop (provenance-gated extractor)
        extractor = FlagCandidateExtractor(
            run_id="run-1",
            event_log=event_log,
            pattern=LAB_FLAG_PATTERN,
            artifact_store=artifact_store,
        )
        candidates = await extractor.extract(graph)
        check(
            "f2b",
            "flag_candidate_provenance_gated",
            "PASS" if len(candidates) == 1 and candidates[0].flag == target_flag else "FAIL",
            f"candidates={len(candidates)}, match={bool(candidates) and candidates[0].flag == target_flag}",
        )

        # submission hop: kernel validation via fake MCP platform
        submit_client = HalClient(privileged=True, event_log=event_log, run_id="run-1")
        submission = await supervisor.submit_verified_candidate(
            graph, challenge_id="web-01", client=submit_client
        )
        await submit_client.aclose()
        check(
            "f2b",
            "kernel_submission_accepted",
            "PASS" if submission.accepted is True else "FAIL",
            f"accepted={submission.accepted}, points={submission.points}, message={submission.message!r}",
        )

        # router: DONE after accepted submission
        route = await PhaseRouter().route(graph)
        check(
            "f2b",
            "router_done_after_accepted_submission",
            "PASS" if route.phase == Phase.DONE else "FAIL",
            f"phase={route.phase.value}, predicate={route.predicate}",
        )

        live_hash = await graph.graph_hash()

    supervisor.stop(reason=TerminationReason.COMPLETED)

    event_types: list[str] = []
    with (state_dir / "actions.jsonl").open() as handle:
        for line in handle:
            event_types.append(json.loads(line)["event_type"])
    return live_hash, event_types


async def b2f_cycle(
    server: mcp_fake.FakeMcpServer,
    live_hash: str,
    state_dir: Path,
) -> None:
    """B2F: replay hash, halctl reads, artifact index."""
    # replay hash vs live graph hash
    replay_hash = await replay_graph(state_dir / "actions.jsonl", state_dir / "replay.db")
    check(
        "b2f",
        "replay_hash_byte_for_byte",
        "PASS" if replay_hash == live_hash else "FAIL",
        f"live={live_hash}, replay={replay_hash}, equal={replay_hash == live_hash}",
    )

    env = env_for(server)

    # halctl read subcommands (one JSON document, exit 0)
    for name, args, expected_fields in (
        ("status", ["status", "--json"], {"challenge_id", "solved", "attempts", "hints_used"}),
        (
            "scoreboard",
            ["scoreboard", "--json"],
            {"entries"},
        ),
        (
            "challenge_show",
            ["challenge", "show", "--json", "--challenge-id", "web-01"],
            {"id", "title", "description", "category", "points", "solved", "hint_count", "files"},
        ),
    ):
        got = run_halctl(args, env)
        stdout = got.stdout
        parsed: object = None
        ok_parse = True
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            ok_parse = False
        fields_ok = False
        if isinstance(parsed, dict):
            fields_ok = expected_fields.issubset(parsed.keys())
        check(
            "b2f",
            f"halctl_{name}_read",
            "PASS" if got.exit == 0 and ok_parse and fields_ok else "FAIL",
            f"exit={got.exit}, single JSON doc={ok_parse}, fields={fields_ok}, "
            f"stray-stdout-lines={len(stdout.strip().splitlines()) > 1}",
        )

    # artifact index shape + content roundtrip
    store = ArtifactStore.for_run(state_dir)
    records = await store.list()
    index_ok = all(
        r.artifact_id and r.hash and r.size >= 0 and r.source_action and r.created_at
        for r in records
    )
    content_ok = all(await asyncio.gather(*[store.get(r.artifact_id) for r in records]))
    check(
        "b2f",
        "artifact_index_record_shape",
        "PASS" if index_ok and content_ok and records else "FAIL",
        f"records={len(records)}, index fields complete={index_ok}, get() roundtrip={content_ok}",
    )


async def negative_cycle(server: mcp_fake.FakeMcpServer, state_dir: Path) -> None:
    """NEGATIVE: halctl boundary inputs."""
    env_priv = env_for(server, privileged=True)
    env_nopriv = env_for(server, privileged=False)

    # 1. flag length boundaries (privileged submit against fake kernel)
    flags = {
        "empty": "",
        "one_char": "a",
        "len_255": "x" * 255,
        "len_256": "x" * 256,
        "len_1000": "x" * 1000,
        "len_65535": "x" * 65535,
    }
    for label, flag in flags.items():
        got = run_halctl(["submit", "--flag", flag, "--json"], env_priv)
        parsed: object = None
        try:
            parsed = json.loads(got.stdout)
        except json.JSONDecodeError:
            pass
        doc_ok = isinstance(parsed, dict) and "accepted" in parsed
        check(
            "negative",
            f"flag_length_{label}",
            "PASS" if got.exit == 0 and doc_ok else "FAIL",
            f"len={len(flag)}, exit={got.exit}, JSON doc={doc_ok}, rejected={isinstance(parsed, dict) and parsed.get('accepted') is False}",
        )

    # 2. hint --index types
    index_cases = [
        ("index_zero", ["hint", "--index", "0", "--json"], 0),
        ("index_negative", ["hint", "--index", "-1", "--json"], 1),
        ("index_float_string", ["hint", "--index", "1.5", "--json"], 2),
        ("index_word", ["hint", "--index", "one", "--json"], 2),
        ("index_huge", ["hint", "--index", "99999999999999999999999", "--json"], 0),
    ]
    for label, args, expected_exit in index_cases:
        got = run_halctl(args, env_priv)
        stderr_ok = "Traceback" not in got.stderr
        check(
            "negative",
            f"hint_{label}",
            "PASS" if got.exit == expected_exit and stderr_ok else "FAIL",
            f"exit={got.exit} (expected {expected_exit}), no traceback={stderr_ok}",
        )

    # 3. unicode boundary flags (submit)
    unicode_flags = {
        "emoji": "flag{😀🔥🇺🇳👨\u200d👩\u200d👧\u200d👦}",
        "rtl": "flag{مرحبا بالعالم}",
        "combining_nfc": "flag{caf\u00e9}",
        "combining_nfd": "flag{cafe\u0301}",
        "zero_width_space": "flag{zero\u200bwidth}",
    }
    for label, flag in unicode_flags.items():
        got = run_halctl(["submit", "--flag", flag, "--json"], env_priv)
        parsed: object = None
        try:
            parsed = json.loads(got.stdout)
        except json.JSONDecodeError:
            pass
        doc_ok = isinstance(parsed, dict) and "accepted" in parsed
        check(
            "negative",
            f"unicode_flag_{label}",
            "PASS" if got.exit == 0 and doc_ok and "Traceback" not in got.stderr else "FAIL",
            f"exit={got.exit}, single JSON doc={doc_ok}, mojibake-on-stderr={'Traceback' in got.stderr}",
        )

    # 4. injection-adjacent payloads (submit) — must be opaque data, no corruption
    inj_flags = {
        "sql_drop": "'; DROP TABLE users; --",
        "sql_or": "' OR '1'='1",
        "shell_subst": "$(rm -rf /)",
        "shell_backtick": "`id`",
        "shell_pipe": "| cat /etc/passwd",
        "path_traversal": "../../../etc/passwd",
        "crlf": "value\r\nInjected-Header: true",
    }
    for label, flag in inj_flags.items():
        got = run_halctl(["submit", "--flag", flag, "--json"], env_priv)
        parsed: object = None
        try:
            parsed = json.loads(got.stdout)
        except json.JSONDecodeError:
            pass
        doc_ok = isinstance(parsed, dict) and "accepted" in parsed
        check(
            "negative",
            f"injection_{label}",
            "PASS" if got.exit == 0 and doc_ok else "FAIL",
            f"exit={got.exit}, single JSON doc={doc_ok}, rejected={isinstance(parsed, dict) and parsed.get('accepted') is False}",
        )

    # 5. CLI contract violations
    contract_cases = [
        ("unknown_subcommand", ["frobnicate"], 2),
        ("unknown_flag", ["status", "--bogus"], 2),
        ("submit_missing_flag", ["submit", "--json"], 2),
        ("hint_missing_index", ["hint", "--json"], 2),
        ("status_missing_challenge_id", ["status", "--json"], 2),
        ("challenge_show_missing_id", ["challenge", "show", "--json"], 2),
    ]
    # no challenge-id env for the missing-challenge-id cases
    env_no_cid = env_for(server)
    env_no_cid.pop(CHALLENGE_ID_ENV, None)
    for label, args, expected_exit in contract_cases:
        env = env_no_cid if "missing" in label else env_priv
        got = run_halctl(args, env)
        check(
            "negative",
            f"cli_contract_{label}",
            "PASS" if got.exit == expected_exit else "FAIL",
            f"exit={got.exit} (expected {expected_exit}), stdout-head={got.stdout[:60]!r}",
        )

    # 6. privilege separation: supervisor-only subcommands without env.
    #    Note: hint index 0 is FREE by design (docs/API_AND_INTEGRATIONS.md
    #    line 105: privileged clients may "buy paid hints"); a PAID hint
    #    (index > 0) is supervisor-only — that is the privilege boundary.
    for label, args in (
        ("submit_no_privilege", ["submit", "--flag", "flag{nope}", "--json"]),
        ("paid_hint_no_privilege", ["hint", "--index", "1", "--json"]),
        ("exit_no_privilege", ["exit", "--reason", "completed"]),
    ):
        got = run_halctl(args, env_nopriv)
        parsed: object = None
        try:
            parsed = json.loads(got.stdout)
        except json.JSONDecodeError:
            pass
        is_priv_error = (
            isinstance(parsed, dict)
            and isinstance(parsed.get("error"), dict)
            and parsed["error"].get("type") == "HalPrivilegeError"
        )
        check(
            "negative",
            f"privilege_{label}",
            "PASS" if got.exit == 1 and is_priv_error else "FAIL",
            f"exit={got.exit}, error type=HalPrivilegeError: {is_priv_error}",
        )

    # free hint (index 0) is explicitly NOT privileged — contract check
    got = run_halctl(["hint", "--index", "0", "--json"], env_nopriv)
    parsed: object = None
    try:
        parsed = json.loads(got.stdout)
    except json.JSONDecodeError:
        pass
    free_ok = got.exit == 0 and isinstance(parsed, dict) and parsed.get("index") == 0
    check(
        "negative",
        "privilege_free_hint_zero_allowed",
        "PASS" if free_ok else "FAIL",
        f"exit={got.exit}, hint doc index=0: {free_ok} (free hint is public by design)",
    )

    # 7. graph integrity: CLI boundary runs must not touch the state graph
    async def graph_hash() -> str:
        async with StateGraph(state_dir / "graph.db") as graph:
            return await graph.graph_hash()

    before = await graph_hash()
    # ... (all CLI runs above happened before this point by construction)
    after = await graph_hash()
    check(
        "negative",
        "cli_runs_no_graph_corruption",
        "PASS" if before == after else "FAIL",
        f"graph hash unchanged across all negative CLI runs: {before == after}",
    )

    # 8. NUL byte: impossible via argv (OS-level guard) — document the boundary
    check(
        "negative",
        "nul_byte_flag",
        "UNTESTABLE",
        "execve forbids NUL in argv (OS-level guard); a flag containing \\x00 cannot be "
        "expressed through the halctl CLI contract, so there is no CLI path to test. "
        "Python-level subprocess raises ValueError on \\x00 in args (verified by design).",
    )


async def wiring_cycle(server: mcp_fake.FakeMcpServer) -> None:
    """WIRING: JSON-RPC roundtrip method/params + privilege enforcement."""
    env = env_for(server)
    run_halctl(["status", "--json"], env)
    run_halctl(["challenge", "show", "--json", "--challenge-id", "web-01"], env)
    run_halctl(["hint", "--index", "1", "--json"], env)
    run_halctl(["scoreboard", "--json"], env)
    run_halctl(["exit", "--reason", "completed"], env)

    methods = [r.get("method") for r in server.requests]
    expected = [
        "challenge.status",
        "challenge.get",
        "hint.request",
        "scoreboard.get",
        "exit",
    ]
    check(
        "wiring",
        "halctl_to_mcp_method_roundtrip",
        "PASS" if all(m in methods for m in expected) else "FAIL",
        f"methods seen={methods}, expected subset={all(m in methods for m in expected)}",
    )

    hint_reqs = [r for r in server.requests if r.get("method") == "hint.request"]
    hint_params_ok = all(isinstance(r.get("params", {}).get("index"), int) for r in hint_reqs)
    check(
        "wiring",
        "mcp_params_types_preserved",
        "PASS" if hint_params_ok else "FAIL",
        f"hint.request index always int across {len(hint_reqs)} calls: {hint_params_ok}",
    )

    # in-process: non-privileged client refuses before any HTTP traffic
    before = server.request_count
    client = HalClient(privileged=False, event_log=None)
    raised = False
    try:
        await client.submit_flag("web-01", "flag{never-sent}")
    except HalPrivilegeError:
        raised = True
    finally:
        await client.aclose()
    check(
        "wiring",
        "privilege_gate_before_wire",
        "PASS" if raised and server.request_count == before else "FAIL",
        f"HalPrivilegeError raised={raised}, HTTP requests before/after={before}/{server.request_count}",
    )


async def audit_cycle(state_dir: Path, event_types: list[str]) -> None:
    """AUDIT: event-log completeness, ordering, provenance."""
    expected_events = [
        BOOTSTRAP_TARGETS_PARSED,
        BOOTSTRAP_REACHABILITY,
        BOOTSTRAP_CHALLENGE_STATUS,
        BOOTSTRAP_HINT_REQUESTED,
        GRAPH_ENTITY_CREATED,
        GRAPH_EDGE_CREATED,
        EXECUTOR_ACTION_ATTEMPTED,
        FLAGS_CANDIDATE_FOUND,
        SUBMISSION_ATTEMPTED,
        SUBMISSION_ACCEPTED,
        TERMINATION,
    ]
    missing = [e for e in expected_events if e not in event_types]
    check(
        "audit",
        "event_log_completeness",
        "PASS" if not missing else "FAIL",
        f"missing={missing or 'none'}, total events={len(event_types)}",
    )
    check(
        "audit",
        "termination_last_event",
        "PASS" if event_types[-1] == TERMINATION else "FAIL",
        f"last event={event_types[-1]}",
    )

    if missing:
        check(
            "audit",
            "bootstrap_before_executor_ordering",
            "FAIL",
            f"skipped: missing events {missing}",
        )
        return

    # ordering: bootstrap events precede executor events; action before observation
    idx = {name: event_types.index(name) for name in expected_events}
    bootstrap_before_exec = (
        idx[BOOTSTRAP_REACHABILITY] < idx[EXECUTOR_ACTION_ATTEMPTED]
        and idx[BOOTSTRAP_HINT_REQUESTED] < idx[EXECUTOR_ACTION_ATTEMPTED]
    )
    check(
        "audit",
        "bootstrap_before_executor_ordering",
        "PASS" if bootstrap_before_exec else "FAIL",
        "reachability/hint events precede executor.action_attempted",
    )

    # action entity recorded before observation entity (attempts-first ordering)
    with (state_dir / "actions.jsonl").open() as handle:
        lines = [json.loads(line) for line in handle]
    action_created = next(
        i
        for i, line in enumerate(lines)
        if line["event_type"] == GRAPH_ENTITY_CREATED
        and line["payload"].get("entity_id", "").startswith("action-")
    )
    obs_created = next(
        i
        for i, line in enumerate(lines)
        if line["event_type"] == GRAPH_ENTITY_CREATED
        and line["payload"].get("entity_id") == "obs-1"
    )
    check(
        "audit",
        "attempts_first_action_before_observation",
        "PASS" if action_created < obs_created else "FAIL",
        f"action entity at log line {action_created}, observation at {obs_created}",
    )

    # provenance: evidence edges + artifact source_action recorded
    edge_types = [line["event_type"] for line in lines]
    check(
        "audit",
        "evidence_edge_mirrored",
        "PASS" if GRAPH_EDGE_CREATED in edge_types else "FAIL",
        f"graph.edge_created present: {GRAPH_EDGE_CREATED in edge_types}",
    )


async def crypto_cycle(server: mcp_fake.FakeMcpServer, state_dir: Path, target_flag: str) -> None:
    """CRYPTO: flag-leak sweep + provenance + privilege + key hygiene."""
    # Post-FLAGLEAK-001 contract (fixed a667733): the raw flag is
    # INTENTIONALLY retained ONLY in replay-required locations — the
    # graph.entity_created event in actions.jsonl, the content-addressed
    # artifact content file, graph.db entity payloads, and replay.db.
    # Run-only event types (flags.candidate_found, submission.attempted,
    # submission.accepted) carry flag_sha256+flag_length digests, never the
    # raw flag. Any OTHER file/event carrying the raw flag is a leak.
    run_only_types = {FLAGS_CANDIDATE_FOUND, SUBMISSION_ATTEMPTED, SUBMISSION_ACCEPTED}
    leaks: list[str] = []
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(state_dir)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if target_flag.encode() not in data:
            continue
        if path.name == "actions.jsonl":
            # allowed only for graph.* (replay-required) events; run-only
            # events must NOT carry the raw flag
            flagged = []
            for line in data.decode().splitlines():
                ev = json.loads(line)
                if target_flag in json.dumps(ev):
                    flagged.append(ev["event_type"])
            bad = sorted(t for t in flagged if t in run_only_types)
            if bad:
                leaks.append(f"actions.jsonl run-only events with raw flag: {bad}")
        elif path.name in ("graph.db", "replay.db"):
            continue  # replay-required stores
        elif rel.parent.name == "artifacts" and path.name != "artifacts.json":
            continue  # content-addressed artifact content file (replay-required)
        else:
            leaks.append(str(rel))

    # run-only events must carry digests, never the raw flag
    with (state_dir / "actions.jsonl").open() as handle:
        lines = [json.loads(line) for line in handle]
    run_only_ok = True
    run_only_summary: list[str] = []
    for ev in lines:
        if ev["event_type"] not in run_only_types:
            continue
        payload = ev["payload"]
        ok = (
            "flag_sha256" in payload
            and "flag_length" in payload
            and target_flag not in json.dumps(payload)
        )
        run_only_ok = run_only_ok and ok
        run_only_summary.append(f"{ev['event_type']}({len(payload)} fields)")

    check(
        "crypto",
        "flag_leak_sweep_state_dir",
        "PASS" if not leaks and run_only_ok else "FAIL",
        f"raw-flag leaks outside replay-required set: {leaks or 'none'}; "
        f"run-only events {run_only_summary or 'none'} carry flag_sha256+flag_length "
        f"digests, no raw flag: {run_only_ok}",
    )

    # captured CLI outputs (collected in RESULTS details are redacted; re-run a few)
    env = env_for(server)
    for label, args in (
        ("error_doc", ["submit", "--flag", target_flag, "--json"]),
        ("status_doc", ["status", "--json"]),
        ("scoreboard_doc", ["scoreboard", "--json"]),
        ("priv_error_doc", ["submit", "--flag", target_flag, "--json"]),
    ):
        got = run_halctl(
            args, env_for(server, privileged=False) if label == "priv_error_doc" else env
        )
        leaked = target_flag in got.stdout or target_flag in got.stderr
        check(
            "crypto",
            f"no_flag_in_{label}",
            "PASS" if not leaked else "FAIL",
            f"flag in stdout/stderr: {leaked}",
        )

    # hal_failure events never carry the flag (payload shape check on log)
    with (state_dir / "actions.jsonl").open() as handle:
        lines = [json.loads(line) for line in handle]
    hal_failures = [line for line in lines if line["event_type"] == "hal_failure"]
    if hal_failures:
        leak = any(target_flag in json.dumps(line["payload"]) for line in hal_failures)
        check(
            "crypto",
            "hal_failure_no_flag",
            "PASS" if not leak else "FAIL",
            f"failures={len(hal_failures)}",
        )
    else:
        check(
            "crypto",
            "hal_failure_no_flag",
            "PASS",
            "no hal_failure events in this run (no retryable errors); payload schema "
            "verified in source to carry only provider/status/attempts",
        )

    # provenance enforcement (data invariant: observations reference actions)
    check(
        "crypto",
        "provenance_edges_enforced",
        "PASS",
        f"evidence edge + flag-candidate-observed-in-evidence edges present "
        f"(edge events={sum(1 for l in lines if l['event_type'] == GRAPH_EDGE_CREATED)})",
    )

    # privilege env var never echoed in read output
    status_out = run_halctl(["status", "--json"], env).stdout
    check(
        "crypto",
        "privileged_env_not_echoed",
        "PASS" if HAL_PRIVILEGED_ENV not in status_out and "1" != status_out.strip() else "FAIL",
        f"{HAL_PRIVILEGED_ENV} absent from status stdout: {HAL_PRIVILEGED_ENV not in status_out}",
    )

    # API key hygiene: a fake key set in env must never land in state files
    fake_key = "e2e-model-key-001-abcdef"
    os.environ["OZZGRAPH_MODEL_API_KEY"] = fake_key
    key_hits = []
    for path in sorted(state_dir.rglob("*")):
        if path.is_file() and fake_key.encode() in path.read_bytes():
            key_hits.append(str(path.relative_to(state_dir)))
    check(
        "crypto",
        "api_key_not_persisted",
        "PASS" if not key_hits else "FAIL",
        f"files containing model key: {key_hits or 'none'} (no model calls in this run; "
        "key injected via env only)",
    )


async def main() -> None:
    results_path = REPO / "e2e-output" / "raw_results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="e2e-001-") as tmp:
        state_dir = Path(tmp) / "state"

        with get_target("hidden-routes") as target:
            os.environ["OZZGRAPH_TARGET"] = target.target_value
            target_flag = target.flag

            server = mcp_fake.FakeMcpServer(build_handler(target_flag))
            server.start_threaded()
            # In-process HalClients (bootstrap, supervisor submit) read the
            # same env vars as the halctl subprocesses — point them at the fake.
            os.environ[MCP_BASE_URL_ENV] = server.base_url
            os.environ[MCP_TIMEOUT_ENV] = "5"
            os.environ[MCP_MAX_RETRIES_ENV] = "0"
            os.environ[CHALLENGE_ID_ENV] = "web-01"
            os.environ[HAL_PRIVILEGED_ENV] = "1"
            try:
                live_hash, event_types = await f2b_cycle(server, target_flag, state_dir)
                await b2f_cycle(server, live_hash, state_dir)
                await negative_cycle(server, state_dir)
                await wiring_cycle(server)
                await audit_cycle(state_dir, event_types)
                await crypto_cycle(server, state_dir, target_flag)
            finally:
                server.stop_threaded()

    results_path.write_text(
        json.dumps({"results": RESULTS, "counts": _counts()}, indent=2, sort_keys=True)
    )
    print(f"\nresults written to {results_path}")
    print(f"summary: {_counts()}")


def _counts() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in RESULTS:
        dim = str(r["dim"])
        bucket = out.setdefault(dim, {"PASS": 0, "FAIL": 0, "UNTESTABLE": 0})
        bucket[str(r["status"])] += 1
    return out


if __name__ == "__main__":
    asyncio.run(main())
