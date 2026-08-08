# Security Policy

## Reporting a vulnerability

OzzGraph is an autonomous CTF agent harness; security findings are taken
seriously. If you believe you have found a vulnerability:

1. **Do not open a public issue or PR describing the flaw.** Report it
   privately so it can be fixed before it is public knowledge.
2. Use GitHub Private Vulnerability Reporting for the repository
   (https://github.com/carterlasalle/ozzgraph — Security tab → Report a
   vulnerability) when the repository is public; otherwise email the
   maintainers directly (`carterlasalle@gmail.com`).
3. Include as much of the following as you can:
   - affected version or commit hash
   - a description of the vulnerability and its impact
   - a minimal reproduction (command line, config, or test)
   - any suggested fix

Reports are acknowledged as soon as possible, and fixes are released
through the normal workflow (see [CONTRIBUTING.md](CONTRIBUTING.md)).
Please allow a reasonable coordinated-disclosure window before publishing
details, and do not include live flag material, credentials, or challenge
artifacts in reports.

## Scope and trust model

OzzGraph is a model-adaptive autonomous CTF harness for **authorized,
isolated environments** only. Its security model treats the model as an
untrusted component that is granted bounded, auditable capabilities:

- **All model output is untrusted.** It is validated before execution and
  labeled as data (never merged into system instructions) when it reaches
  the model again (AGENTS.md § Security Boundaries).
- **The harness owns everything that matters**: state, budgets, scope
  enforcement, process execution, evidence provenance, retries, context
  management, scheduling, hint policy, flag validation, and lifecycle
  (AGENTS.md § Mission). Models own only bounded judgment at uncertain
  decision points.
- The harness is designed for authorized environments; it assumes the
  platform it talks to (HalCTF challenge platform) is part of the
  authorized scope.

## Agent-isolated execution

The core isolation property: **authoritative state lives outside model
context** (AGENTS.md rule 1).

- Durable state is SQLite-backed graph state (`graph.db`), append-only
  JSONL events (`actions.jsonl`), and a content-addressed artifact store.
  Raw artifacts live outside model context; parsers return compact
  summaries and artifact handles.
- **Transcripts are not memory** (rule 2): a conversation transcript is
  never authoritative state. Compaction may shrink active context but must
  never destroy durable history — failed branches remain durable and
  compaction never deletes history.
- Each executor turn is **one bounded action** (rule 4): every action has a
  timeout, an output limit, and a normalized fingerprint; duplicate and
  failed fingerprints are never retried. The tool plane enforces timeouts,
  output limits, process-group termination, and artifact capture.
- Skills load lazily (rule 6): only per-phase summaries are advertised;
  full skill cards load on selection — no prompt carries every capability.

## Provenance tracking

Every confirmed fact is traceable to deterministic evidence (AGENTS.md
rules 3 and 8, docs/ARCHITECTURE.md § Reducer):

- A model claim is a hypothesis; a fact requires deterministic evidence or
  evaluator acceptance tied to evidence IDs.
- Graph invariants: every `Fact` references at least one `Evidence`; every
  `Evidence` references an `Observation` or artifact; every `Observation`
  references an `Action`; every `Submission` references a
  `FlagCandidate`; every submitted `FlagCandidate` has observed provenance.
- Fact entity IDs are deterministic hashes
  (`fact-<sha256(task_id:source:sorted(evidence_ids):summary)>`), and
  replaying all events reconstructs the same graph hash — any tampering
  with the event log is detectable as a replay mismatch.
- The reducer validates every finding's evidence references before merge
  and rejects unresolvable ones loudly (`UnresolvedEvidenceError`) — a
  rejected finding is never represented in the graph.

## Flag isolation and hashing

Flag material is the crown jewel of a CTF harness. It is isolated at
multiple layers:

- **Supervisor-only submission.** Only the supervisor may submit flags,
  buy paid hints, or exit the run (AGENTS.md rules 5 and 7; invariant:
  "The supervisor is the only component that can invoke privileged HalCTF
  operations"). Workers never submit flags; models never purchase hints.
  In `halctl`, privileged subcommands (submit, paid hints, exit) fail
  unless `OZZGRAPH_HAL_PRIVILEGED` is set (docs/USAGE.md).
- **Flag hashing in run-only events (FLAGLEAK-001).** Run-only events are
  not replay-required, so they must not persist plaintext flag material at
  rest: `flags.candidate_found` and the `submission.*` verdict events
  carry only a `flag_sha256` digest and `flag_length`, never the raw flag
  text. The plaintext flag lives only in the replay-required `graph.*`
  entity payloads (commit a667733; docs/API_AND_INTEGRATIONS.md).
- **Bounded attempts.** Submissions are capped per candidate and per run
  (`DEFAULT_MAX_SUBMISSIONS = 3`); flag submission and paid hints are
  always serialized (rule 7) and paid hint count never exceeds the
  configured maximum.
- **Deterministic validation.** Flag candidates are matched against the
  configured flag pattern and only submitted after observed provenance;
  rejected candidates are terminal.

## Privileged operations

Models never call raw MCP methods (AGENTS.md rule 5): MCP is wrapped behind
the local `halctl` adapter, the only adapter surface. Privileged operations
(flag submission, paid hint purchase, run exit) are gated on
`OZZGRAPH_HAL_PRIVILEGED`, which only the supervisor sets.

## Command execution boundary

Before any command executes, the policy gate (`src/ozzgraph/policy.py`)
enforces AGENTS.md § Security Boundaries in order:

1. parse the selected adapter protocol
2. validate schema
3. enforce command-length limits
4. enforce target allowlists (hostnames, IPs, CIDRs)
5. block platform and public-internet destinations
6. check worker and phase permissions (command families: recon / exploit /
   shell)
7. compute a normalized fingerprint
8. reject duplicates
9. attach timeout and output limits
10. record the attempted action before execution

The gate is deterministic and fail-closed: unknown phases, unknown
families, and unallowlisted destinations are rejected loudly. Approved
commands yield a `PolicyDecision` whose fingerprint is mirrored to
`state_dir/duplicates.jsonl` (append-only) — every approved command is
auditable. Target output is also untrusted and is always labeled as data
when fed back to the model.

## Scope and parallelism

Workers are scope-limited (ADR-0005): a worker cannot mutate state outside
its declared task scope. The task-DAG scheduler permits parallel work only
for independent, non-conflicting evidence gathering; mutating exploit
chains, concurrent flag submissions, concurrent hint purchases, and
parallel rate-limited credential attacks are never parallelized.

## Secrets handling

- **gitleaks on every commit.** The GitReins Tier-1 `secrets` guard scans
  all staged content with `.gitleaks.toml`, which catches `sk-`-prefixed
  API keys (OpenAI/OpenRouter/DeepSeek etc.). Docs, specs, and markdown are
  deliberately **not** whitelisted — secrets must be caught everywhere.
- **Never commit secrets.** AGENTS.md § Forbidden Shortcuts: never commit
  secrets, tokens, generated credentials, or live challenge artifacts.
  `.dockerignore` excludes caches, `state/`, fixtures, and secrets from
  the competition image build context.
- **Environment-driven configuration.** All configuration is environment
  variables (`HAL_USER_ID`, `OZZGRAPH_*`); the image carries no baked-in
  identity or challenge config, and the model API key never enters the
  config model (docs/API_AND_INTEGRATIONS.md).
- **Redaction at rest.** Run bundles may optionally redact credentials in
  user-facing views while preserving hashes for deterministic replay
  (docs/DATA_STRATEGY.md).

## Container hardening

The competition image (docs/IMAGE_HARDENING.md, ADR-0007) adds process
isolation around the kernel: non-root runtime user (`ozzgraph`, uid
10001), read-only rootfs (state on a declared volume), no runtime package
installer (pip removed), no exposed ports, no shell wrapper, no Node or
dashboard runtime, and no public-internet runtime dependencies. Forbidden
payloads (CUDA, PyTorch, vLLM, model weights) are structurally impossible
to add, and the CI `docker` job asserts the size budget and the non-root
user on every PR.

## Data invariants with security relevance

From AGENTS.md § Data Invariants — enforced by tests and replay:

- Every graph mutation is representable as an append-only event; replaying
  all events reconstructs the same graph hash.
- A worker cannot mutate state outside its declared task scope.
- Paid hint count never exceeds the configured maximum.
- The supervisor is the only component that can invoke privileged HalCTF
  operations.
- No silent exception swallowing: every fatal path produces a structured
  termination event and a human-readable summary (rule 9).
