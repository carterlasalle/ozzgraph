# ADR-0002: Bootstrap Event Types and Deterministic Probe Policy

Status: accepted

Date: 2026-08-07

## Context

PR12 introduces deterministic bootstrap reconnaissance
(docs/TECHNICAL_REQUIREMENTS.md, "Bootstrap"): parse single and namespaced
target variables, retrieve challenge status, inspect the startup environment
for the smoke-test flag, request free hint zero when available, validate
target reachability, and run category-appropriate deterministic probes —
all before the supervisor's main loop, with no model involvement.

Every bootstrap step must land in the append-only event log. ADR-0001 fixed
the log format and the two original event types (`bootstrap`, `termination`);
bootstrap adds a family of new event types. The probe policy — deterministic
commands, scope-policy-gated, fail closed on an empty allowlist — is a second
architectural decision. Both are recorded here.

## Decision

We will record every bootstrap step as exactly one structured event with
producer `bootstrap`, using new event-type constants in
`src/ozzgraph/events.py`:

- `bootstrap.targets_parsed` — validated target configuration (single +
  namespaced).
- `bootstrap.challenge_status` — challenge status retrieval; on Hal service
  failure the payload carries the error.
- `bootstrap.smoke_submitted` — smoke-flag submission verdict; on failure
  the payload carries the error.
- `bootstrap.hint_requested` — free hint zero result.
- `bootstrap.hint_unavailable` — free hint zero request failed (hint not
  available).
- `bootstrap.reachability` — per-target outcome (`reachable`,
  `unreachable`, or `blocked`).
- `bootstrap.probe_run` — per-target probe detail (command, exit code,
  timeout, duration, output excerpt).
- `bootstrap.failed` — fatal bootstrap configuration error.

Failure policy: Hal service failures during a bootstrap step are recorded in
that step's payload (the client also emits its own `hal_failure` event) and
are not fatal — the harness must survive MCP outages (Recovery
Requirements). Configuration errors (malformed target variables, unknown
target namespace, smoke flag without a challenge id) record
`bootstrap.failed` and raise `ConfigError`; the supervisor terminates with
`FAILED`, producing the structured termination event AGENTS.md rule 9
requires.

Target configuration comes from `OZZGRAPH_TARGET` (single) and
`OZZGRAPH_TARGET_<NS>` (namespaced; `NS` ∈ `HTTP`, `HTTPS`, `DNS`).
`OZZGRAPH_TARGET_ALLOWLIST` shares the prefix but is a scope-policy knob and
is excluded. Each target maps to exactly one fixed probe command (`curl` for
HTTP/HTTPS, `dig` for DNS) with explicit in-command timeouts, ShellRunner
wall-clock bounds, and output limits. Every probe passes the scope policy
(length, target allowlist, platform/public-internet blocks, BOOTSTRAP-phase
command families) and the fingerprint store before execution; an empty
allowlist fails closed, so a misconfigured deployment cannot probe anything.
Probes are deterministic — no model calls — and their outcomes are data
(events), never exceptions. The privileged `HalClient` used for smoke
submission is constructed by the supervisor only (AGENTS.md invariant 5).

## Consequences

Easier:

- The full bootstrap is auditable and replayable from the event log alone.
- Deterministic probes keep startup behavior reproducible and cheap (no
  LLM, bounded timeouts, bounded output).
- Fail-closed allowlisting means a misconfigured deployment cannot reach the
  public internet during bootstrap.

Harder:

- Each probed target adds two events (reachability + probe_run) to the run
  log — bounded and small, but the log grows with the target count.
- Adding a target category requires a new namespace, a new probe spec, and
  test coverage.
- Operators must set `OZZGRAPH_TARGET_ALLOWLIST` explicitly before any probe
  can run; an empty allowlist silently blocks all probes (recorded as
  `blocked` reachability events).
