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
