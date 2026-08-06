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
- Hint
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
```

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
