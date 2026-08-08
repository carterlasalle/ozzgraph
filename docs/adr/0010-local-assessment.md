# ADR-0010: Local Assessment — Modes, Scope/Credential Files, Report Bundle

Status: accepted

Date: 2026-08-08

## Context

Milestone 8 of the v2 plan (docs/CHANGES_v2.md, "v2/local-assessment") makes
the local experience the DEFAULT OzzGraph experience: an operator points the
harness at a target and gets a complete, evidence-backed vulnerability
assessment — modes for the kinds of targets (URL, network, repository, Docker
Compose, hybrid), operator-supplied scope and credentials, and a report the
operator can read, machine-consume, or feed to CI (SARIF). Three decisions
fall out of that milestone, and each touches an architectural invariant:

1. **Target modes.** The v1 local environment (ADR-0008) classified targets
   into url/network/host only. Repositories and Docker-Compose stacks are
   local filesystem surfaces — the harness must classify them, and a path
   that is not what it claims must fail loudly (AGENTS.md rule #9).
2. **Scope + credentials files.** Scope entries must merge into the SAME
   allowlist the policy gate enforces (ADR-0008: scope data and the gate can
   never disagree), and credentials must be usable without ever storing a
   secret (AGENTS.md: no committed secrets).
3. **Reporting.** AGENTS.md rule #1 makes the SQLite graph the authoritative
   state and rule #9 demands a human-readable summary on every fatal path;
   the milestone adds a full operator-facing report bundle (markdown, JSON,
   SARIF) plus a materialized `evidence/` directory.

## Decision

### 1. Local-assessment modes (`ozzgraph.environments.local`)

`classify_local_target(address)` deterministically classifies every target
address, in this order:

- a URL scheme marker (`://`) -> `url` (target type `url`);
- a parseable CIDR with an explicit prefix -> `network` (a bare IP is a
  single `host`, per the milestone vocabulary "host/IP -> host");
- a path-like address (absolute/relative markers, an embedded separator, or
  an existing directory) -> validated loudly: contains `.git` -> `repo`,
  contains a compose file (`docker-compose.yml` / `.yaml` / `compose.yaml` /
  `compose.yml`) -> `compose`; a nonexistent path, a non-directory, or a
  directory that is neither raises `ConfigError` (fail loudly);
- everything else (hostname, bare IP, host:port) -> `host`.

`OZZGRAPH_TARGET` / `OZZGRAPH_TARGET_<NS>` values and allowlist fallback
entries are classified with this function; each `Target` carries its mode in
`metadata["mode"]` using the human vocabulary (`repository`, `docker-compose`)
while the `type` field keeps the model literal (`repo`, `compose`). The
scope's `constraints["mode"]` is derived from the effective target set: one
mode when every target shares a type, `hybrid` when targets span multiple
types, `none` with no targets; `constraints["target_modes"]` lists the sorted
unique modes. Repo/compose entries are local surfaces and never enter the
scope's host/url/network buckets.

### 2. Scope + credentials files (`ozzgraph.config`)

- `OZZGRAPH_SCOPE_FILE` (JSON / YAML / TOML by suffix; TOML uses an
  `allowlist` table since TOML has no bare top-level lists): a list of
  allowlist entries, validated non-empty strings, merged into
  `target_allowlist` as a sorted set union with the `OZZGRAPH_TARGET_ALLOWLIST`
  env var. The allowlist remains the single source of truth for the
  authorized surface, so the policy gate and scope data still cannot
  disagree.
- `OZZGRAPH_CREDENTIALS_FILE` (same formats; TOML uses a `credentials` array
  of tables): a list of `{name, kind, username?, secret_env?}` records
  validated by the `Credential` model (`extra="forbid"`; at least one of
  `username`/`secret_env`). The file and config carry ONLY references: the
  secret value is read from the environment variable named by `secret_env`
  at runtime (`credential_secret`), never stored in the file, the config
  model, or any graph entity.
- Both files fail loudly on any malformed input: unreadable file, unknown
  suffix, unparseable document, wrong shape, or invalid record -> `ConfigError`.

### 3. Report bundle (`ozzgraph.reporting`)

At COMPLETED termination the runner renders the full bundle into `state_dir`
(the bundle is derived output — the authoritative `graph.db` / `actions.jsonl`
are never modified, preserving replay compatibility):

- `report.md` — deterministic human-readable per-finding writeup (id, CWE,
  severity derived from the impact CIA, affected assets, preconditions,
  evidence ids, reproduction commands, confidence);
- `report.json` — the same finding payloads as the V02 `findings.json` plus
  graph metadata: run id, environment, model id, targets, scope, termination
  status/reason, turns/model/tool counts, and zero-filled entity counts;
- `report.sarif` — a SARIF 2.1.0 document: one result per finding mapped to
  its CWE rule (`ruleId`/`ruleIndex`), `level` derived deterministically from
  the impact CIA (any high -> error, any medium -> warning, any low -> note,
  else warning), locations pointing at the materialized evidence artifacts,
  driver `ozzgraph`; run metadata in `automationDetails`/`properties`;
- `evidence/` — copies (named by artifact id) of every artifact referenced by
  the findings' evidence chains, from the authoritative artifact store;
- `graph.sqlite` + `events.jsonl` — deterministic snapshots of `graph.db`
  (sqlite3 online-backup API, consistent under WAL) and `actions.jsonl` under
  the milestone's canonical names. The canonical authoritative names
  (`graph.db` / `actions.jsonl`) are untouched because they are embedded in
  the replay/dashboard/trace tooling (docs/DATA_STRATEGY.md); the bundle
  materializes the milestone's names as derived copies.

Every render derives from authoritative graph state — never wall-clock time or
model prose (`generated_at` comes from the run entity's creation timestamp).
A render failure raises `ReportError`; the runner records it as a
`runner.report_failed` event and still returns the terminal status (the run
itself completed — the failure is loud, never silent).

### 4. Default experience

Environment selection is unchanged (ADR-0008): with no `HAL_*` configuration
the run uses `LocalEnvironment`. The `ozzgraph run` CLI help documents the
modes, the scope/credentials files, and the report bundle.

## Consequences

Easier:

- One deterministic classifier drives scope buckets, targets, and the hybrid
  designation — no divergent classification paths.
- The report bundle turns a completed run into consumable output for humans
  (markdown), machines (JSON), and CI/security tooling (SARIF with evidence
  locations) without touching authoritative state.
- Credentials are reference-only: operators can commit the credentials file
  safely and supply secrets via the environment.
- Scope files let operators keep their allowlist in a checked-in artifact
  instead of an env var, merged deterministically with the env allowlist.

Harder:

- Path classification is filesystem-dependent by nature: a target that is
  path-like only because a same-named directory exists can flip between host
  and repository classification when the working directory changes — the
  deterministic rules (absolute/relative markers or an embedded separator)
  keep this stable for explicit paths.
- `report.json`'s `findings` array duplicates `findings.json`; the graph
  entities remain the authoritative store and both renders derive from it, so
  the duplication is derived output, not a second source of truth.
- The bundle adds I/O at termination (evidence copies + sqlite backup); a
  failure is loud (`runner.report_failed`) but never changes the run's
  terminal status.
