# OzzGraph Customization Guide

This guide explains how to extend and tune the OzzGraph harness as
implemented: model profiles and adapter interfaces, the skill registry, scope
policy and duplicate detection, the hint policy, specialist workers, the
reducer, and graph-driven phase routing. Every extension point is a **plain,
deterministic registry** — there is no plugin system, no discovery, and no
dynamic import (AGENTS.md rule #10): adding something means adding one explicit
registration. For end-to-end usage, see [USAGE.md](USAGE.md); for the full
component contracts and wire protocols, see
[API_AND_INTEGRATIONS.md](API_AND_INTEGRATIONS.md).

## 1. Model profiles and adapter interfaces

### 1.1 Profiles (`ozzgraph.profiles`, PR13)

A `ModelProfile` captures everything an adapter needs to know about one model
family:

```python
from ozzgraph.profiles import ModelProfile

# Fields: family, protocols, context_soft_limit, output_token_limit,
# temperature, supported_roles, max_advertised_skills,
# failure_behavior, confidence
```

| Field | Meaning |
|---|---|
| `family` | Stable family name (e.g. `"gpt"`, `"claude"`, `"deepseek"`, `"llama"`). |
| `protocols` | Protocol families the model is known/assumed to support: `"terminal"`, `"three_line"`, `"json"`, `"function_call"`. |
| `context_soft_limit` | Usable context budget (chars/tokens) for the adapter. |
| `output_token_limit` | Output token cap for completions. |
| `temperature` | Sampling temperature; `None` means the model/provider default. |
| `supported_roles` | Message roles the protocol can express (e.g. `system`/`user`/`assistant`). |
| `max_advertised_skills` | Cap on how many skills are advertised to the model in one prompt. |
| `failure_behavior` | `"repair_retry"` (known families) or `"abort_turn"` (unknown-model fallback). |
| `confidence` | How sure we are the profile matches the model; low confidence triggers protocol probing. |

The built-in registry `BUILTIN_PROFILES` is keyed by family: `gpt`, `claude`,
`deepseek`, `llama`, plus `fallback`. Model ids map to families through
deterministic prefixes (`gpt-4o` → `gpt`, `deepseek-v4-flash-0731` →
`deepseek`, `claude-3` → `claude`, `llama3.1:8b` → `llama`); a prefix matches
only when followed by a non-letter, so lookalike ids like `gptool` never map
silently.

`FALLBACK_PROFILE` is deliberately conservative: terminal text only, no
function-call assumption, no system role, no advertised skills, `abort_turn`
failure behavior, confidence 0.3 — so protocol probing stays in the loop for
unknown models.

**Discovery** (`discover_profile`) refines a base mapping with evidence: a
model id present in the provider's advertised model list adds confidence, and
a completion sample that conforms to a not-yet-declared protocol adds it to
the profile. `function_call` is **never** added from a text sample (AGENTS.md:
never assume function-call support). Probing (`probe_protocol`) is pure and
never raises on hostile input; only the first 4096 chars are examined.

**Adding a profile** is one dict entry:

```python
from ozzgraph.profiles import BUILTIN_PROFILES, ModelProfile

BUILTIN_PROFILES["myfamily"] = ModelProfile(
    family="myfamily",
    protocols=frozenset({"terminal", "json"}),
    context_soft_limit=64_000,
    output_token_limit=4_096,
    temperature=0.2,
    supported_roles=["system", "user", "assistant"],
    max_advertised_skills=8,
    failure_behavior="repair_retry",
    confidence=0.9,
)
```

### 1.2 Adapter interfaces (`ozzgraph.adapters`, PR13–PR15)

A `ModelAdapter` owns one protocol family end to end: prompt compilation,
parsing, repair, and protocol-specific limits. The abstract contract is
`protocol`, `compile_prompt(mission, graph_summary, transcript_tail, skills,
output_contract)`, `parse(completion) -> ParsedAction`, and
`repair(completion, error) -> str | None` (never raising). Profile-derived
limits (`context_soft_limit`, `output_token_limit`, `temperature`,
`supported_roles`, `max_advertised_skills`, `failure_behavior`) read through
to the profile and may be overridden when a protocol imposes stricter limits.

`ParsedAction` is the normalized, protocol-independent result: `kind`
(e.g. `run`, `think`, `submit`, `hint`, `exit`), optional `payload` and
`rationale`, plus `raw` (the original completion, so repair and evidence
handling never lose the source). The kind vocabulary is NOT validated by the
adapters — executor policy owns that, so schema-valid kinds pass through.

The registry `ADAPTERS` maps protocol → adapter class, populated by explicit
`register_adapter()` calls. Duplicate registration or a non-`ModelAdapter`
subclass raises `AdapterRegistryError` loudly.

## 2. The three adapter protocols

### 2.1 Terminal-native (`"terminal"`)

The permissive plain-text fallback for unknown models. Free text with an
optional directive:

```text
MISSION ...
GRAPH SUMMARY ...
TRANSCRIPT TAIL ...
AVAILABLE SKILLS ...
...
ACTION: <kind>
PAYLOAD: <value>
```

A completion with no `ACTION:` line degrades to a `think` action; parsing
never raises on plain text (only on empty input). There is no repair strategy —
the protocol is permissive by construction.

### 2.2 Three-line (`"three_line"`)

The strict bounded-output protocol: exactly three non-empty lines, in order,
each matching `LABEL: <value>`:

```text
THOUGHT: <reasoning>
ACTION: <kind>
PAYLOAD: <value>
```

Every deviation — wrong line count, wrong label order, missing/empty values,
extra lines — raises `AdapterParseError` with a human-readable detail. The
repair strategy (PR15) is **labeled-line extraction**: prose-wrapped
completions are scanned for the first occurrence of each label and rebuilt
into the exact three-line format.

### 2.3 JSON (`"json"`)

The strict structured-output protocol: exactly one JSON object carrying the
normalized action shape — required non-empty string `kind`, optional string
`payload` / `rationale`, no other keys (`extra="forbid"`). Every deviation
raises `AdapterParseError`; a raw pydantic error never escapes the layer.

The repair strategy is deterministic, never-raising salvage: (a) strip a
surrounding markdown code fence (` ``` ` / ` ```json `), (b) else extract the
first balanced `{...}` object from prose (string-aware: braces inside JSON
strings never count toward depth), (c) return the repaired JSON text only when
it parses as the action shape and differs from the input — else `None`. No LLM
calls.

```python
from ozzgraph.adapters import JsonAdapter, adapter_for
from ozzgraph.profiles import GPT_PROFILE

adapter = adapter_for("json")(GPT_PROFILE)
parsed = adapter.parse('{"kind": "run", "payload": "nmap -sV 10.0.0.5"}')
assert parsed.kind == "run"
```

## 3. Skill registry, lazy loading, and skill packs

`ozzgraph.skills` (PR17) implements AGENTS.md rule #6: **advertise summaries
first; load full cards only when selected**.

- `SkillSummary` — the compact advertisement: `skill_id`, `name`, `phases`,
  and a one-line `description` (bounded). This is exactly what the context
  compiler offers the model.
- `Skill` — the full card: `skill_id`, `name`, `phases`, `description`,
  bounded `card` text (purpose, bounded command guidance consistent with the
  policy gate's command families, and an explicit "Do NOT" list),
  `timeout_seconds` (the skill's default action timeout), and `parsers`
  (mappings of output shapes to registered `(source, kind)` parser keys).
- `SkillRegistry` — `list_summaries(phase)` is the cheap advertisement path
  (sorted by `skill_id`); `load(skill_id)` fetches the full card;
  `parsers_for(skill_id)` resolves parser keys to live
  `~ozzgraph.observations.Parser` instances (an unregistered key fails
  loudly). Unknown ids raise `SkillRegistryError`.

The initial packs (12 skills) cover RECON, ENUMERATION, EXPLOITATION,
FLAG_HUNT, and VERIFY_AND_SUBMIT:

| Skill | Phases | Default timeout |
|---|---|---|
| `recon_dns_enum` | RECON | 60 s |
| `recon_http_fingerprint` | RECON | 60 s |
| `recon_port_probe` | RECON | 90 s |
| `enum_web_content` | ENUMERATION | 90 s |
| `enum_service_version` | ENUMERATION | 60 s |
| `enum_http_application` | ENUMERATION | 90 s |
| `exploit_parameter_injection` | EXPLOITATION | 90 s |
| `exploit_command_injection` | EXPLOITATION | 90 s |
| `exploit_auth_bypass` | EXPLOITATION | 60 s |
| `flag_hunt_filesystem` | FLAG_HUNT | 60 s |
| `flag_hunt_web_artifacts` | FLAG_HUNT | 60 s |
| `flag_hunt_submit` | FLAG_HUNT, VERIFY_AND_SUBMIT | 30 s |

**Adding a skill** is one explicit registration:

```python
from ozzgraph.phases import Phase
from ozzgraph.skills import SKILLS, Skill, register_skill

register_skill(
    Skill(
        skill_id="exploit_sqli",
        name="SQL injection",
        phases=(Phase.EXPLOITATION,),
        description="SQL injection: parameter probing and bounded extraction",
        card=(
            "Purpose: confirm and exploit an SQL injection safely.\n"
            "Commands (bounded): sqlmap --url <target> --batch --level 1\n"
            "Do NOT: run large wordlists in one action or use --os-shell."
        ),
        timeout_seconds=90,
        parsers=(("shell", "text"),),
    )
)
assert "exploit_sqli" in SKILLS
```

A registry instance snapshots its mapping at construction, so tests and
isolated consumers pass their own dict without mutating the module-level
`SKILLS`.

## 4. Scope policy and duplicate detection

`ozzgraph.policy` (PR10) implements AGENTS.md Security Boundaries steps 3–8 as
a gate that runs BEFORE anything executes. `ScopePolicy.check(command, phase=...,
worker_scope=...)` enforces, in order:

1. **Command length** — `OZZGRAPH_MAX_COMMAND_LENGTH` (default 4096) →
   `CommandLengthError`.
2. **Target allowlist** — destinations extracted by a deterministic heuristic
   (URL authorities, network-verb arguments, bare IP literals; not a shell
   parser — defense in depth, not a sandbox). Hostnames/IPs/CIDRs must be in
   `OZZGRAPH_TARGET_ALLOWLIST`; the default is **empty = fail closed**.
3. **Platform/public-internet blocks** — well-known metadata endpoints
   (`169.254.169.254`, `metadata.google.internal`, ...), loopback, and
   link-local addresses are rejected unless explicitly allowlisted
   (`PlatformDestinationError`); public addresses that are not allowlisted →
   `PublicInternetError`.
4. **Command families** — commands classify into families (`shell`, `recon`,
   `exploit`) by program basename. The policy level
   (`OZZGRAPH_ALLOWED_COMMAND_FAMILIES`, default `shell,recon,exploit`) is
   narrowed per phase (`_PHASE_FAMILIES`:

   `BOOTSTRAP`/`RECON`/`ENUMERATION`/`PIVOT` → `shell,recon`;
   `EXPLOITATION`/`POST_EXPLOITATION` → `shell,exploit`;
   `FLAG_HUNT`/`VERIFY_AND_SUBMIT`/`REPLAN` → `shell`; `DONE` → none) and per
   worker scope. Unknown phases fail closed.
5. **Normalized fingerprint** — whitespace collapsed, `sh -c` wrappers
   unwrapped, trivial trailing shell noise stripped, casefolded, then sha256.
   The fingerprint is the stable identity carried into events and actions.
6. **Duplicate rejection** — the `FingerprintStore` rejects already-recorded
   fingerprints (`DuplicateActionError`); every approved fingerprint is
   mirrored to `<state_dir>/duplicates.jsonl`.

Fingerprinting is a loop-prevention heuristic, not a semantic-equivalence
oracle: semantically-identical commands hash the same, while genuinely
distinct commands usually do not. All rejections are typed
`ScopeViolationError` subclasses so the supervisor can classify and log each
one precisely.

**Tuning**: the operator-facing knobs are environment variables (see
[USAGE.md](USAGE.md) § 2.1); the phase-family table and the command-family
vocabulary live in `policy.py` (`_PHASE_FAMILIES`, `_RECON_COMMANDS`,
`_EXPLOIT_COMMANDS`).

## 5. Hint policy

`ozzgraph.hints` (PR23, [ADR-0003](adr/0003-hint-policy.md)) implements the
deterministic paid-hint gate. **Hint zero is free and automatic** — bootstrap
requests it, it is not privileged, and it is never gated. A **paid hint**
(`index > 0`) is supervisor-only and purchased only when every rule of
`HintPolicy.evaluate` passes (the gate never touches the wire; it is a pure
function of the authoritative graph):

| Rule | Meaning |
|---|---|
| Privilege | The injected client must be privileged (`HintPrivilegeError` otherwise); `HalClient.request_hint` double-guards `index > 0`. |
| Budget | The paid-hint count (number of persisted `hint_purchase` entities) must be below `OZZGRAPH_MAX_HINTS` (default 1). |
| No recent information gain | No `fact`, `evidence`, or `observation` entity created after the latest assessment anchor (the later of the latest purchase and the latest `evaluation`). Fail-closed without an anchor. |
| Exhausted low-cost actions | Every step of the latest `plan` has at least one attempted `action` bound via `plan_step_id`. |
| Two evaluator recommendations | At least two distinct `hint_recommendation` entities (`hint-rec-<sha256(evaluation_id)>`, idempotent per evaluation). |
| Sufficient expected-value gain | `(1 - progress) * min(1, attempts / EV_STALL_FLOOR) >= 0.5`, where `progress` is the fraction of the latest plan's steps completed in the latest evaluation. |

The `HintCoordinator` is the only kernel caller of `request_hint` for
`index > 0`. It serializes the whole check-then-request-then-persist sequence
under an `asyncio.Lock` (concurrent evaluations can never double-purchase) and
re-reads the budget inside the lock. Purchases persist as `hint_purchase`
entities (`hint-purchase-<seq>` — the entity count IS the paid-hint count) and
emit `hint.*` run events; every graph mutation is mirrored as `graph.*` events
so replay reconstructs the identical graph hash.

**Extension**: `HintPolicy` rules are deterministic predicates over the graph;
adding a rule means adding one predicate to the gate and a documented reason
string — the coordinator and event flow stay unchanged.

## 6. Specialist workers

`ozzgraph.workers` (PR25, [ADR-0005](adr/0005-worker-scopes.md)) provides
scope-limited workers the scheduler (PR24) drives. A `WorkerScope` is an
immutable contract: `name`, `command_families` (subset of the policy gate's
families; `shell` is never implied), `phases` (canonical Phase order), 
`mutating` (read-only workers can never run a mutating-family command), and
optional `target_allowlist` narrowing. Construction validates loudly: empty
scopes, unknown/duplicate families, and read-only+mutating contradictions are
rejected before the worker can ever run.

The mutation partition is deterministic:
`MUTATING_COMMAND_FAMILIES = {"exploit"}`. Evidence gathering (recon/shell)
parallelizes; exploit chains and rate-limited credential attacks serialize.

`SpecialistWorker`:

- `assign(task)` — a task is assigned only when the worker's scope covers the
  task's required scope (`TaskOutOfScopeError` otherwise, before any
  execution).
- `run_task(task)` — re-checks the assignment, gates the task's one bounded
  command through `ScopePolicy` (worker families as `worker_scope`), records
  the fingerprint, executes through the bounded shell runner, stores bounded
  output as content-addressed artifacts, and returns a `TaskOutcome` carrying
  one structured `Finding` with provenance (task id, worker source) and
  mandatory evidence references.

Concrete workers shipped: `ReconWorker` (read-only `recon`/`shell`,
BOOTSTRAP/RECON/ENUMERATION/PIVOT), `ArtifactAnalysisWorker` (read-only
`shell`, ENUMERATION/POST_EXPLOITATION/FLAG_HUNT), and `SubmissionWorker` —
the supervisor-serialized wrapper that refuses any task without the reserved
`SERIALIZED_CONFLICT_KEY` (`serialized_task()` is the only way to build such
tasks), so flag submission can only ever run serialized and supervisor-wired.
The same wrapper shape composes paid-hint purchases.

**Adding a worker**:

```python
from ozzgraph.phases import Phase
from ozzgraph.workers import SpecialistWorker, WorkerScope


class MyReconWorker(SpecialistWorker):
    scope = WorkerScope(
        name="my-recon",
        command_families=("recon", "shell"),
        phases=(Phase.RECON, Phase.ENUMERATION),
        mutating=False,
    )
    worker_id = "my-recon"
    default_confidence = 0.7
```

## 7. Reducer and conflict handling

`ozzgraph.reducer` (PR26) turns structured `Finding`s (carried by persisted
`worker_run` entities) into authoritative `fact` graph entities **after**
validating their evidence references (AGENTS.md rule #3 — a finding without
evidence is model prose and is rejected loudly as `UnresolvedEvidenceError`,
never merged, never silently dropped). Key properties:

- **Deterministic fact ids** — `fact-<sha256(fingerprint)>` where the
  fingerprint is `{task_id}:{source}:{sorted(evidence_ids)}:{summary}`
  (reproducible when replaying events).
- **Idempotent, conflict-safe merge** — an existing fact is skipped, and
  `FACT DERIVED_FROM EVIDENCE` edges are created only when missing (and only
  toward graph-entity references; artifact-only references stay in the fact
  payload as provenance). Reducing the same worker runs twice writes nothing
  new; identical findings dedupe to one fact.
- **Conflicts are additive** — contradictory findings (same evidence,
  different summary/confidence) have different fingerprints and merge as
  separate facts with provenance; resolution is downstream. This is the
  "conflict handling" of the reducer, distinct from **scheduler-level task
  conflicts** (PR24), where overlapping conflict keys make tasks mutually
  exclusive at run time.

Failed `WorkerRun`s carry no findings and are skipped. Every mutation is
mirrored as a same-timestamp `graph.*` event (producer `reducer`), so replay
reconstructs the identical graph hash.

## 8. Graph-driven phase routing

`ozzgraph.router` (PR18) is the deterministic bridge between the SQLite state
graph and the rest of the harness: `PhaseRouter.route(graph)` returns the next
`Phase` **only through graph-state predicates** — never action counts or
timers (AGENTS.md rule #8). The transition table is evaluated top to bottom,
first match wins, and always terminates (the leading `graph_is_empty`
predicate matches the empty graph → BOOTSTRAP; the trailing `default_replan`
matches every non-empty graph → REPLAN):

| # | Predicate | Routes to | Graph condition |
|---|---|---|---|
| 1 | `graph_is_empty` | BOOTSTRAP | No entities at all. |
| 2 | `has_accepted_submission` | DONE | A submission with `accepted: true` (terminal). |
| 3 | `has_verified_flag` | VERIFY_AND_SUBMIT | A verified, non-rejected flag candidate. |
| 4 | `targets_unconfirmed` | RECON | No target, or a non-pivot target without `confirmed: true`. |
| 5 | `has_uncharacterized_services` | ENUMERATION | A service without `characterized: true`. |
| 6 | `has_supported_exploitable_hypothesis` | EXPLOITATION | A hypothesis with `exploitable: true` and `EVIDENCE SUPPORTS HYPOTHESIS` incoming edge. |
| 7 | `has_new_access` | POST_EXPLOITATION | A valid credential not yet `explored`. |
| 8 | `has_new_reachable_targets` | PIVOT | A pivot-discovered target that is `reachable: true`. |
| 9 | `has_access_but_no_flag` | FLAG_HUNT | Valid access exists but no verified flag candidate. |
| 10 | `default_replan` | REPLAN | Any non-empty graph (fallback). |

Payload conventions: lowercase entity types (`target`, `service`,
`hypothesis`, `credential`, `flag_candidate`, `submission`), uppercase edge
types (`SUBMISSION SUBMITS FLAG_CANDIDATE`, `FLAG_CANDIDATE OBSERVED_IN
EVIDENCE`, `EVIDENCE SUPPORTS HYPOTHESIS`), strict-boolean payload fields.
The router validates what it reads: a present-but-non-boolean payload field
raises `InvalidGraphStateError`; an accepted submission or verified flag
candidate missing its invariant-critical provenance edge raises
`MissingRequiredStateError`. Nothing is coerced or swallowed.

`PhaseRoute` carries the matched `predicate` name and the skill summaries
covering the routed phase (resolved through the registry — no second lookup
downstream).

**Extending routing** means adding a `Transition(predicate_name, phase,
check)` to the `TRANSITIONS` table in evaluation order, where `check` is a
pure async predicate over `StateGraph`. Terminal states outrank working phases
by position.

## 9. Cross-cutting conventions

- **Determinism**: every registry is a plain dict; lookups, ranking, ids, and
  summaries are deterministic; no randomness, no wall-clock ordering
  decisions, no dynamic imports.
- **Fail loudly**: every layer raises typed error hierarchies
  (`ScopeViolationError`, `AdapterParseError`, `SkillRegistryError`,
  `WorkerScopeError`, `ReducerError`, `PhaseRouterError`, ...) — nothing is
  silently filtered or coerced.
- **Replay compatibility**: every graph mutation shares one timestamp with its
  `graph.*` event, so the append-only log always reconstructs the same graph
  hash ([DATA_STRATEGY.md](DATA_STRATEGY.md), [GOLDEN_TRACES.md](GOLDEN_TRACES.md)).
- **Tests for extensions**: adapter changes need format-compliance,
  malformed-output, and contradiction fixtures; parser changes need success,
  failure, truncation, and adversarial output fixtures; kernel changes need
  unit + integration + replay/golden-trace + failure-path tests
  (AGENTS.md Testing Expectations, [TESTING_AND_QA.md](TESTING_AND_QA.md)).

## 10. Related documentation

- [USAGE.md](USAGE.md) — install, configuration, running a capture.
- [ARCHITECTURE.md](ARCHITECTURE.md) — component overview, phase model,
  safe-parallel-work model.
- [API_AND_INTEGRATIONS.md](API_AND_INTEGRATIONS.md) — full contracts: model
  service, HalCTF integration, shell runner, bootstrap, parsers, skill
  registry, phase router, executor, flags/submission, hint policy, scheduler,
  workers, state graph, dashboard.
- [DATA_STRATEGY.md](DATA_STRATEGY.md) — entity/edge/payload conventions.
- [TECHNICAL_REQUIREMENTS.md](TECHNICAL_REQUIREMENTS.md) — model discovery,
  adapter requirements, hint policy, flag submission.
- [PRD.md](PRD.md) — product goals and non-goals.
- ADRs — [0003](adr/0003-hint-policy.md) (hint policy),
  [0004](adr/0004-task-dag-scheduler.md) (scheduler),
  [0005](adr/0005-worker-scopes.md) (worker scopes).
