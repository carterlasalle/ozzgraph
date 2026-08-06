# Architecture

## Overview

OzzGraph consists of a small supervisor kernel surrounded by replaceable model adapters, skills, parsers, schedulers, and integrations.

```text
Supervisor Kernel
    ↓
State and Work Graph
    ↓
Phase Router
    ↓
Planner / Scheduler
    ↓
Context Compiler
    ↓
Model Adapter
    ↓
Policy and Tool Plane
    ↓
Observation and Artifact Pipeline
    ↓
Evaluator and Reducer
    ↺
```

## Components

### Supervisor Kernel

Owns:

- startup
- identity output
- heartbeat
- budgets
- integration clients
- privileged operations
- lifecycle
- recovery
- final summary

It must not contain challenge-category logic.

### State and Work Graph

Stores:

- targets
- services
- endpoints
- actions
- observations
- artifacts
- evidence
- facts
- hypotheses
- plans
- tasks
- workers
- credentials
- flag candidates
- submissions
- hints
- model calls
- checkpoints

### Phase Router

Supported phases:

```text
BOOTSTRAP
RECON
ENUMERATION
EXPLOITATION
POST_EXPLOITATION
PIVOT
FLAG_HUNT
VERIFY_AND_SUBMIT
REPLAN
DONE
```

Transitions are based on graph predicates, not action counts.

### Planner

Runs only when multiple strategic paths exist.

Produces:

- ranked hypotheses
- supporting and contradicting evidence
- bounded plan steps
- skill selection
- completion conditions
- abandonment conditions

### Executor

Receives:

- one objective
- one current hypothesis
- relevant facts
- recent observations
- loaded skills
- failed actions
- a strict output contract

Returns one bounded action.

### Evaluator

Decides:

- continue
- revise
- abandon
- complete

Deterministic conditions are preferred. Model evaluation is used only when evidence is ambiguous.

### Scheduler

Maintains a task DAG and permits parallel work only when tasks are independent and non-conflicting.

### Reducer

Validates worker findings and merges structured evidence into the graph. It never merges free-form model prose as authoritative state.

### Context Compiler

Builds a bounded, model-specific view from the graph.

Context layers:

1. immutable mission context
2. active task context
3. relevant graph projection
4. recent transcript tail
5. loaded skills
6. output contract

### Model Adapters

Supported protocol families:

- terminal-native text
- three-line action format
- structured JSON
- native function calls

The adapter owns prompt compilation, parsing, repair, and protocol-specific limits.

### Tool Plane

Owns:

- command validation
- scope enforcement
- timeouts
- output limits
- process-group termination
- artifact capture
- normalized results
- fingerprints

### Artifact Pipeline

Raw output and downloaded files live outside model context. Parsers return compact summaries and artifact handles.

## Phase Transition Examples

```python
if not graph.targets_confirmed():
    phase = RECON
elif graph.has_uncharacterized_services():
    phase = ENUMERATION
elif graph.has_supported_exploitable_hypothesis():
    phase = EXPLOITATION
elif graph.has_new_access():
    phase = POST_EXPLOITATION
elif graph.has_new_reachable_targets():
    phase = PIVOT
elif graph.has_access_but_no_flag():
    phase = FLAG_HUNT
else:
    phase = REPLAN
```

## Parallelism

Safe parallel work:

- independent service enumeration
- separate artifact analysis
- independent vulnerability hypotheses
- read-only source localization

Unsafe parallel work:

- multiple workers mutating the same session
- concurrent flag submissions
- concurrent hint purchases
- dependent pivot attempts
- parallel rate-limited credential attacks

Default maximum: three workers.

## Architectural Invariants

- the model never owns authoritative state
- every fact has provenance
- every action is bounded
- privileged operations belong to the supervisor
- failed branches remain durable
- compaction never deletes history
- the graph is reconstructable from events
- workers are scope-limited
