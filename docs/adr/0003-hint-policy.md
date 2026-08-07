# ADR-0003: Deterministic Paid-Hint Policy and Event Semantics

Status: accepted

Date: 2026-08-07

## Context

PR23 implements the paid-hint policy slice of Phase 8
(docs/TECHNICAL_REQUIREMENTS.md, "Hint Policy"; docs/IMPLEMENTATION_PLAN.md
step 23): hint zero is free and automatic, paid hints are supervisor-only,
and a paid hint must satisfy five deterministic conditions — a paid-hint
budget (maximum one per detonation), no recent information gain, exhausted
low-cost actions, two evaluator recommendations, and sufficient
expected-value improvement.

HalClient already guards `request_hint` for `index > 0` with a privilege
check (AGENTS.md invariant 5), but nothing in the kernel decides WHEN a
paid hint is justified. The paid-hint count must never exceed the
configured maximum (AGENTS.md data invariant), paid hints must be
serialized (AGENTS.md rule #7), and every decision must be auditable from
the append-only event log (AGENTS.md rule #1).

This change adds graph event semantics (a family of `hint.*` run events
and two new entity types) and formalizes privileged-operation policy for
paid hints — both are architectural decisions per AGENTS.md ("Architecture
Decision Records"), so an ADR is required. ADR-0001 fixed the log format;
ADR-0002 fixed the bootstrap event family and probe policy; this ADR
records the hint-policy event family, entity contracts, and gate rules.

## Decision

We will implement `ozzgraph.hints` as two small, deterministic modules
mirroring the PR22 `ozzgraph.flags` / `ozzgraph.submissions` pattern:

- `HintPolicy` — a pure, fail-closed gate. `evaluate(graph, request)`
  returns a typed `PaidHintDecision` (rule breakdown + reasons) with no
  graph mutation, no events, and no wire calls. Hint zero is approved
  unconditionally (`rules = {"free_hint": true}`). Every paid hint is
  evaluated against five deterministic predicates over the authoritative
  graph state:

  - `budget_available` — the persisted `hint_purchase` entity count is
    below `max_hints` (default 1). The count of `hint_purchase` entities
    IS the paid-hint ledger, so the count can never exceed the maximum.
  - `no_recent_information_gain` — no `fact`/`evidence`/`observation`
    entity has `created_at` strictly after the anchor, where the anchor is
    the later of the latest `hint_purchase` and the latest `evaluation`
    entity `created_at`. No anchor (neither purchase nor evaluation) is a
    denial — the gate cannot claim the state was assessed.
  - `low_cost_actions_exhausted` — every `plan_step` entity of the latest
    `plan` entity (greatest `(created_at, id)`, the evaluator's PR21
    selection rule) has at least one attempted `action` entity bound via
    its `plan_step_id` payload. Unrepresentable state (missing plan,
    corrupt `step_count`, step-set mismatch) denies.
  - `two_evaluator_recommendations` — at least two `hint_recommendation`
    entities exist; each is idempotent per evaluation
    (`hint-rec-<sha256(evaluation_id)>`), so two records mean two distinct
    evaluations recommended a hint.
  - `sufficient_expected_value` — `(1 - progress) * min(1, attempts /
    EV_STALL_FLOOR) >= MIN_EV_GAIN` (0.5), where `progress` is the
    completed fraction of the latest plan per the latest evaluation's
    `step_outcomes` (0.0 when absent) and `attempts` is the count of
    `action` entities bound to the latest plan via `plan_id`.

  The gate never coerces state: any unrepresentable or unknown state
  denies with a documented reason (fail-closed).

- `HintCoordinator` — the ONLY kernel caller of `request_hint` for
  `index > 0`, mirroring `SubmissionCoordinator`'s ownership of
  `submit_flag`. `check_then_request(graph, index)` serializes paid hints
  under an `asyncio.Lock` (AGENTS.md rule #7), raises
  `HintPrivilegeError` before the gate when the client is not privileged,
  and on approval records `hint.policy_approved`, then
  `hint.purchase_attempted` BEFORE the wire call, then persists the
  `hint_purchase` entity (same-timestamp `graph.*` event, the PR20
  executor pattern) and records `hint.purchase_succeeded`; a denial
  records `hint.policy_denied` (with the rule breakdown and reasons) and
  raises `HintPolicyDeniedError`; a wire failure records
  `hint.purchase_failed` and re-raises. A `paid: false` platform answer
  to a paid request raises `HintStateError` and persists no purchase.

New event types (producer `hints`, added to `src/ozzgraph/events.py`):
`hint.policy_denied`, `hint.policy_approved`, `hint.purchase_attempted`,
`hint.purchase_succeeded`, `hint.purchase_failed`,
`hint.recommendation_recorded`. New entity types
(docs/DATA_STRATEGY.md): `hint_purchase` (`hint-purchase-<seq>`,
deterministic, entity-only — no edge, because the count is the ledger)
and `hint_recommendation` (`hint-rec-<sha256(evaluation_id)>`,
idempotent per evaluation).

`Supervisor.request_paid_hint(graph, index, challenge_id=None, *,
client=None)` is the supervisor-owned entry point (AGENTS.md invariant 5),
mirroring `submit_verified_candidate`: it requires `index >= 1` (hint zero
is bootstrap's job), resolves the challenge id, constructs a
supervisor-owned privileged `HalClient` when none is injected, drives the
coordinator, and closes the client it owns. The idle loop is untouched —
the surface exists for a future loop driver, exactly like PR22.

## Consequences

Easier:

- The paid-hint budget is derived from persisted entities, so replay
  reconstructs the identical ledger and the "never exceeds the maximum"
  invariant is checkable from the graph alone.
- Every gate decision is auditable: denials carry the full rule breakdown
  and reasons in `hint.policy_denied`, approvals record the same
  breakdown, and purchases are mirrored as `graph.*` events.
- The coordinator's lock plus the in-lock budget re-read make concurrent
  gate evaluations safe without any graph-level locking protocol.
- The gate is pure and fully unit-testable: every rule denies its own
  condition deterministically, and the fail-closed cases (no plan, corrupt
  payloads, no anchor) are covered by tests.

Harder:

- A paid hint is intentionally hard to justify: with the default
  `max_hints = 1`, the gate requires a stalled plan (every step attempted,
  gain ≥ 0.5), two distinct evaluator recommendations, and no fresh
  information — the "buy a hint" path needs genuine, recorded stagnation.
- The evaluator itself still emits no recommendation signal; whoever wires
  the loop must call `HintPolicy.record_evaluator_recommendation` after an
  evaluation pass that warrants one. The gate only counts the records.
- New graph entity types and event types are forward-only additions; any
  dashboard or tooling that enumerates entity/event types must be updated
  to know `hint_purchase`, `hint_recommendation`, and the `hint.*` events.
