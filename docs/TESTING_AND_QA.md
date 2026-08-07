# Test Suite and Quality Assurance

## Quality Philosophy

The harness controls security-sensitive and state-sensitive behavior. Tests must cover not only successful solves but also malformed outputs, loops, timeouts, partial failures, and replay consistency.

## Unit Tests

Required areas:

- environment parsing
- target extraction
- command fingerprinting
- scope validation
- timeout handling
- output truncation
- flag extraction
- provenance checks
- graph insertion
- graph replay
- checkpoints
- phase transitions
- hint policy
- scheduler conflicts
- adapter parsing
- context rendering
- budget accounting

## Contract Tests

Contract-test:

- OpenAI-compatible model endpoint
- HalCTF client
- `halctl` CLI
- `ToolResult`
- skill YAML
- graph events
- dashboard API
- run-bundle format

## Integration Tests

Use:

- fake model server
- fake MCP server
- temporary SQLite database
- temporary artifact store
- isolated synthetic target containers

Required scenarios:

1. Smoke-test flag discovered and submitted.
2. Simple web challenge solved.
3. Wrong hypothesis abandoned.
4. Duplicate command blocked.
5. Malformed output repaired.
6. Context compacted without losing objective.
7. Worker crash recovered.
8. Paid hint blocked.
9. Unsupported flag rejected.
10. Correct flag submitted and graceful exit called.

Scenario 8 ("Paid hint blocked") is covered end-to-end by
`tests/test_hints.py` (every paid-hint gate rule denies its own
condition, the gate is fail-closed, and concurrent evaluations never
double-purchase) and `tests/test_supervisor.py`
(`test_request_paid_hint_blocked_end_to_end` drives the
supervisor-owned `request_paid_hint` on a graph the gate denies and
asserts the wire is never reached).

The "scheduler conflicts" unit-test area (PR24) is covered by
`tests/test_scheduler.py`: DAG construction failures (duplicate id,
missing dependency, cycle, self-dependency), conflict-key mutual
exclusion (an instrumented gate runner records execution intervals and
asserts conflicting tasks never overlap while independent tasks run
concurrently), dependency ordering, bounded parallelism (never more
than `max_workers` concurrent), deterministic scheduling order (two
schedules of the same DAG produce identical start sequences),
supervisor-only serialization (the reserved `serialized` key never
overlaps any other task), the structured-findings contract (mandatory
evidence references, task attribution), structured failure paths (a
failed outcome and a crashing runner both become failed `worker_run`
records), and graph/event persistence with replay consistency
(replaying the event log reconstructs the identical graph hash).

The "worker scopes" unit-test area (PR25) is covered by
`tests/test_workers.py`: scope construction validation (empty scopes,
blank/duplicate/unknown families, read-only scopes declaring mutating
families, target-allowlist validation, deterministic canonicalization),
scope containment (families, phases, mutation permission, CIDR-aware
target narrowing), the assignment gate (out-of-scope tasks rejected
with the typed `TaskOutOfScopeError`, duplicate assignments, the
supervisor-serialized task gate), run-time action enforcement (a
read-only worker can never run a mutating-family command, families
outside the declared scope are rejected loudly — every rejection test
uses an instrumented recording runner and asserts nothing ever
executed), the bounded execution pipeline (policy gate + fingerprint
duplicate rejection + content-addressed artifact evidence in findings),
structured failed outcomes, and an integration of a TaskDAG of
specialist workers (recon, artifact analysis, serialized submission)
driven through the scheduler with deterministic results and replay
consistency (in-memory SQLite plus file-backed replay).

## Synthetic Challenge Suite

Include isolated targets for:

- HTTP reconnaissance
- hidden routes
- authentication logic
- source vulnerability localization
- file forensics
- binary string extraction
- credential reuse
- simple network pivot
- multi-stage flag discovery

The suite is implemented as the synthetic test lab (PR27) in
`src/ozzgraph/lab/`: deterministic, loopback-only, stdlib targets the
harness can be pointed at via `OZZGRAPH_TARGET`, with lifecycle,
registry-determinism, per-category discovery, and integration-solve
tests in `tests/test_lab.py` / `tests/test_lab_solve.py`. See
`docs/SYNTHETIC_LAB.md` for the catalogue, the flag format
(`OZ{...}` + `OZZGRAPH_FLAG_PATTERN`), and how to run the suite.

## Golden Traces

A golden trace contains:

- challenge input
- model responses
- tool outputs
- expected graph events
- expected final graph
- expected metrics

Golden traces ensure:

- deterministic replay
- stable reducer behavior
- compaction safety
- prompt-regression visibility
- schema migration compatibility

Implemented as PR28 in `src/ozzgraph/traces.py` (`ozzgraph.traces`):
`capture_trace` snapshots a run's event log, live graph (entity set,
edge set, graph hash, schema version), and metrics into a single JSON
document; `verify_trace` replays the events through `ozzgraph.replay`
into a fresh database and reports every mismatch (entity set, edge set,
graph hash, schema version, metrics) as a structured diff. See
`docs/GOLDEN_TRACES.md` for the format, usage, and the regression
matrix (prompt regression, reducer drift, schema migration, event
loss). Tests: `tests/test_traces.py`.

## Model–Harness Matrix

Evaluate each model with:

- terminal-native protocol
- three-line protocol
- JSON protocol
- function calls when supported

Metrics:

- valid-output rate
- correct tool selection
- repetition rate
- recovery rate
- output tokens per decision
- steps per objective
- solve rate
- unsupported-fact rate
- unsupported-flag rate

Implemented as PR28 in `src/ozzgraph/matrix.py` (`ozzgraph.matrix`):
`evaluate_model` runs a model client (a prompt callable or a
`ModelService`-like object) against the harness protocols — reusing the
`ozzgraph.adapters` adapters and `profiles.probe_protocol` for protocol
detection — and the synthetic lab targets, computing all nine metrics
deterministically from the recorded interactions (a scope-policy-gated
bounded shell per tool action; nothing outside loopback is ever
touched). The metrics are also the golden trace's `expected_metrics`
contract. See `docs/GOLDEN_TRACES.md` for the metric definitions and
usage. Tests: `tests/test_matrix.py`.

## Adversarial Tests

Target output fixtures should include:

- fake system instructions
- fake flags
- public-internet suggestions
- ANSI escape sequences
- malformed Unicode
- shell-control characters
- huge repeated output
- deceptive tool instructions

## Chaos Tests

Inject:

- model timeout
- model server 500
- MCP timeout
- malformed MCP response
- process hang
- worker crash
- disk full
- SQLite lock
- partial artifact write
- heartbeat failure
- termination signals

## Performance Tests

Measure:

- startup time
- graph-query latency
- context-compilation time
- parser throughput
- worker scheduling overhead
- SQLite write throughput
- memory usage
- image size

## CI Gates

A PR may merge only when:

- formatting passes
- linting passes
- type checking passes
- unit tests pass
- contract tests pass
- integration tests pass
- golden traces pass
- security scans pass
- image builds
- image-size budget passes
- container smoke test passes
- no critical dependency vulnerability is introduced

## Required Commands

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Optional dashboard:

```bash
cd dashboard
yarn lint
yarn typecheck
yarn test
yarn build
```
