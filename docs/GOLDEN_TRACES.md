# Golden Traces and the Model–Harness Matrix (PR28)

Golden traces and the model–harness matrix implement the QA contract of
docs/TESTING_AND_QA.md: **deterministic replay**, **stable reducer
behavior**, **compaction safety**, **prompt-regression visibility**, and
**schema migration compatibility**.

- `src/ozzgraph/traces.py` — the golden-trace document format plus a
  capture/verify runner (`ozzgraph.traces`).
- `src/ozzgraph/matrix.py` — the model–harness matrix evaluator
  (`ozzgraph.matrix`), which is also the producer of the nine metrics a
  trace stores as its `expected_metrics`.

Both modules are stdlib-only additions over the existing runtime
(pydantic models, no new dependencies), deterministic, and never touch
the public internet.

## Golden Traces

A golden trace is a single JSON document capturing one run:

| section | contents |
|---------|----------|
| `challenge` | challenge input: target identity (name, category, description) and the accepted `flag_pattern` — never the flag value, never the ephemeral target URL |
| `events` | the run's append-only event log lines, in order, as parsed JSON objects (the replayable artifact) |
| `model_responses` | the raw model completions, in order |
| `tool_outputs` | the recorded bounded tool outputs (turn, command, exit code, bounded stdout/stderr) |
| `expected_graph` | the expected final graph: entity set, edge set, and the `graph_hash` digest |
| `expected_metrics` | the expected nine metrics (the matrix contract) |
| `schema_version` | the graph schema version observed at capture time |
| `format` / `format_version` | trace document identity (`ozzgraph.golden_trace` v1) |

### Capturing a trace

Capture snapshots a real run's event log and live graph into a trace
file. The event log is embedded verbatim and the graph is snapshotted
through the same canonical reads the graph hash uses, so the same run
always captures the same trace bytes:

```python
import asyncio
from pathlib import Path

from ozzgraph.state_graph import StateGraph
from ozzgraph.traces import TraceChallenge, TraceMetrics, capture_trace


async def main() -> None:
    challenge = TraceChallenge(
        target_name="hidden-routes",
        target_category="hidden routes",
        target_description="robots.txt advertises /admin; only /admin holds the flag",
        flag_pattern=r"OZ\{[^{}\s]+\}",
    )
    metrics = TraceMetrics(  # the nine matrix metrics for this run
        valid_output_rate=1.0,
        correct_tool_selection=1.0,
        repetition_rate=0.0,
        recovery_rate=1.0,
        output_tokens_per_decision=42.0,
        steps_per_objective=3.0,
        solve_rate=1.0,
        unsupported_fact_rate=0.0,
        unsupported_flag_rate=0.0,
    )
    async with StateGraph(Path("state/graph.db")) as graph:
        await capture_trace(
            Path("traces/hidden-routes.json"),
            events_path=Path("state/actions.jsonl"),
            graph=graph,
            challenge=challenge,
            metrics=metrics,
            model_responses=["probe the root", '{"kind": "think"}'],
            tool_outputs=[],
        )


asyncio.run(main())
```

### Verifying a trace

Verify rewrites the trace's events to a fresh JSONL file and replays
them through `ozzgraph.replay.replay_into` (a fresh database runs the
current migrations), then compares:

1. the reconstructed **entity set** and **edge set** against
   `expected_graph` (canonical content per id),
2. the reconstructed **graph hash** against `expected_graph.graph_hash`,
3. the trace's **schema version** against the replayed database's
   version (a trace captured under an older schema fails loudly with a
   `schema` mismatch instead of silently re-hashing),
4. the supplied `actual_metrics` against `expected_metrics`, field by
   field, with exact float equality (metrics are deterministic
   functions of the recorded interactions).

Every mismatch is reported as a structured
`TraceVerification.mismatches` diff (`path`, `kind`, `detail`) — the
prompt-regression visibility the QA plan requires. Nothing is silently
skipped; a corrupt trace raises `TraceError`.

```python
import asyncio
from pathlib import Path

from ozzgraph.traces import verify_trace


async def main() -> None:
    verification = await verify_trace(
        Path("traces/hidden-routes.json"),
        actual_metrics=None,  # or the run's recomputed TraceMetrics
    )
    if verification.ok:
        print("trace verifies:", verification.replayed_hash)
    else:
        for mismatch in verification.mismatches:
            print(f"- {mismatch.path} [{mismatch.kind}]: {mismatch.detail}")


asyncio.run(main())
```

`verification.assert_ok()` raises `TraceVerificationError` with the full
diff for gate contexts (CI, commit hooks). A capture-then-verify helper
(`capture_and_verify`) is provided for tests: a trace that does not
verify `ok` right after capture means capture and replay disagree — a
broken replay invariant, not a prompt regression.

### What a diff catches

- **Prompt regression**: a changed prompt makes a deterministic client
  produce different completions → different events → a different final
  graph → `entity_set` / `edge_set` / `hash` mismatches, or different
  metrics → `metric` mismatches.
- **Reducer drift**: a reducer change that alters graph mutations
  changes the replayed graph → hash/entity-set mismatch.
- **Schema migration**: bumping `SCHEMA_VERSION` without migrating old
  traces changes the hash header → `schema` mismatch (plus hash).
- **Event loss**: dropping an event line changes the reconstructed
  graph → missing entities/edges plus hash mismatch.

## Model–Harness Matrix

`evaluate_model` runs one model client against the harness protocols —
**terminal-native**, **three-line**, **JSON** (and `function_call` when
an adapter is registered and requested explicitly; the harness never
assumes function-call support) — reusing the concrete adapters from
`ozzgraph.adapters` and `profiles.probe_protocol` for protocol
detection. Each protocol runs one episode per lab target (fresh,
isolated instances from `ozzgraph.lab.get_target`); every interaction is
recorded and the nine metrics are computed per model × protocol.

A model client is either:

- a callable `async (prompt: str) -> str | MatrixCompletion`, or
- a `ModelService`-like object with `async complete(ModelRequest)`
  (its token `usage` accounting drives `output_tokens_per_decision`).

```python
import asyncio

from ozzgraph.matrix import LAB_FLAG_PATTERN, MatrixCompletion, evaluate_model


async def scripted_model(prompt: str) -> str:
    # deterministic fake model; real runs pass a ModelService instead
    return '{"kind": "think", "rationale": "thinking"}'


async def main() -> None:
    report = await evaluate_model(
        scripted_model,
        model_id="my-model",
        targets=("http-recon", "hidden-routes"),  # deterministic subset
        protocols=("terminal", "three_line", "json"),
        max_turns=12,
        flag_pattern=LAB_FLAG_PATTERN,  # lab flags are OZ{...}
    )
    for row in report.rows:
        print(row.protocol, row.metrics.model_dump())
    # report.model_dump_json(indent=2) is the machine-readable report


asyncio.run(main())
```

Before any episode, one fixed probe completion is classified with
`probe_protocol` and recorded as `report.probe.detected_protocol` — the
model's own preferred protocol, independent of the prompted ones.

### Metrics

All nine metrics are deterministic functions of the recorded
interactions (per protocol row); definitions:

| metric | definition |
|--------|------------|
| valid-output rate | completions yielding a usable action (first-attempt parse, or recovered via the adapter's repair strategy) ÷ all completions |
| correct tool selection | run actions the scope-policy gate approved (the harness would execute them) ÷ run actions; 1.0 when none |
| repetition rate | run actions whose fingerprint was already attempted in the episode ÷ run actions; 0.0 when none |
| recovery rate | first-attempt parse failures the adapter's repair strategy turned into a usable action ÷ first-attempt failures; 1.0 when none |
| output tokens per decision | total completion tokens ÷ completions; 0.0 when none |
| steps per objective | total turns ÷ episodes (one objective per episode); 0.0 when none |
| solve rate | episodes that submitted the target's flag ÷ episodes; 0.0 when none |
| unsupported-fact rate | flag-shaped claims in rationales / non-submit payloads that never appeared in any prior tool output ÷ all such claims (AGENTS.md rule #3: a fact requires deterministic evidence); 0.0 when none |
| unsupported-flag rate | submit actions whose payload is not the target's flag ÷ submit actions; 0.0 when none |

Tool actions run through the bounded `ShellRunner` and the fail-closed
`ScopePolicy` gate with `127.0.0.1` allowlisted (the lab's loopback
surface): a command the policy rejects is **never executed** and counts
as an incorrect tool selection.

## Tests

- `tests/test_traces.py` — capture → verify round trip, byte-deterministic
  capture, replay reproducing the expected final graph, and regression
  detection (mutated metric, entity, hash, dropped event, schema
  migration), plus loud failures on corrupt traces.
- `tests/test_matrix.py` — a deterministic scripted model exercises all
  nine metrics against hand-computed values, including repair recovery,
  repetition, a policy-rejected tool, unsupported claims, and a solve;
  determinism (modulo the lab's ephemeral port), probe detection, both
  client forms, and loud failure paths.
