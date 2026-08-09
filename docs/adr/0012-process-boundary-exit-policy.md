# ADR-0012: Process-Boundary Exit Policy for HalCTF Mode

Status: accepted

Date: 2026-08-09

## Context

The supervisor terminates every run with a structured `TerminationReason`
(completed / interrupted / failed / budget_exhausted) that is appended to the
run log as the authoritative `termination` event (AGENTS.md rule 9) and printed
as the human-readable `TERMINATION: <reason>` summary line. The process entry
point (`src/ozzgraph/__main__.py`) maps that reason to a container exit code
via `_EXIT_CODES`: `0` completed, `130` interrupted, `1` failed, `3` budget
exhausted. That mapping predates the HalCTF adapter and was designed for a
local operator watching a terminal.

On the real HalCTF event platform the process boundary has a different
consumer: the platform interprets a NONZERO container exit as a crash and
reruns the detonation — wasting the run budget and marking the run FAILED
even when it scored. Tottori's committed live-run practice (verified from the
halctf-team-tottori deployment's live-run logs, HAL-004) is to return `0` on
any ordinary completed attempt. HalCTF mode already records the full reason
in the run log and fires the sidecar `/done` with the structured reason for
COMPLETED runs (HAL-005), so the container exit code carries no information
the platform cannot get elsewhere.

## Decision

We will make the exit mapping HalCTF-mode-aware at the process boundary only,
leaving the kernel, the `TerminationReason` enum, and the event model
untouched:

1. **HalCTF mode (any `HAL_CTF_ID` / `HAL_CHALLENGE_ID` / `HAL_ENDPOINT` /
   `HAL_MCP_ENDPOINT` / `MCP_ENDPOINT` / `OZZGRAPH_CHALLENGE_ID` variable
   non-blank — `halctf_mode_selected`): every run that reaches a structured
   `TerminationReason` exits `0`.** Scored (COMPLETED), unsolved /
   budget-exhausted (BUDGET_EXHAUSTED), gave-up (a rejected submission that
   never becomes an accepted one), and graceful platform failure (a structured
   FAILED termination) are all ordinary completed attempts at the process
   boundary.
2. **INTERRUPTED also exits `0` in HalCTF mode.** A SIGTERM/SIGINT stop is how
   the platform tears a run down; exiting `130` would be misread as a crash and
   trigger a needless rerun. The `termination` event still records
   `interrupted`.
3. **The internal model is never collapsed (criterion 2).** The `termination`
   event in the run log keeps the structured reason — `budget_exhausted`,
   `failed`, `interrupted`, `completed` — and the `TERMINATION:` summary line
   is unchanged. Only the process exit code is flattened.
4. **Startup-impossible / process corruption stays exit `1`.** Load-time
   `ConfigError` (missing `HAL_USER_ID`, a set-but-invalid `HAL_TARGET_PORT`,
   malformed scope/credentials files), CLI usage errors, and uncaught
   exceptions keep the nonzero boundary: the process never started a run, so
   there is no structured termination to flatten.
5. **Local mode is byte-for-byte unchanged.** With no HalCTF runtime variable
   the legacy mapping applies (`0` / `130` / `1` / `3`), including
   `ozzgraph run <target>` (V02) and the benchmark CLI.

Implementation: `_HALCTF_EXIT_CODES` (every reason -> `0`) plus
`_exit_code_for(reason)` in `src/ozzgraph/__main__.py`, consulted only for the
supervisor's structured termination; all load-time/usage error paths keep
`_EXIT_CODES[FAILED]` (`1`).

## Consequences

Easier:

- The event platform never misreads a deliberate, structured stop as a crash:
  scored-but-exhausted runs are not rerun and are not marked FAILED.
- The run log remains the single source of truth for WHY a run ended; the
  container exit code becomes a crash/startup detector only.
- Local development and CI behavior (`ozzgraph run`, the benchmark suite, the
  read-only image smoke) are untouched.

Harder:

- A platform that wanted to distinguish termination reasons from the exit code
  alone can no longer do so in HalCTF mode — it must read the run log's
  `termination` event (or the sidecar `/done` payload).
- The exit code is mode-dependent, so operators must know whether a run was a
  HalCTF detonation; the `TERMINATION:` line and the run log disambiguate.
