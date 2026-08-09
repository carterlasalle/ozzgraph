# ADR-0011: HalCTF Adapter — Discovery, Tool Set, and Kernel Decoupling

Status: accepted

Date: 2026-08-08

## Context

Milestone 9 of the v2 plan (docs/CHANGES_v2.md, "v2/halctf-adapter")
completes the HalCTF runtime adapter behind the generic
`EnvironmentAdapter` protocol (docs/adr/0008). V01 deliberately left the
adapter MINIMAL: no scoreboard, no hints, no submissions, no smoke
flags — "the full HalCTF adapter is milestone 9". Four things stand
between the V01 slice and a complete adapter:

1. **Discovery.** HalCTF mode is selected by the operator's environment,
   but the runtime endpoint can arrive under several variable families
   (`OZZGRAPH_MCP_BASE_URL`, `HAL_MCP_ENDPOINT`, `HAL_ENDPOINT`,
   `MCP_ENDPOINT`, `OPENAI_BASE_URL`), and nothing fails loudly when
   HalCTF mode is selected without an endpoint.
2. **The official tool set.** The platform's MCP surface is
   `list_ctfs`, `challenges`, `status`, `submit_flag`, `request_hint`,
   `scoreboard`. `hal_client` was missing the two list tools, and the
   status/hint schemas did not carry smoke flags, scoring, or hint
   costs.
3. **Kernel contamination.** The generic kernel still owned
   CTF-specific modules: `ozzgraph.hints` (HintPolicy /
   HintCoordinator), `ozzgraph.submissions` (SubmissionCoordinator), and
   `ozzgraph.flags` (FlagCandidateExtractor) sat at the kernel root and
   were imported directly by `supervisor.py` (and `ozzgraph.flags` by
   `reducer.py` / `specialists.py`). CHANGES_v2.md calls for
   "Hints/submissions fully out of the kernel".
4. **Graceful completion.** A HalCTF run must be able to complete
   gracefully — objective satisfied -> termination COMPLETED -> V08
   report bundle — with zero HalCTF knowledge in the kernel.

## Decision

### 1. Deterministic env-based discovery (config + adapter)

`ozzgraph.config` owns the discovery vocabulary (V09):

- HalCTF mode is selected by the presence of any HalCTF runtime
  variable: `HAL_CTF_ID`, `HAL_CHALLENGE_ID`, `HAL_ENDPOINT`,
  `HAL_MCP_ENDPOINT`, `MCP_ENDPOINT`, or the legacy
  `OZZGRAPH_CHALLENGE_ID` (`halctf_mode_selected`).
- The challenge id is the first non-blank of `HAL_CTF_ID` /
  `HAL_CHALLENGE_ID` / `OZZGRAPH_CHALLENGE_ID`
  (`discover_halctf_challenge_id`).
- The MCP endpoint is the first non-blank of the ordered candidates
  `OZZGRAPH_MCP_BASE_URL` / `HAL_MCP_ENDPOINT` / `HAL_ENDPOINT` /
  `MCP_ENDPOINT` (`discover_halctf_endpoint`). `OPENAI_BASE_URL` is
  deliberately NOT a candidate — it is the model service (`/llm`), not
  the MCP server (`/mcp/`) — and it never selects HalCTF mode itself
  (it is a model endpoint in local mode).
- `HAL_USER_ID` is operator identity, required for EVERY run (local
  included) — it is deliberately NOT a mode selector, so the local
  default is unchanged (docs/adr/0010: with no HalCTF runtime variable
  the run uses `LocalEnvironment` and the V08 `OZZGRAPH_TARGET`
  classification stays authoritative).

**Fail loudly** (HAL-002 amendment, 2026-08-09): the MCP endpoint is
OPTIONAL. An env-only detonation — platform-injected `HAL_TARGET_*`
services and `HAL_CHALLENGE_*` metadata, no endpoint — starts without
one: `load_config` and `HalCTFEnvironment.__init__` construct with
`endpoint=None` and MCP stays enrichment/fallback.
`require_halctf_endpoint` remains the loud helper (`ConfigError`) for
callers that genuinely need the endpoint (explicit submission /
HalClient construction), so fail-loud is preserved for truly
unrecoverable configuration (e.g. a set-but-invalid `HAL_TARGET_*`
port). The supervisor maps any construction `ConfigError` to a
structured `FAILED` termination.

`hal_client` resolves its base URL through the same discovery
(`ozzgraph.config.discover_halctf_endpoint`, falling back to the
localhost default for standalone `halctl` use), so the client a
supervisor run constructs always targets the discovered endpoint.

### 2. Official tool set

`hal_client` exposes all six official tools, keyed by
`OFFICIAL_HALCTF_TOOLS` (tool name -> method):

| Tool | Method | Wire method |
|---|---|---|
| `list_ctfs` | `list_ctfs` | `ctf.list` |
| `challenges` | `list_challenges([ctf_id])` | `challenge.list` |
| `status` | `get_status` | `challenge.status` |
| `submit_flag` | `submit_flag` | `flag.submit` |
| `request_hint` | `request_hint` | `hint.request` |
| `scoreboard` | `get_scoreboard` | `scoreboard.get` |

Privileged enforcement is unchanged (AGENTS.md rule 5/7 + data
invariants): `submit_flag`, paid `request_hint` (`index > 0`), and
`graceful_exit` remain supervisor-only (`HalPrivilegeError`);
`list_ctfs` / `list_challenges` / `get_status` / `get_scoreboard` and
free hint zero are read-only and open. `halctl` gains `ctfs` and
`challenges` subcommands, and the HalctlJsonParser classifies the new
document shapes.

### 3. Smoke flag, scoring, hint costs

- `ChallengeStatus` gains `smoke_flag: bool` and `scoring: Scoring`
  (max points, solves, first blood, hint penalty) — the smoke-flag
  signal and deterministic scoring ride the status the environment and
  bootstrap already fetch (no extra calls). Bootstrap records both in
  its `bootstrap.challenge_status` event.
- `HintResult` gains `cost: int | None` — the platform-reported per-hint
  price. Paid-hint enforcement stays the deterministic gate:
  `HintPolicy` + `HintCoordinator` keep the max-paid-hint-count
  invariant (the persisted `hint_purchase` count never exceeds
  `config.max_hints`) and the supervisor-only purchase path.
- The HalCTF environment wires the smoke-flag/scoring/hint-cost data
  and exposes the service factories below, so a HalCTF driver (bootstrap,
  the supervisor, or a future loop driver) consumes them without
  importing adapter internals.

### 4. Kernel decoupling — hints/submissions/flags/scoreboard move

The HalCTF-owned modules move OUT of the generic kernel:

- `ozzgraph/hints.py` -> `ozzgraph/environments/halctf/hints.py`
- `ozzgraph/submissions.py` -> `ozzgraph/environments/halctf/submissions.py`
- `ozzgraph/flags.py` -> `ozzgraph/environments/halctf/flags.py`
- new: `ozzgraph/environments/halctf/scoreboard.py`
  (`ScoreboardCoordinator` — the scoreboard tool's supervisor-side
  service; read-only, records a bounded `scoreboard.retrieved` run
  event, never persists live leaderboard data to the graph)
- new: `ozzgraph/environments/halctf/environment.py` holds the
  `HalCTFEnvironment` adapter (moved from `environments/halctf.py`),
  now with V09 discovery and service factories:
  `flag_extractor()`, `submission_coordinator()`, `hint_coordinator()`,
  `scoreboard_coordinator()` — each wired to the environment's
  discovered challenge id and the config's budgets.
- `ozzgraph/environments/halctf/__init__.py` is the shim the kernel
  imports (supervisor: `from ozzgraph.environments.halctf import
  HintCoordinator, SubmissionCoordinator, ...`).
- The generic entity vocabulary shared with the kernel
  (`observation`, `evidence`,
  `EVIDENCE EXTRACTED_FROM OBSERVATION`) moves to a new kernel module
  `ozzgraph/entities.py`; `reducer.py` and `specialists.py` import
  `ENTITY_EVIDENCE` from there.

**Invariant**: after this change, no module outside
`ozzgraph.environments` imports `ozzgraph.hints` /
`ozzgraph.submissions` / `ozzgraph.flags` (all deleted) or reaches into
`ozzgraph.environments.halctf.<module>` directly — the package shim and
the environment's service factories are the only surfaces. A test
(`tests/test_halctf_adapter.py::test_kernel_never_imports_moved_halctf_modules`)
greps the source tree to enforce it.

### 5. Graceful completion

Completion stays on the generic DONE paths (docs/adr/0008): the HalCTF
objective's `success_hint` names the deterministic signal ("submission
accepted for the challenge"), an accepted submission routes the graph
DONE via the router's `has_accepted_submission` predicate, the runner
completes the objectives, terminates COMPLETED, and renders the V08
report bundle. No kernel HalCTF knowledge is added.

## Consequences

Easier:

- Adding another runtime (Docker, Git repo, vulnerable VM) remains one
  adapter behind the protocol; HalCTF behavior is fully contained under
  `ozzgraph.environments.halctf`.
- The supervisor's privileged surfaces are unchanged in shape
  (`submit_verified_candidate`, `request_paid_hint`) but now build
  their coordinators through the environment when one is active, so the
  discovered challenge id and config budgets are applied in one place.
- Discovery is deterministic and validated: a platform-injected
  `HAL_*`/`MCP_ENDPOINT` environment needs no OzzGraph-specific
  variable beyond the challenge id, and the MCP endpoint is optional
  (HAL-002) — env-derived challenge metadata alone starts a run.

Harder:

- `ozzgraph.hints` / `ozzgraph.submissions` / `ozzgraph.flags` are
  deleted — any external code importing them must move to
  `ozzgraph.environments.halctf`. The package re-exports the full
  public surface, so the rename is mechanical.
- The moved modules still import kernel modules (`config`, `events`,
  `state_graph`, `router`, `executor`, `evaluator`, `hal_client`) — the
  decoupling is one-directional (kernel never imports adapter
  internals), which is the invariant AGENTS.md and CHANGES_v2.md
  require.
