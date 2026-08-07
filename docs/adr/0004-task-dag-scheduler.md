# ADR-0004: Task DAG, Conflict Keys, and the Bounded-Parallel Scheduler

Status: accepted

Date: 2026-08-07

## Context

PR24 implements the first slice of Phase 9 "Workers"
(docs/IMPLEMENTATION_PLAN.md, step 24; docs/ARCHITECTURE.md,
"Scheduler"): the task DAG, the scheduler, conflict keys, and the
structured-findings / worker-run contracts. AGENTS.md rule #7 mandates
that workers have explicit dependencies and conflict keys — parallelize
evidence gathering, never mutable exploit chains — and that flag
submission and paid hints are always serialized. AGENTS.md rule #3
mandates that a model claim is a hypothesis and that no free-form model
prose becomes authoritative state; the Phase 9 exit is "independent
tasks run concurrently, conflicting tasks serialize".

This change adds a new concurrency model to the kernel (bounded
parallel task execution with conflict-key serialization) and graph
event semantics (two new entity types, two new edge types, a family of
`scheduler.*` run events) — both are architectural decisions per
AGENTS.md ("Architecture Decision Records"). ADR-0001 fixed the log
format; ADR-0002 fixed the bootstrap event family and probe policy;
ADR-0003 fixed the hint-policy event family and gate rules; this ADR
records the scheduler's concurrency model, entity/edge contracts, and
event family.

## Decision

We will implement `ozzgraph.scheduler` as a small, deterministic
component with an injected runner:

- `TaskDAG` — a DAG of `Task` nodes, each carrying explicit
  `depends_on` references and an explicit `conflict_keys` set.
  Construction validates the whole DAG and fails loudly (AGENTS.md rule
  #9) through the typed `TaskDAGError` hierarchy: duplicate ids,
  dependencies on unknown tasks, and cycles (including self-
  dependencies) are rejected before anything runs. Ready-task selection
  and topological order are deterministic — sorted by stable id.

- `Scheduler` — given a `TaskDAG`, runs tasks with bounded parallelism
  (`max_workers`, the existing `OZZGRAPH_MAX_WORKERS` config knob;
  nothing new is wired into config). A task starts only when it is
  (a) dependency-complete and (b) non-conflicting with every currently
  running task. Two tasks conflict when their conflict-key sets
  intersect; tasks with no keys run concurrently. The dispatch loop
  iterates `ready_order` (id-sorted), so schedules are deterministic
  and reproducible. The runner is injected (an `async` `run_task`
  returning a typed `TaskOutcome`); a runner crash becomes a structured
  failed worker run — never silent, never retried.

- Supervisor-only serialization (AGENTS.md rule #7): the reserved
  `SERIALIZED_CONFLICT_KEY` (`"serialized"`) makes a task conflict with
  every other task, including other serialized tasks. `serialized_task`
  is the dedicated hook the supervisor uses for flag submission and
  paid hints; the gate is deterministic and fail-closed.

- Structured findings (AGENTS.md rule #3): a `Finding` is a typed
  record with provenance (task id, source) that MUST carry at least one
  evidence/artifact id — a finding without evidence is rejected loudly.
  Each scheduled task produces one `WorkerRun` with a stable id
  (`worker-run-<sha256(run_id:task_id)>`), a status, and its findings.
  Findings stay embedded in their `worker_run` payload; the reducer
  (step 26) promotes them into `evidence`/`fact` entities. The
  scheduler itself never merges findings into the graph as
  authoritative state.

- Graph persistence (AGENTS.md rule #1): the scheduler persists `task`
  entities (entity id = the caller-supplied task id, idempotent),
  `worker_run` entities, `TASK IMPLEMENTS PLANSTEP` edges (when a task
  implements a plan step) and `WORKER_RUN EXPLORED HYPOTHESIS` edges
  (when a task explores a hypothesis), mirroring every mutation to the
  append-only event log as a same-timestamp `graph.*` event (the PR20
  executor pattern), so replay reconstructs the identical graph hash.

New entity types (docs/DATA_STRATEGY.md): `task` and `worker_run`. New
edge types: `TASK IMPLEMENTS PLANSTEP` and `WORKER_RUN EXPLORED
HYPOTHESIS`. New run-event types (producer `scheduler`, defined in
`src/ozzgraph/scheduler.py`): `scheduler.run_started`,
`scheduler.task_started`, `scheduler.task_completed`,
`scheduler.task_failed`, `scheduler.run_completed`.

Nothing is wired into the supervisor's idle loop or main flow: PR24
delivers the scheduler component plus its contracts. The reducer
(step 26) and specialist-worker scoping (step 25) are separate PRs and
are not implemented here.

## Consequences

Easier:

- The concurrency contract is explicit and testable: conflict-key
  mutual exclusion, dependency ordering, bounded parallelism, and
  deterministic order are all verified with an instrumented runner
  that records execution intervals — no timing-dependent assertions.
- Serialization of privileged work (flag submission, paid hints) is a
  deterministic gate on the DAG itself, not an ad-hoc lock: a
  serialized task simply never starts while anything else runs.
- Worker outputs enter the graph only as structured, evidence-
  referenced findings inside `worker_run` records; replay reconstructs
  the identical graph hash, and the reducer (PR26) can merge findings
  without guessing provenance.
- `max_workers` was already a config knob; the scheduler adds no new
  configuration surface.

Harder:

- A DAG is scheduled once per run id: re-scheduling the same
  `(run_id, task_id)` pair fails loudly on the duplicate `worker_run`
  entity rather than rewriting history; whoever wires the loop must
  choose distinct run ids (or a wave identifier) per schedule.
- Findings are not yet graph `evidence`/`fact` entities — consumers
  that need promoted evidence must wait for the reducer PR.
- New entity and event types are forward-only additions; any dashboard
  or tooling that enumerates entity/event types must know `task`,
  `worker_run`, the two edge types, and the `scheduler.*` events.
