# ADR-0005: Declarative Worker Scopes and Fail-Closed Isolation

Status: accepted

Date: 2026-08-07

## Context

PR25 implements step 25 of the implementation plan ("Specialist
workers") on top of the PR24 scheduler: the concrete scope-limited
workers the task DAG drives. AGENTS.md's data invariants require that
"a worker cannot mutate state outside its declared task scope", and
docs/ARCHITECTURE.md ("Parallelism") distinguishes safe parallel work
(independent service enumeration, separate artifact analysis,
independent vulnerability hypotheses, read-only source localization)
from unsafe parallel work (multiple workers mutating the same session,
concurrent flag submissions, concurrent hint purchases, dependent pivot
attempts, parallel rate-limited credential attacks).

PR24 recorded the scheduler's concurrency model (conflict keys, bounded
parallelism, supervisor-only serialization) in ADR-0004 but deliberately
deferred the worker-scope model to this PR. That model is a new
isolation semantic: it defines, declaratively and before anything runs,
what each worker may execute, and it changes the process-isolation
policy at the worker layer (AGENTS.md: "Architecture Decision Records" —
"changes process-isolation policy"). It therefore gets its own ADR.

## Decision

We will implement `ozzgraph.workers` as a small, deterministic
component of scope-limited specialist workers:

- **Declarative scopes.** A `WorkerScope` is an immutable contract
  declaring the command families a worker may run (a subset of the
  policy gate's vocabulary — `shell` is never implied), the graph
  phases it may serve, whether its work mutates state (read-only vs
  mutating), and an optional target-allowlist narrowing (hostnames,
  IPs, CIDRs). Scopes are class attributes on the concrete workers and
  are validated loudly at construction: empty scopes, blank or
  duplicate families, unknown families, and a read-only scope declaring
  a mutating family all raise typed `WorkerScopeError` subclasses
  before anything can run.

- **Deterministic mutation partition.** The policy gate's command
  families are partitioned once, at module level, into mutating
  (`MUTATING_COMMAND_FAMILIES = {"exploit"}`) and read-only (`recon`,
  `shell`), mirroring ARCHITECTURE.md's safe/unsafe parallel-work
  lists: evidence gathering parallelizes, exploit chains and
  rate-limited credential attacks never run on a read-only worker.

- **Fail-closed assignment gate.** A `WorkerTask` — the scheduler's DAG
  node plus exactly one bounded action (command, timeout, output limit,
  phase, and its required scope) — is assigned to a worker only when
  the worker's declared scope covers the task's required scope
  (families, phases, mutation permission, and CIDR-aware target
  coverage). A conflicting assignment raises the typed
  `TaskOutOfScopeError` BEFORE any execution. `run_task` re-checks the
  assignment and additionally rejects, at run time and before any
  execution, commands whose classified family is outside the declared
  families (`FamilyOutOfScopeError`) or is a mutating family on a
  read-only worker (`ReadOnlyViolationError`) — a mis-specified
  assignment can never smuggle a command past the worker's families.
  Rejections are structured errors, never silent filtering, and when
  driven through the scheduler they become structured failed
  `worker_run` records (never silent, never retried).

- **Supervisor-only serialization composes.** `SubmissionWorker` is the
  supervisor-serialized worker wrapper: it refuses any task that does
  not carry the reserved `SERIALIZED_CONFLICT_KEY`
  (`SerializationRequiredError`), so it can only ever run tasks created
  with `serialized_task()` — which the scheduler already serializes
  against every other task. Only the supervisor may wire this worker
  (AGENTS.md rule #5); nothing is wired in this PR.

- **Evidence, never prose.** A successful run stores the bounded output
  as content-addressed artifacts and returns one structured `Finding`
  (reused from the scheduler) with provenance (task id, worker source)
  and mandatory evidence references — no free-form prose becomes state
  (AGENTS.md rule #3). Findings stay embedded in `worker_run` records;
  the reducer (step 26) promotes them.

- **Deterministic and kernel-small.** No randomness, no wall-clock
  ordering decisions, no hidden global mutable state (the only instance
  state is the assignment map, written only through `assign()`), no
  dynamic imports. Workers persist nothing — the scheduler owns the
  `task`/`worker_run` entities and the event log — and nothing is wired
  into the supervisor (rule #10). PR25 delivers the component plus its
  contracts, standalone like PR24.

## Consequences

Easier:

- The worker-scope invariant is enforced as data, not as an unwritten
  convention: a worker's capabilities are a typed, validated scope, and
  every task execution is checked against it in two layers (assignment
  and run time) before any command can run.
- Scope violations are typed and diagnosable: `TaskOutOfScopeError`
  messages enumerate the exact gaps (families, phases, mutation
  permission, targets), and read-only/family violations carry the
  offending family and command.
- Safe parallel work composes with the PR24 scheduler without new
  scheduler logic: read-only recon/analysis workers assign independent,
  conflict-free tasks that run concurrently, while privileged work goes
  through the serialized-task gate on both the DAG and the worker.
- Target narrowing composes with the existing policy gate: a worker
  with a declared allowlist runs every command through a scope-narrowed
  `ScopePolicy` in addition to the operator policy, so narrowing is
  enforced by the same destination logic the gate already owns.

Harder:

- Assignments are per-worker instance state: whoever wires the loop
  (a later PR) must build a dispatch runner that routes each DAG task
  to the worker owning its assignment, and must not reuse a worker
  across schedules that repeat fingerprints (the per-worker in-memory
  fingerprint store rejects duplicates).
- The mutating partition is a fixed module-level constant; extending
  the family vocabulary (e.g. a future `cleanup` family) requires
  revisiting `MUTATING_COMMAND_FAMILIES`.
- Worker findings are still not graph `evidence`/`fact` entities —
  consumers that need promoted evidence must wait for the reducer PR
  (step 26).
