# Product Requirements Document

## Product

OzzGraph is a model-adaptive autonomous CTF agent harness for authorized, isolated environments.

## Vision

Make smaller open-weight models behave like disciplined security operators by externalizing memory, tool knowledge, execution control, evidence, scheduling, and recovery into software.

## Problem

Small models often fail autonomous security tasks because they:

- repeat commands
- forget discoveries
- misuse tools
- treat guesses as facts
- lose objectives after context compaction
- consume excessive tokens
- overuse broad scans
- guess flags
- fail to recover from bad plans
- receive interfaces unlike those seen during training

The product must optimize the complete model–harness system rather than the model alone.

## Goals

1. Fully autonomous operation from startup through flag submission.
2. Model-specific interaction adapters.
3. Terminal-native interfaces where appropriate.
4. Durable graph state outside model context.
5. Evidence provenance for every confirmed fact.
6. Planner–Executor–Evaluator control.
7. Lazy skills and bounded tool exposure.
8. Bounded specialist workers.
9. Token, time, and action efficiency.
10. Replayable traces and deterministic state reconstruction.
11. Small OCI image suitable for HalCTF-style deployment.
12. Loud, explainable failure behavior.

## Non-Goals

- Real-world unauthorized testing
- Public-internet reconnaissance
- Bundled local model inference
- Unlimited multi-agent swarms
- A vector database in v1
- A dashboard inside the competition image
- Automatic fine-tuning
- Arbitrary raw MCP exposure

## Users

### Competition participant

Needs reliable autonomous performance, small image size, clear logs, and graceful failure.

### Researcher

Needs repeatable experiments, model–harness comparison, golden traces, and metric exports.

### Harness developer

Needs stable contracts, modular extensions, typed schemas, and strong tests.

## Core User Journey

1. Container starts.
2. Supervisor prints identity and starts heartbeat.
3. Challenge configuration is loaded.
4. Smoke-test flag and free hint are processed.
5. Deterministic bootstrap reconnaissance runs.
6. Phase router selects work.
7. Planner creates a bounded plan when branching is meaningful.
8. Executor performs one controlled action.
9. Tool output is stored, parsed, and converted into evidence.
10. Evaluator continues, revises, or abandons the plan.
11. Specialist workers may investigate independent hypotheses.
12. Candidate flags are checked for provenance.
13. Supervisor submits a valid candidate.
14. Agent exits gracefully with a complete summary.

## Success Metrics

- maximize challenge solve rate
- minimize tokens per solve
- minimize tool and model calls per solve
- repeated-action rate below 2%
- invalid output below 5% after tuning
- unsupported flag submissions: zero
- out-of-scope commands executed: zero
- deterministic replay: 100%
- kernel unit-test coverage: at least 90%
- overall coverage: at least 80%
- no silent run termination
- competition image target: 1.5 GB or less

## Product Principles

- Models choose experiments; software controls execution.
- Evidence beats narrative.
- Context is compiled, not accumulated.
- Specialists are narrow and bounded.
- Parallelism is used only when information gain exceeds coordination cost.
- Every failure should improve the state graph.
