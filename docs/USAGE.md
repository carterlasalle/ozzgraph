# OzzGraph Usage Guide

This guide covers end-to-end use of the OzzGraph harness as implemented
(version 1.0.0, spec-complete through PR32): installing, configuring, running a
capture, talking to the challenge platform through `halctl`, and understanding
the artifact store, event log, replay, executor loop, budgets, lifecycle, and
scheduler. For how the pieces work internally and how to customize them, see
[CUSTOMIZATION.md](CUSTOMIZATION.md); the component contracts live in
[API_AND_INTEGRATIONS.md](API_AND_INTEGRATIONS.md).

> Authorized use only. OzzGraph is designed for authorized, isolated security
> challenges (see [PRD.md](PRD.md) — public-internet reconnaissance is a
> non-goal and the scope policy fails closed on it).

## 1. Install

Requirements:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (dependency and lockfile management)

```bash
git clone <repo-url> ozzgraph
cd ozzgraph
uv sync          # installs the package + dev group (ruff, mypy, pytest, pyright)
uv run python -m ozzgraph --version   # ozzgraph 1.0.0
```

The package installs a `halctl` console script (`[project.scripts]` in
`pyproject.toml`) so `uv run halctl ...` works from the repo root; inside the
competition image `halctl` is on PATH.

## 2. Configure

All runtime configuration is **environment-driven** — there is no config file.
`uv run python -m ozzgraph` reads `load_config()` from `ozzgraph.config`, which
validates everything into a Pydantic v2 model and fails loudly on invalid
values. Secrets and model/MCP endpoints are *not* part of that model; they are
constructor-injected with environment fallback in `ozzgraph.model_client` and
`ozzgraph.hal_client`.

### 2.1 Core runtime knobs (`ozzgraph.config`)

| Variable | Default | Meaning |
|---|---|---|
| `HAL_USER_ID` | *(required)* | Operator identity; printed as the first stdout line (`USER ID: ...`) so the platform can attribute the run. |
| `OZZGRAPH_STATE_DIR` | `state` | Root directory for durable state (`actions.jsonl`, `graph.db`, `duplicates.jsonl`). |
| `OZZGRAPH_ARTIFACT_DIR` | `<state_dir>/artifacts` | Raw tool output / downloaded files. |
| `OZZGRAPH_HEARTBEAT_INTERVAL_S` | `30` | Seconds between `HEARTBEAT ...` progress lines. |
| `OZZGRAPH_MAX_RUNTIME_S` | `7200` | Wall-clock budget; the supervisor terminates with `budget_exhausted` when exceeded. |
| `OZZGRAPH_MAX_TOKENS` | `0` (unlimited) | Cumulative token budget across model calls. |
| `OZZGRAPH_MAX_MODEL_CALLS` | `0` (unlimited) | Cumulative model-call budget. |
| `OZZGRAPH_MAX_TOOL_CALLS` | `0` (unlimited) | Cumulative tool-call budget. |
| `OZZGRAPH_MAX_WORKERS` | `4` | Maximum concurrent scheduled tasks. |
| `OZZGRAPH_SPECIALISTS_ENABLED` | *(off)* | When set (any of `1`/`true`/`yes`/`on`), the supervisor composes the V07 specialist fleet into the runner: a pure independent-hypothesis decision dispatches a bounded parallel micro-agent batch with zero LLM calls instead of the StrategicPlanner (HAL-010, docs/adr/0009). Off by default — the V06 model path is unchanged. |
| `OZZGRAPH_MAX_HINTS` | `1` | Maximum paid hints the supervisor may purchase. |
| `OZZGRAPH_MAX_COMMAND_LENGTH` | `4096` | Ceiling for one command line (chars); longer commands are rejected by the scope policy. |
| `OZZGRAPH_TARGET_ALLOWLIST` | *(empty — fail closed)* | Comma-separated hosts/IPs/CIDRs commands may address. Empty means **no** external destination is permitted. |
| `OZZGRAPH_ALLOWED_COMMAND_FAMILIES` | `shell,recon,exploit` | Command families permitted at the policy level; phases and worker scopes narrow this per call. |
| `OZZGRAPH_FLAG_PATTERN` | `flag\{[^{}\s]+\}` | Regex the flag candidate extractor scans observations/artifacts with. |
| `OZZGRAPH_MAX_SUBMISSIONS` | `3` | Attempt cap — per flag candidate and in total. |
| `OZZGRAPH_SCOPE_FILE` | — | Optional scope file (JSON/YAML/TOML): allowlist entries merged deterministically into `OZZGRAPH_TARGET_ALLOWLIST` (V08, docs/adr/0010). |
| `OZZGRAPH_CREDENTIALS_FILE` | — | Optional credentials file (JSON/YAML/TOML): `{name, kind, username?, secret_env?}` references; secrets are read from the named env vars at runtime and never stored in the file (V08). |

### 2.2 Challenge platform (HalCTF MCP)

| Variable | Default | Meaning |
|---|---|---|
| `HAL_CTF_ID` / `HAL_CHALLENGE_ID` / `OZZGRAPH_CHALLENGE_ID` | — | Challenge id for the HalCTF runtime (first non-blank wins; V09 discovery, docs/adr/0011). Used by `halctl`, bootstrap (status, free hint), and the environment. |
| `OZZGRAPH_TARGET` | — | Single target for bootstrap reconnaissance. |
| `OZZGRAPH_TARGET_<NS>` | — | Namespaced targets, e.g. `OZZGRAPH_TARGET_HTTP`, `OZZGRAPH_TARGET_DNS` (namespaces `HTTP`/`HTTPS`/`DNS` select the probe category). |
| `OZZGRAPH_SMOKE_FLAG` | — | When set, submitted once at startup through the privileged client as a pipeline smoke test (requires a challenge id). |
| `OZZGRAPH_MCP_BASE_URL` | `http://127.0.0.1:9000/mcp` | JSON-RPC 2.0 MCP endpoint (base URL including the path) — the FIRST of the deterministic discovery candidates. |
| `HAL_MCP_ENDPOINT` / `HAL_ENDPOINT` / `MCP_ENDPOINT` | — | Additional endpoint candidates, consulted in that order after `OZZGRAPH_MCP_BASE_URL` (first non-blank wins). `OPENAI_BASE_URL` is NOT a candidate — it is the model service (`/llm`), not the MCP server. |
| `OZZGRAPH_MCP_TIMEOUT_S` | `60` | Per-request timeout. |
| `OZZGRAPH_MCP_MAX_RETRIES` | `3` | Bounded retries on transient failures (429/5xx/transport; max 10). |
| `OZZGRAPH_HAL_PRIVILEGED` | — | When set, `halctl` privileged operations (submit, paid hints, exit) are allowed. Only the supervisor sets this. |
| `OZZGRAPH_SIDECAR_BASE_URL` | MCP origin, else `http://127.0.0.1:9000` | The real competition sidecar's plain-HTTP root (HAL-004): `POST /submit` + `POST /done`. Env-first discovery — an explicit value wins, then the ORIGIN of the resolved MCP endpoint (the sidecar shares the MCP host:port in the real deployment: `MCP_ENDPOINT=http://127.0.0.1:9000/mcp` -> `http://127.0.0.1:9000`), then the localhost default. `OPENAI_BASE_URL` is never consulted (it is the model service). |
| `OZZGRAPH_SIDECAR_TIMEOUT_S` | `60` | Per-request timeout for sidecar `/submit` + `/done`. |
| `OZZGRAPH_SIDECAR_MAX_RETRIES` | `3` | Bounded retries on transient sidecar failures (429/5xx/transport; max 10). |

HalCTF mode is selected when ANY HalCTF runtime variable is set
(`HAL_CTF_ID`, `HAL_CHALLENGE_ID`, `HAL_ENDPOINT`, `HAL_MCP_ENDPOINT`,
`MCP_ENDPOINT`, or `OZZGRAPH_CHALLENGE_ID`). With none of them set the
run is a **local assessment** (V08 `OZZGRAPH_TARGET` classification).
`HAL_USER_ID` is identity and never selects HalCTF mode. The MCP
endpoint is **optional** (HAL-002): an env-only detonation with
platform-injected `HAL_TARGET_*` services and `HAL_CHALLENGE_*`
metadata starts without one — MCP is enrichment/fallback. Set one of
the endpoint candidates above to enable MCP features (bootstrap
status, smoke flag, submissions, hints, scoreboard).

**Sidecar submission transport (HAL-004):** the real competition
sidecar speaks PLAIN HTTP at the MCP host:port's root (not JSON-RPC):
`POST /submit` with `{"challenge_id", "flag"}` yields
`{"status": "correct", "points_awarded": 1}`, and `POST /done` signals
run teardown. `SidecarSubmissionClient` (via the environment's
`sidecar_submission_client()` factory) is the transport adapter at that
boundary: it normalizes every observed response form into the internal
`SubmissionResult` schema (status strings `correct`/`accepted`/`solved`/
`success`/`already_solved`, boolean verdict fields, and points > 0 all
accept deterministically), retries bounded on transient failures, and
enforces the same supervisor-only privilege boundary as the MCP client
(`submit_flag` and `done` require `OZZGRAPH_HAL_PRIVILEGED`). `/done` is
best-effort — failures are recorded as `sidecar.done_failed` events and
never fail the run.

### 2.3 Model endpoint (OpenAI-compatible)

| Variable | Default | Meaning |
|---|---|---|
| `OZZGRAPH_MODEL_BASE_URL` | `http://127.0.0.1:8000/v1` | Base URL including the API prefix (`GET /models`, `POST /chat/completions`). |
| `OZZGRAPH_MODEL_API_KEY` | — | Optional bearer token. |
| `OZZGRAPH_MODEL_TIMEOUT_S` | `60` | Per-request timeout. |
| `OZZGRAPH_MODEL_MAX_RETRIES` | `3` | Bounded exponential-backoff retries (max 10). |

**HalCTF model routing (HAL-003):** on the live competition platform
`OPENAI_BASE_URL` maps the model client base URL (`http://127.0.0.1:9000/llm`)
and `HAL_AGENT_MODEL` maps the model id (`google/gemma-4-26b-a4b-it-maas`) —
both override the `OZZGRAPH_MODEL_*` / defaults above in HalCTF mode. When
either is absent the run degrades gracefully to `OZZGRAPH_MODEL_ID` /
`OZZGRAPH_MODEL_BASE_URL` / the defaults. Local mode is unchanged.

### 2.4 Minimal working example

```bash
export HAL_USER_ID=team-42
export OZZGRAPH_CHALLENGE_ID=challenge-01
export OZZGRAPH_TARGET=http://127.0.0.1:8000
export OZZGRAPH_TARGET_ALLOWLIST=127.0.0.1
export OZZGRAPH_MODEL_BASE_URL=http://127.0.0.1:8000/v1
```

## 3. Run a capture

```bash
uv run python -m ozzgraph
```

Startup sequence (all deterministic, no model involvement until the loop):

1. **Identity** — `USER ID: team-42` is the first stdout line.
2. **Runtime directories** — `state/` and `state/artifacts/` are created
   idempotently; the run log `state/actions.jsonl` and the artifact store are
   opened; a `bootstrap` event records the run identity and budget.
3. **Heartbeat** — a task starts emitting `HEARTBEAT ...` lines every
   `OZZGRAPH_HEARTBEAT_INTERVAL_S` (the summary carries
   `runtime_left=...s`).
4. **Deterministic bootstrap reconnaissance** (`ozzgraph.bootstrap`,
   [ADR-0002](adr/0002-bootstrap-events-and-probes.md)) — parses
   `OZZGRAPH_TARGET*` variables; retrieves challenge status when a challenge id
   is set; submits `OZZGRAPH_SMOKE_FLAG` when provided; requests **free hint
   zero** (`hint.request index=0`, free and not privileged); validates target
   reachability with fixed, policy-gated probes (`curl` for HTTP/HTTPS, `dig`
   for DNS) through the bounded shell runner. Hal service failures are recorded
   as events and are not fatal; configuration errors abort with `FAILED`.
5. **Main loop** — polls budgets until one is exhausted
   (`BUDGET_EXHAUSTED`, exit code `3`) or `SIGTERM`/`SIGINT` requests a
   graceful stop (`INTERRUPTED`, exit code `130`).

Every terminal path appends a structured `termination` event to the run log and
prints a human-readable summary as the **final stdout line**
(`TERMINATION: completed | interrupted | failed | budget_exhausted`).
Exit codes (local mode — no HalCTF runtime variable): `0` completed, `1`
failed (e.g. configuration error), `130` interrupted, `3` budget exhausted.

In **HalCTF mode** (any `HAL_CTF_ID` / `HAL_CHALLENGE_ID` / `HAL_ENDPOINT` /
`HAL_MCP_ENDPOINT` / `MCP_ENDPOINT` / legacy `OZZGRAPH_CHALLENGE_ID` set,
docs/adr/0012) the process boundary flattens: every run that reaches a
structured termination — scored, unsolved, budget-exhausted, gave-up, or a
graceful stop — exits `0`, because the event platform interprets a nonzero
container exit as a crash and reruns the detonation. Only startup-impossible
configuration errors (e.g. missing `HAL_USER_ID`, a set-but-invalid
`HAL_TARGET_PORT`) and uncaught exceptions exit `1`. The full reason is always
preserved in the run log's `termination` event and the `TERMINATION:` line.

> Component wiring note: the executor loop, planner, evaluator, and scheduler
> are implemented as standalone, fully-tested components (PR20–PR26) with the
> supervisor exposing the privileged integration surfaces
> (`Supervisor.submit_verified_candidate`, `Supervisor.request_paid_hint`).
> Driving them from the supervisor idle loop is the remaining integration
> step; everything below documents the components as implemented.

## 4. `halctl` — the model-facing adapter CLI

`halctl` wraps the HalCTF MCP integration behind a local terminal-native CLI.
**Models never call raw MCP** (AGENTS.md invariant 5); `halctl` is the only
adapter surface. Every subcommand prints **exactly one JSON document** to
stdout (deterministic key order) and exits non-zero on failure.

| Subcommand | Privileged? | Notes |
|---|---|---|
| `ctfs` | no | List the available competitions (V09). |
| `challenges [--ctf-id <id>]` | no | List challenges, optionally narrowed to one competition (V09). |
| `challenge show --challenge-id <id>` | no | Normalized challenge details. |
| `status --challenge-id <id>` | no | Challenge status (solved, attempts, hints used, points, smoke flag, scoring). |
| `submit --flag <flag> --challenge-id <id>` | **yes** | Submit a flag. |
| `hint --index <n> --challenge-id <id>` | **yes if n > 0** | Hint zero is free; paid hints are supervisor-only. |
| `scoreboard` | no | Competition scoreboard. |
| `exit --reason <reason>` | **yes** | Graceful exit. |

The challenge id may be passed as `--challenge-id` or set once via
`OZZGRAPH_CHALLENGE_ID` (missing id → usage error, exit code `2`).

```bash
uv run halctl challenge show --challenge-id "$OZZGRAPH_CHALLENGE_ID"
uv run halctl status
uv run halctl submit --flag 'flag{let-me-in}' --challenge-id "$OZZGRAPH_CHALLENGE_ID"
uv run halctl hint --index 0
uv run halctl scoreboard
```

Privileged operations fail with a normalized JSON error document and exit code
`1` unless `OZZGRAPH_HAL_PRIVILEGED` is set — the supervisor runs the adapter
with it; models run it without. Exit codes: `0` success, `1` operational
failure (`HalServiceError`, `HalPrivilegeError`, config `ValueError`), `2`
usage failure.

The underlying wire protocol is JSON-RPC 2.0
(`ctf.list`, `challenge.list`, `challenge.get`, `challenge.status`,
`flag.submit`, `hint.request`, `scoreboard.get`, `exit`) with bounded
retries and a normalized, contract-versioned internal schema — see
[API_AND_INTEGRATIONS.md](API_AND_INTEGRATIONS.md) § HalCTF Integration.

## 5. Artifact store, event log, and replay

### 5.1 Artifact store (`ozzgraph.artifacts`)

Raw tool output and downloaded files live **outside model context**
(AGENTS.md rule #1). Layout of one store rooted at `<state_dir>/artifacts`:

- `artifacts.json` — the authoritative metadata index (artifact id → record:
  sha256 hash, MIME type, size, source action, target, timestamps, truncation
  flag, parser metadata, sensitivity). Updated atomically (temp file +
  `os.replace`); a missing/corrupt index raises `ArtifactIndexError` — it is
  never rebuilt on demand.
- `<artifact_id>` — raw content files. When no id is supplied, content is
  stored under its own sha256 digest (content-addressed), so identical bytes
  dedupe naturally.

### 5.2 Event log (`ozzgraph.events`)

`<state_dir>/actions.jsonl` is the append-only structured run log. Every event
is one JSON line: `event_id`, `run_id`, UTC `timestamp` (ISO-8601),
`event_type`, `producer`, `schema_version`, optional `task_id`/`worker_id`, and
a free-form `payload`. Reopening the log appends; it never truncates or
rewrites (ADR-0001). Event families include:

- `bootstrap` / `termination` — lifecycle (producer `supervisor`).
- `bootstrap.*` — target parse, challenge status, smoke submission, free hint,
  reachability, probe runs (producer `bootstrap`).
- `graph.entity_created|updated|deleted`, `graph.edge_created|deleted` — every
  graph mutation, mirrored (producers `executor`, `scheduler`, `reducer`,
  `submissions`, `hints`, ...).
- `executor.action_attempted`, `executor.plan_persisted` — the executor loop.
- `submission.*`, `hint.*`, `flags.candidate_found` — flag/hint lifecycle.
- `scheduler.*`, `reducer.*` — worker scheduling and findings merge.
- `model_failure`, `hal_failure` — bounded-retry failures from the clients.

`<state_dir>/duplicates.jsonl` is a second append-only log: the fingerprint
store mirrors every approved action fingerprint there (the duplicate gate is a
loop-prevention heuristic, not a semantic-equivalence oracle).

### 5.3 Replay (`ozzgraph.replay`)

Replaying all `graph.*` events in file order reconstructs the **same entity
set, edge set, and graph hash** as the live run, preserving the schema version
(fresh databases run the standard migrations; the hash includes
`schema_version`). Non-graph event types are ignored, so new event kinds never
break replay. A malformed graph event raises loudly. This invariant is covered
by `tests/test_replay.py` and the golden-trace verifier
([GOLDEN_TRACES.md](GOLDEN_TRACES.md)).

The dashboard replays runs read-only and reproduces the kernel graph hash
byte-for-byte ([API_AND_INTEGRATIONS.md](API_AND_INTEGRATIONS.md) § Optional
Dashboard API).

## 6. Executor loop — one bounded action per turn

The executor (`ozzgraph.executor`, PR20) is the deterministic loop between the
phase router and the planner. One `Executor.turn()`:

1. **Budget check** — raises `BudgetExceeded` loudly if runtime, tokens, model
   calls, or tool calls are exhausted; then consumes one model call.
2. **Route** — `PhaseRouter.route(graph)` returns the next phase from
   graph-state predicates (see [CUSTOMIZATION.md](CUSTOMIZATION.md) § Phase
   routing).
3. **Plan** — `Planner.plan(graph, route)` returns a bounded, ranked plan when
   the graph is in a branching state, else a typed `NoPlanDecision`.
4. **Validate the model's proposal** — model output is untrusted and must be a
   JSON object (or mapping) with exactly `action` (1–4096 chars) and
   `skill_id`; anything else is `MalformedOutputError` (never coerced or
   repaired).
5. **Resolve the skill** — the selected skill must exist, cover the routed
   phase, and (when a plan step is bound) match the step's assigned skill
   (`InvalidSkillError` otherwise).
6. **Policy gate** — `ScopePolicy.check()` enforces length, allowlist,
   platform/public-internet blocks, and phase/family permissions (see
   [CUSTOMIZATION.md](CUSTOMIZATION.md) § Scope policy).
7. **Duplicate rejection** — fingerprints from previously failed actions are
   never retried, and the fingerprint store rejects repeats
   (`DuplicateFingerprintError`); a plan whose every step failed raises
   `PlanExhaustedError` instead of looping.
8. **Consume one tool call**, persist the plan entities the first time a plan
   id is seen, and record the attempt **before execution** — an `action`
   graph entity keyed `action-<fingerprint>` plus an
   `executor.action_attempted` event.

The turn returns **exactly one** typed `ActionRequest` — never a list — with
the action text, skill id, the skill's default timeout, an output limit
(65536 chars/stream by default), the policy fingerprint, the routed phase, and
plan bindings (plan id / step id / hypothesis id). Multi-command plans
disguised as one action are governed by the skill cards' bounded scripts and
the action-length bound, never silently unbound. Failed actions never retry;
the tool plane attaches observations to the recorded action entity later.

## 7. Budgets, heartbeat, and lifecycle

- **Budgets** (`ozzgraph.budgets`): deterministic trackers for runtime, tokens,
  model calls, tool calls, worker concurrency, and paid hints. Zero means
  unlimited for the cumulative dimensions; `Budgets.consume_hint` raises
  `BudgetExceeded` rather than over-spending (AGENTS.md invariant: paid hint
  count never exceeds the configured maximum).
- **Heartbeat** (`ozzgraph.heartbeat`): plain asyncio task printing a
  `HEARTBEAT ...` line at the configured interval until stopped — an external
  observer can tell the process is alive.
- **Lifecycle** (`ozzgraph.supervisor`, PR2/PR3): signal handlers for
  `SIGTERM`/`SIGINT` are installed before startup so early signals are caught;
  every terminal path appends a structured `termination` event and prints
  `TERMINATION: <reason>` as the final line (AGENTS.md rule 9).
- **Privileged surfaces**: only the supervisor constructs a privileged
  `HalClient`. `Supervisor.submit_verified_candidate(graph)` drives the
  submission coordinator (provenance validation + attempt budgets, PR22);
  `Supervisor.request_paid_hint(graph, index)` drives the deterministic
  paid-hint gate (PR23, [ADR-0003](adr/0003-hint-policy.md)).

## 8. Scheduling and the task DAG

The scheduler (`ozzgraph.scheduler`, PR24; [ADR-0004](adr/0004-task-dag-scheduler.md))
parallelizes evidence gathering without breaking invariants:

- **Tasks** carry explicit `depends_on` references and a **conflict-key** set.
  Tasks with overlapping conflict keys are mutually exclusive; tasks with no
  keys conflict with nothing. DAG construction validates duplicates, missing
  dependencies, and cycles loudly (`TaskDAGError`).
- **Deterministic order**: `TaskDAG.ready_order()` returns dependency-complete
  tasks sorted by stable id; the scheduler starts at most
  `OZZGRAPH_MAX_WORKERS` tasks concurrently, only dependency-complete,
  non-conflicting ones.
- **Serialization**: the reserved `SERIALIZED_CONFLICT_KEY` makes a task
  conflict with every other task — flag submission and paid hints are always
  serialized (`serialized_task()` is the supervisor hook).
- **Findings**: each scheduled task produces one `WorkerRun`
  (`worker-run-<fingerprint>`) with typed `Finding`s that MUST carry at least
  one evidence/artifact reference — a finding without evidence is model prose
  and is rejected loudly.
- **Specialist workers** (`ozzgraph.workers`, PR25; [ADR-0005](adr/0005-worker-scopes.md)):
  declarative `WorkerScope`s (command families, phases, mutation permission,
  optional target narrowing) validated at construction; a worker refuses
  out-of-scope tasks and commands before any execution. `exploit` is the only
  mutating family — read-only workers (recon/shell) can never run it, so
  evidence gathering parallelizes while exploit chains serialize.
- **Reducer** (`ozzgraph.reducer`, PR26): validates each finding's evidence
  references against graph evidence entities and the artifact-store index,
  then merges it as an authoritative `fact` entity
  (`fact-<sha256(fingerprint)>`, idempotent, `FACT DERIVED_FROM EVIDENCE`
  edges). Unresolvable evidence is rejected loudly and never written; failed
  runs (which carry no findings) are skipped.

Everything the scheduler/reducer persists is mirrored as `graph.*` events, so
replay reconstructs the identical graph hash.

## 9. Related documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — component overview and phase model.
- [API_AND_INTEGRATIONS.md](API_AND_INTEGRATIONS.md) — full component
  contracts, wire protocols, event tables, dashboard API.
- [DATA_STRATEGY.md](DATA_STRATEGY.md) — entity/edge conventions and state
  layout.
- [TECHNICAL_REQUIREMENTS.md](TECHNICAL_REQUIREMENTS.md) — bootstrap, hint
  policy, and flag submission requirements.
- [CUSTOMIZATION.md](CUSTOMIZATION.md) — profiles, adapters, skills, policy,
  workers, routing.
- [TESTING_AND_QA.md](TESTING_AND_QA.md), [GOLDEN_TRACES.md](GOLDEN_TRACES.md),
  [SYNTHETIC_LAB.md](SYNTHETIC_LAB.md) — quality infrastructure.
- [RELEASE.md](RELEASE.md) — v1.0 release candidate checklist and rehearsal.
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the 32-PR sequence.
