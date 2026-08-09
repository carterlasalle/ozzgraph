# ADR-0008: Generic Runtime — Environment Adapter, Phase Set, AutonomousRunner

Status: accepted

Date: 2026-08-07

## Context

V01 of the v2 milestone plan (docs/CHANGES_v2.md, milestone 1
"v2/generic-runtime") pivots OzzGraph from a CTF/flag agent into a
general autonomous security-research harness that happens to support
HalCTF as one runtime adapter. Three decisions fall out of that pivot:

1. **A runtime environment abstraction.** The harness must drive any
   authorized assessment surface (local targets, Docker Compose stacks,
   Git repositories, HalCTF challenges) through one contract, so the
   kernel never imports HalCTF/CTF concepts directly.
2. **A generic phase set.** FLAG_HUNT and VERIFY_AND_SUBMIT are CTF
   behaviors, not kernel lifecycle. The kernel phases must end at the
   generic lifecycle (BOOTSTRAP/RECON/ENUMERATION/EXPLOITATION/
   POST_EXPLOITATION/PIVOT/REPLAN/DONE), and the DONE predicate must be
   generic ("all objectives completed") rather than flag-centric.
3. **A real investigate loop.** v1's `Supervisor.run()` bootstraps then
   idles (`while ...: await asyncio.sleep(0.25)`). CHANGES_v2.md calls
   this the "most important fix": the supervisor must actually DRIVE
   the agent through route → plan → context → one model action →
   execute → persist → evaluate.

These decisions change a core model interaction protocol (the kernel's
environment contract) and the phase/transition semantics — both ADR
triggers per AGENTS.md.

## Decision

We will implement `ozzgraph.environments` (the adapter contract and the
V01 concrete adapters), remove FLAG_HUNT / VERIFY_AND_SUBMIT from the
generic kernel, add `ozzgraph.runner.AutonomousRunner` (the real
investigate loop), and wire `Supervisor.run()` to drive it.

- **EnvironmentAdapter protocol** (`ozzgraph.environments.base`): five
  async methods — `discover_scope() -> Scope`,
  `discover_targets() -> list[Target]`,
  `discover_objectives() -> list[Objective]`,
  `discover_capabilities() -> set[str]`, and `aclose()`. The models
  (`Scope`, `Target`, `Objective`) are Pydantic v2 with
  `extra="forbid"`. The protocol is a plain `typing.Protocol`, NOT
  `@runtime_checkable`: an isinstance check on an async protocol only
  verifies that methods EXIST, never that they are coroutine functions,
  so a broken adapter could pass a runtime check and fail loudly
  mid-run. The harness constructs concrete adapters explicitly and mypy
  enforces the structural contract (same convention as
  `bootstrap.ProbeRunner`).

- **V01 concrete adapters**:
  - `LocalEnvironment` — deterministic local assessment derived from
    `OzzGraphConfig` and the operator's `OZZGRAPH_TARGET*` variables
    (parsed with the existing validated `bootstrap.load_targets`
    parser, one source of truth; malformed variables raise
    `ConfigError` loudly). The authorized surface IS
    `config.target_allowlist` (the same allowlist the policy gate
    enforces, so scope data and the gate can never disagree). One
    generic objective; conservative capabilities
    `{"http.request", "network.probe", "filesystem.read"}` until V03's
    tool-runtime.
  - `HalCTFEnvironment` — MINIMAL V01 slice: reads the existing
    `OZZGRAPH_CHALLENGE_ID` / config, yields exactly one Target (the
    challenge) and one Objective ("obtain and submit the flag",
    expressed as an Objective, NOT a kernel phase). Scoreboard, hints,
    submissions, and smoke flags are deliberately NOT ported — the full
    HalCTF adapter is milestone 9 (docs/adr/0011 completes it: HAL_*
    discovery, the official tool set, smoke flag, scoring, hint costs,
    graceful completion, and the hint/submission/flag/scoreboard
    services moved out of the generic kernel into
    `ozzgraph.environments.halctf`).

- **Phase set change**: `Phase.FLAG_HUNT` and `Phase.VERIFY_AND_SUBMIT`
  are removed from the generic kernel (phases, router transitions,
  policy phase families, skill packs, worker scopes). Flag hunting and
  submission become HalCTF environment behaviors owned by the full
  HalCTF adapter (V09); the kernel keeps the supervisor-only privileged
  submission surface (`Supervisor.submit_verified_candidate`) as-is for
  the HalCTF path.

- **Generic DONE predicate**: the router's terminal transitions are
  `has_accepted_submission` (kept — the HalCTF submission path) and the
  new `all_objectives_completed`: DONE when every seeded `objective`
  graph entity is `completed: true`. Objectives are seeded into the
  authoritative SQLite graph from the environment adapter and flipped
  to completed ONLY through deterministic paths (an accepted submission
  routed DONE, or an evaluator COMPLETE verdict the environment accepts
  as satisfying its objectives — HAL-006 adds the environment-specific
  `verdict_satisfies_objectives` predicate, so a validated hypothesis
  alone never completes a HalCTF objective) — never because a model
  claimed completion.

- **AutonomousRunner** (`ozzgraph.runner`): the real investigate loop.
  Constructor takes config, the state graph, the event log, the
  artifact store, budgets, an `EnvironmentAdapter`, and the
  supervisor's stop event. The loop per iteration: check stop / budget
  / objectives → route via the existing `PhaseRouter` → plan via the
  existing `Planner` ONLY when the graph is branching → compile bounded
  context via the existing context compiler → ONE bounded model action
  (existing `ModelService` + adapters + `Executor`; one action per turn
  per AGENTS.md rule #4) → execute through the policy gate + bounded
  shell runner → persist RAW output to the artifact store first, then
  observation/evidence entities (satisfying the data invariants) →
  consult the existing `Evaluator` when a plan exists. The loop never
  sleeps; the only awaits are the component calls. Model/executor/
  policy failures are recorded as structured `runner.*` events and the
  loop continues (budgets bound it, mirroring the bootstrap's
  non-fatal-service convention); budget exhaustion, supervisor stop,
  all-objectives-completed, and unexpected kernel errors terminate with
  a structured `RunnerStatus`.

- **Supervisor wiring**: `Supervisor.run()` keeps identity print,
  runtime-dir init, heartbeat, budgets, signal handling, and the
  structured termination event + reason, then drives
  `AutonomousRunner` instead of the idle sleep loop. Environment
  selection is deterministic: HalCTF when `OZZGRAPH_CHALLENGE_ID` is
  configured (bootstrap stays wired for HalCTF), else
  `LocalEnvironment`, whose discovered scope/targets/objectives are
  printed as the local-mode bootstrap summary (the local environment
  has no privileged HalCTF surface to bootstrap against). Runner status
  maps to the existing `TerminationReason` (COMPLETED / INTERRUPTED /
  FAILED / BUDGET_EXHAUSTED).

## Consequences

Easier:

- The kernel is environment-agnostic: adding a new runtime (Docker
  Compose, Git repo, vulnerable VM) is one adapter behind the protocol,
  with zero kernel changes — the milestone-2 "vertical slice" target.
- The supervisor finally drives the agent: `ozzgraph run <target>` has
  a real, observable investigate loop (route → act → persist →
  evaluate) instead of an idle poll.
- Objectives are first-class, typed state: the DONE predicate is a
  deterministic graph predicate shared by the router and the runner,
  and the halctf environment expresses "obtain and submit the flag" as
  data, not as a kernel phase.
- V01 reuses every v1 component unchanged (router/planner/executor/
  evaluator/context/adapters/policy/shell/artifacts) — no rewrite, no
  new dependencies.

Harder:

- The loop's completion paths are still narrow in V01: objectives
  complete via the accepted-submission DONE route or an evaluator
  COMPLETE verdict the environment accepts (HAL-006: local always
  accepts it, HalCTF only with an accepted submission), and the
  evaluator needs a persisted plan (branching graph), so a default
  single-target run ends on budget exhaustion
  until V02 adds the true process-level slice and V09 completes the
  HalCTF adapter.
- Flag-hunt skills are gone from the kernel; challenge-specific skill
  packs must arrive with the environment adapters that own them (V09).
- Existing tests that asserted FLAG_HUNT/VERIFY_AND_SUBMIT routing were
  updated to the generic predicates; the supervisor's budget-exhaustion
  tests now wait out a bounded model-call failure (~seconds) instead of
  an idle poll.
- The runner's adapter bridge (ParsedAction → executor contract) pins
  the model's skill deterministically per turn; V02's
  `NormalizedDecision` design will replace this bridge.
