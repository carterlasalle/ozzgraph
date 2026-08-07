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

Implemented as PR29. The fixture inventory lives in
`tests/adversarial_fixtures.py`: eight named, categorised raw
target-output fixtures (one per category above), consumed by
`tests/test_adversarial.py`, which wires every fixture through the four
untrusted-data surfaces and proves the harness treats all model and
target output as data (AGENTS.md "All model output is untrusted"):

- observation parsers (`ozzgraph.observations`): every fixture parses
  into labeled, bounded observations — summaries always start with the
  "untrusted" prefix, ANSI escapes are stripped and C0 control
  characters escaped to visible `\xNN` forms (never raw in context),
  huge repeated output stays bounded with exact counts, malformed
  Unicode (lone surrogates, U+FFFD replacements, bidi overrides) never
  crashes a parser, and broken/poisoned documents surface as structured
  `malformed=True` / `parse_error` fields (fail loudly, never raised).
- model adapters (`ozzgraph.adapters`): completions embedding the
  fixtures parse into `ParsedAction` values where the injection is
  confined to `rationale` / `payload` / `raw`; the strict three-line and
  JSON protocols reject injected extra lines/keys loudly
  (`AdapterParseError`), and the executor's strict output contract
  rejects raw injected directive text (`MalformedOutputError`) so a
  directive embedded in target output can never become an executed
  action.
- flag extractor (`ozzgraph.flags`): fake flags are extracted only with
  observed provenance (a flag in output with no evidence edge is never a
  candidate), dedupe to exactly one candidate per string, and can only
  ever reach submission through the supervisor-only coordinator with a
  privileged client (`SubmissionPrivilegeError` for anything else).
- scope-policy gate (`ozzgraph.policy`): every public-internet
  suggestion (`evil.example.com`, bare public IPs, platform metadata
  endpoints) is rejected before execution with its typed
  `ScopeViolationError` subclass.

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

Implemented as PR29 in `tests/test_chaos.py` (injection mechanics:
monkeypatches and fakes only — no network, no real MCP; the only live
endpoints are loopback shell commands and in-memory/temporary state):

- model timeout / server 500: `httpx.MockTransport` handlers raise
  `ConnectTimeout`/`ReadTimeout` or return HTTP 500; the client retries
  with bounded backoff and raises the typed `ModelServiceError`
  (`status_code`, `retryable`), appending a `model_failure` event.
- MCP timeout / malformed response: the same transport injection against
  `HalClient` raises the typed `HalServiceError` (transport failures
  retryable, malformed JSON-RPC bodies and wrong-shaped results
  non-retryable parse failures) with a `hal_failure` event.
- process hang: the bounded shell runner kills the whole process group —
  a delayed marker in a background grandchild never appears, the runner
  is reusable afterwards, and no orphan survives.
- worker crash: a runner raising mid-task becomes a structured failed
  `worker_run` record (never a silent swallow), the dependent task still
  runs, and the schedule completes without hanging.
- disk full: an `ENOSPC` on the artifact store's content write raises
  `ArtifactStoreError`; an `ENOSPC` on the event log propagates — no
  silent artifact or event loss.
- SQLite lock: a locked database surfaces as `StateGraphError` (write
  and transaction paths), never a bare `sqlite3` exception.
- partial artifact write: a torn JSONL line aborts replay loudly
  (`ReplayMalformedEventError` — no silent skipping) and a corrupt
  artifact index is a loud `ArtifactIndexError`, never silently rebuilt.
- heartbeat failure: a failing summary callable or sleeper raises out of
  `Heartbeat.run()` — the emitter fails loudly instead of silently
  stopping.
- termination signals: `Supervisor.stop(INTERRUPTED)` — the
  SIGTERM/SIGINT path — appends a structured `termination` event
  (`reason: interrupted`) to the run log; subprocess-level signal
  delivery (SIGTERM/SIGINT → exit 130) stays covered by
  `tests/test_signals.py`.

## Loop and Timeout Detection

Implemented as PR29 in `tests/test_loop_detection.py` (plus the chaos
process-hang tests above). The harness abandons unbounded or repetitive
loops and recovers from timeouts without hanging:

- repetition detection: a looping model's identical proposal is rejected
  by the executor's fingerprint store (`DuplicateFingerprintError`, the
  action is never executed twice), a plan whose every step failed is
  abandoned (`PlanExhaustedError` — never retried forever), and the
  matrix layer measures a pure looping model as `repeated=True` from the
  second identical command with the repetition-rate metric, bounded by
  `max_turns`.
- action-budget abandonment: an executor turn records exactly one action
  entity (one model call); once a plan's `MAX_MODEL_CALLS_PER_PLAN`
  budget is exhausted the evaluator abandons/re-plans it — the plan
  never loops on a spent budget (step-attempt thresholds
  `MAX_ATTEMPTS_PER_STEP` are covered in `tests/test_evaluator.py`).
- timeout recovery: the bounded shell runner kills the hanging process
  group (`ToolResult.timeout_state`), the parser carries the timeout
  into the observation summary ("timed out"), the failed action is fed
  back with `reason="timeout"`, and the executor skips the dead plan
  step and continues with the next one — no hang, no infinite retry.

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

Optional dashboard (PR30):

```bash
cd dashboard
yarn lint
yarn typecheck
yarn test
yarn build
```

The dashboard is a self-contained Yarn + strict TypeScript project under
`dashboard/` (zero runtime dependencies; `node:http`, `node:sqlite`,
`node:crypto` only) that serves the Optional Dashboard API
(docs/API_AND_INTEGRATIONS.md) from real kernel run state, strictly
read-only. Setup: `corepack prepare yarn@stable --activate`, then
`yarn install`. Quality gates and coverage:

- `yarn lint` — ESLint (typescript-eslint recommended) over `src/` and
  `test/`.
- `yarn typecheck` — `tsc --noEmit` with `strict: true`,
  `noImplicitAny`, `noUncheckedIndexedAccess`, and friends.
- `yarn test` — vitest unit + end-to-end tests (86 tests): run
  discovery (subdirectory runs, root-as-run, artifact counting from the
  kernel's `artifacts.json` index or the directory), path-traversal
  rejection, events/metrics derivation (malformed lines skipped
  gracefully), graph reads (read-only enforcement verified against the
  live database), replay determinism, and the full HTTP API over a real
  listening server.
- `yarn build` — `tsc` emits `dist/` for `yarn start`.

Replay fidelity is cross-checked against the real kernel: the committed
fixture `dashboard/test/fixtures/kernel_events.jsonl` was produced with
`src/ozzgraph/replay.py`, and the TypeScript replay must reproduce the
kernel's `graph_hash` byte-for-byte (`dashboard/test/replay.test.ts`).
`graph.db` is opened with SQLite's read-only flag; the dashboard never
writes kernel state and is not part of the Python package or the
competition image (no dashboard dependency in `pyproject.toml`).

## Container Image Hardening (PR31)

The competition image (Dockerfile at the repository root) is gated in CI by
the `docker` job in `.github/workflows/ci.yml`:

- build the image with buildx;
- assert `docker image inspect` size < 1.5 GiB (`1500 * 1024 * 1024` bytes);
- smoke test 1 — `docker run --rm IMAGE --version` (ENTRYPOINT runs
  `python -m ozzgraph`);
- smoke test 2 — `docker run --rm --entrypoint halctl IMAGE --help`
  (halctl on PATH);
- smoke test 3 — `docker run --rm --entrypoint id IMAGE` must report the
  non-root operator (`uid=10001(ozzgraph)`, never `uid=0(root)`) — the
  immutable image's non-root property, asserted at runtime (PR32);
- smoke test 4 — a 2-second supervised run under `--read-only --tmpfs /tmp`
  (state on the `/var/lib/ozzgraph/state` volume) must terminate with exit
  code 3 (BUDGET_EXHAUSTED), must print the `USER ID:` identity line, and
  must end with the `TERMINATION: budget_exhausted` summary line (PR32) —
  proving the immutable (read-only rootfs, volume-mounted state) runtime
  starts and terminates cleanly end to end.

Non-Docker shape tests live in `tests/test_image_hardening.py` (Dockerfile
multi-stage/non-root/entrypoint shape, `.dockerignore` coverage, the shared
size-budget constant, CI wiring, SBOM script syntax) so `uv run pytest`
stays green on machines without Docker. Build recipe, minimization choices,
size/startup/memory measurements, SBOM generation, and fallback verification
are documented in docs/IMAGE_HARDENING.md (ADR-0007).
