# Data Strategy

## Principles

1. Transcripts are not authoritative memory.
2. Every confirmed fact has provenance.
3. Raw artifacts remain retrievable.
4. Compaction does not destroy history.
5. State changes are append-only and replayable.
6. Model claims remain hypotheses until verified.

## Storage Layers

### Append-Only Event Log

`/state/actions.jsonl`

Every meaningful event includes:

- event ID
- run ID
- timestamp
- event type
- producer
- task and worker IDs
- schema version
- payload

### SQLite State Graph

Core entities:

- Run
- Challenge
- Target
- Service
- Endpoint
- Action
- Observation
- Artifact
- Evidence
- Fact
- Hypothesis
- Plan
- PlanStep
- Task
- WorkerRun
- Credential
- FlagCandidate
- Submission
- HintPurchase
- HintRecommendation
- ModelCall
- Checkpoint

Core relationships:

```text
SERVICE OBSERVED_ON TARGET
ENDPOINT EXPOSED_BY SERVICE
ACTION PRODUCED OBSERVATION
OBSERVATION STORED_AS ARTIFACT
EVIDENCE EXTRACTED_FROM OBSERVATION
EVIDENCE SUPPORTS HYPOTHESIS
EVIDENCE CONTRADICTS HYPOTHESIS
PLANSTEP TESTS HYPOTHESIS
TASK IMPLEMENTS PLANSTEP
CREDENTIAL OBSERVED_IN ARTIFACT
FLAG_CANDIDATE OBSERVED_IN EVIDENCE
SUBMISSION SUBMITS FLAG_CANDIDATE
WORKER_RUN EXPLORED HYPOTHESIS
FACT DERIVED_FROM EVIDENCE
```

The graph stores entity and edge types as plain strings, so the lists above
are semantic contracts rather than SQL enums. Convention: entity types are
stored in lowercase (`run`, `action`, `hypothesis`, ...), edge types in upper
snake case (`ACTION PRODUCED OBSERVATION`), both caller-supplied stable
strings.

`hint_purchase` entities (PR23) are entity-only — no edge — because the
count of entities IS the paid-hint ledger the hint-policy gate's budget
rule reads; `hint_recommendation` entities reference `evaluation`
entities by payload `evaluation_id` and are idempotent per evaluation
(`hint-rec-<sha256(evaluation_id)>`).

`task` entities (PR24) use the caller-supplied task id directly as the
entity id and carry the DAG definition (`depends_on`, `conflict_keys`,
`plan_step_id`, `hypothesis_id`); `worker_run` entities are
`worker-run-<sha256(run_id:task_id)>` and carry the run's status,
timestamps, structured findings, and error. Findings are typed (task
id, source, evidence/artifact ids, confidence) but stay embedded in
their `worker_run` payload until the reducer (PR step 26) promotes them
into `evidence`/`fact` entities — the scheduler itself never merges a
finding into the graph as authoritative state (AGENTS.md rule #3).

`fact` entities (PR26) are the authoritative merge of a validated
finding: `fact-<sha256(task_id:source:sorted(evidence_ids):summary)>` —
deterministic and replay-stable — carrying `task_id`, `source`,
`evidence_ids`, the bounded `summary` (never authoritative by itself),
and `confidence`. Each fact links to every evidence it derives from via
`FACT DERIVED_FROM EVIDENCE` (fact -> evidence). The reducer merges a
finding only when every evidence reference resolves (a graph `evidence`
entity or an artifact known to the artifact store's index); a finding
with an unresolvable reference is rejected loudly
(`UnresolvedEvidenceError` with the exact unresolved ids, counted in
`ReducerResult.rejected` and surfaced as a `reducer.*` run event) and is
never represented in the graph. The merge is idempotent and
conflict-safe: identical findings dedupe to one fact, and contradictory
findings (same evidence, different summary) merge as separate facts with
provenance — conflict resolution is downstream.

Concrete schema details (see `src/ozzgraph/state_graph.py`):

- `entities(id, type, data, created_at, updated_at)`; a `data_version`
  payload-version column is added by migration 2.
- `edges(id, type, src_id, dst_id, data, created_at)`; both endpoints are
  foreign keys to `entities` with `ON DELETE CASCADE`, and the
  `(type, src_id, dst_id)` triple is unique so duplicate relationships are
  rejected.
- The schema version lives in `PRAGMA user_version`; forward-only migrations
  apply in ascending order, and reopening an up-to-date database is a no-op.

### Artifact Store

`/state/artifacts`

Artifacts include:

- stdout
- stderr
- scanner output
- HTTP bodies
- downloaded files
- binary files
- generated scripts
- transcripts
- parser sidecars

Each artifact records:

- ID
- hash
- MIME type
- size
- source action
- target
- creation timestamp
- truncation state
- parser metadata
- sensitivity classification

### Checkpoints

A checkpoint contains:

- phase
- plan
- confirmed facts
- active hypotheses
- rejected hypotheses
- credentials
- flag candidates
- recent actions
- artifact index
- budgets
- model profile
- graph version

## Fact Lifecycle

```text
MODEL CLAIM
    ↓
HYPOTHESIS
    ↓
ACTION
    ↓
OBSERVATION
    ↓
EVIDENCE
    ↓
VALIDATION
    ↓
CONFIRMED FACT
```

No model statement may bypass this lifecycle.

## Confidence

| Range | Meaning |
|---|---|
| 1.00 | direct deterministic evidence |
| 0.90–0.99 | strong parsed evidence |
| 0.70–0.89 | multiple supporting observations |
| 0.40–0.69 | plausible hypothesis |
| below 0.40 | weak or speculative |

## Context Retrieval

The context compiler should query a bounded relevant subgraph based on:

- current task
- target
- service
- hypothesis
- phase
- artifact references
- recency
- confidence
- contradiction state

It must not dump the complete graph.

## Replay

Replaying all append-only events shall:

- reconstruct the same entity and edge set
- reproduce the same graph hash
- reproduce submissions and budget state
- preserve schema version information

## Retention

During a run:

- preserve all events
- preserve all artifacts
- compact only active model context

After a run:

- export a compressed run bundle
- preserve hashes when large artifacts are removed
- optionally redact credentials in user-facing views
- retain enough information for deterministic replay

## Versioning

Every persistent contract includes:

- `schema_version`
- `producer_version`
- `run_version`

Migrations must be forward-only and tested against fixtures.
