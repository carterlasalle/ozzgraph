# ADR-0006: Optional Yarn/TypeScript Dashboard Outside the Image

Status: accepted

Date: 2026-08-07

## Context

PRD.md lists "a dashboard inside the competition image" as a non-goal,
and AGENTS.md's Required Technology section mandates that the only
TypeScript in the repo is the optional dashboard (Yarn, strict
TypeScript, never in the competition image). The kernel owns its state
exclusively (SQLite graph, append-only JSONL events, artifact store),
and the graph hash is a documented determinism contract: replaying all
events reconstructs the same graph hash (AGENTS.md data invariants,
docs/DATA_STRATEGY.md "Replay").

A researcher needs a read-only view of live and finished runs without
touching the Python package, its dependency tree, or the image.

## Decision

We will implement the dashboard as a fully isolated project under
`dashboard/`:

- Yarn 4 (corepack) with strict TypeScript (`strict`, `noImplicitAny`,
  `noUncheckedIndexedAccess`), zero runtime dependencies — `node:http`
  for the server, `node:sqlite` (built into Node >= 22.5) for read-only
  graph access, `node:crypto` for hashing; dev dependencies only for
  tooling (typescript, eslint, vitest, @types/node).
- No dependency is added to `pyproject.toml` or the Python package, and
  nothing under `dashboard/` is imported by Python code.
- Run discovery: every subdirectory of the runs root holding
  `actions.jsonl` and/or `graph.db` is a run (run id = directory name);
  the root itself is tolerated as a single run (run id = basename) for
  the kernel's default single-run layout.
- The API (docs/API_AND_INTEGRATIONS.md "Optional Dashboard API")
  serves runs, graph, events, artifacts, metrics, and replay over
  structured JSON with a stable error shape. Path segments are strictly
  validated (`..`, absolute paths, separators, control characters are
  rejected before any filesystem access); `graph.db` is opened with
  SQLite's read-only flag plus `PRAGMA query_only`; every query is
  parameterized; the dashboard never writes kernel state.
- Replay is a faithful TypeScript port of `src/ozzgraph/replay.py`
  (in-memory entity/edge map; only the five `graph.*` event types are
  applied; malformed graph events abort loudly with `400`). The
  returned `graph_hash` reproduces the kernel's hash byte-for-byte by
  mirroring Python's canonical JSON serialization (`sort_keys`,
  compact separators, `ensure_ascii`, raw number literals) and
  timestamp normalization (`isoformat()` after UTC conversion). A
  kernel-generated golden fixture cross-checks the port in CI.
- The Python kernel remains the only writer of run state; the dashboard
  is read-only by construction.

## Consequences

Easier:

- Researchers can inspect live runs, artifacts, and metrics without a
  Python environment or any kernel change.
- The dashboard's replay hash is directly comparable to kernel replay
  output and golden traces.
- The competition image is untouched: no Node, no Yarn, no dashboard
  dependencies.

Harder:

- The TS replay must track kernel replay semantics and
  `SCHEMA_VERSION` (currently 2) if the kernel evolves them; a drift
  shows up immediately in the golden-fixture test.
- Two toolchains (uv for Python, Yarn for the dashboard) are required
  to develop the full repo.
- `node:sqlite` is still flagged experimental by Node 22 (a stderr
  warning); it is stable enough for read-only local tooling and is
  built in, so no native dependency is needed.
