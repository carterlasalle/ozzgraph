# Implementation Plan

## Phase 0: Repository and Specifications

Deliver:

- initialize `uv` project
- add linting, formatting, typing, and tests
- add CI skeleton
- add documentation
- add ADR template
- define versioning

Exit:

- `uv sync` works
- empty application launches
- CI runs
- container builds

## Phase 1: Runtime Kernel

Deliver:

- configuration
- identity output
- heartbeat
- budgets
- signals
- structured logging
- graceful exit
- runtime directories

Exit:

- clean startup and shutdown
- no model dependency required

## Phase 2: Integrations

Deliver:

- model client
- model discovery
- MCP client
- `halctl`
- fake model server
- fake MCP server
- retry and timeout logic

Exit:

- list models
- retrieve challenge
- submit smoke flag
- handle transient failure

## Phase 3: State Foundations

Deliver:

- JSONL event log
- SQLite graph
- artifact store
- graph events
- replay
- checkpoints
- migrations

Exit:

- event replay produces identical graph hash

## Phase 4: Tool Plane

Deliver:

- bounded shell runner
- process-group timeout
- truncation
- command policy
- allowlists
- fingerprints
- artifact capture
- core parsers
- bootstrap probes

Exit:

- safe command execution
- duplicate blocking
- scope enforcement

## Phase 5: Model Adapters

Deliver:

- terminal-native adapter
- three-line adapter
- JSON adapter
- optional function-call adapter
- profiles
- protocol probes
- context compiler
- repair

Exit:

- two model families pass adapter tests

## Phase 6: Skills and Phases

Deliver:

- skill schema
- lazy registry
- initial phase skill packs
- graph-driven phase router (done — PR18)

Exit:

- synthetic challenge transitions based on graph state

## Phase 7: Planner–Executor–Evaluator

Deliver:

- schemas (done — PR19)
- planner (done — PR19)
- prompts
- executor loop (done — PR20)
- deterministic evaluator (done — PR21)
- model evaluator fallback (done — PR21)
- plan budgets (done — PR21)
- replanning (done — PR21)
- loop recovery (done — PR21)

Exit:

- wrong hypothesis is abandoned and replaced autonomously

## Phase 8: Flags and Hints

Deliver:

- candidate extraction
- provenance validation
- supervisor submission
- rejected candidate tracking
- free hint
- paid-hint policy

Exit:

- no unsupported submission
- no direct model hint access

## Phase 9: Workers

Deliver:

- task DAG
- scheduler
- conflict keys
- worker isolation
- structured findings
- reducer
- maximum concurrency

Exit:

- independent tasks run concurrently
- conflicting tasks serialize

## Phase 10: Test Lab

Deliver:

- synthetic targets
- golden traces
- model–adapter matrix
- adversarial fixtures
- chaos tests
- performance tests

Exit:

- all critical failure paths automated

## Phase 11: Dashboard

Outside competition image.

Deliver:

- Yarn workspace
- run timeline
- state graph
- task DAG
- artifact browser
- metrics
- replay

## Phase 12: Competition Hardening

Deliver:

- image minimization
- dependency audit
- SBOM
- startup optimization
- memory profiling
- fallback verification
- complete rehearsal
- immutable image

Exit:

- size target met
- no public dependency
- all quality gates pass

## Pull Request Sequence

1. Initialize `uv` project and CI
2. Runtime configuration and supervisor
3. Heartbeat, budgets, lifecycle
4. Structured event logging
5. Model client
6. MCP client and `halctl`
7. SQLite state graph
8. Artifact store and replay
9. Bounded shell runner
10. Scope policy and duplicate detection
11. Observation parsers
12. Deterministic bootstrap
13. Model profile and adapter interfaces
14. Terminal-native and three-line adapters
15. JSON adapter and repair
16. Context compiler
17. Skill registry and initial skills
18. Graph-driven phase router
19. Planner and schemas
20. Executor loop (done — PR20)
21. Evaluator and replanning (done — PR21)
22. Flag provenance and submission
23. Hint policy
24. Task DAG and scheduler
25. Specialist workers
26. Reducer and conflict handling
27. Synthetic lab
28. Golden traces and model matrix
29. Chaos and adversarial tests
30. Optional Yarn dashboard
31. Image hardening
32. v1.0 release candidate

## Definition of Done

v1.0 requires:

- correct container startup
- identity and heartbeat
- authorized-only communication
- model discovery or safe fallback
- at least three protocols
- replayable graph
- provenance for all facts
- normalized tool output
- bounded planning and execution
- hypothesis abandonment
- lazy skills
- bounded parallel workers
- loop prevention
- supervisor-only flags and hints
- synthetic multi-stage solves
- deterministic golden replay
- full CI quality gates
- image size target
- clear termination summary
