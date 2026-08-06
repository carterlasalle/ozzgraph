# Technical Requirements

## Runtime

- Python 3.12
- `uv` dependency management
- Pydantic v2
- SQLite
- JSONL event log
- local artifact filesystem
- OCI-compatible container
- non-root runtime

## Supervisor

The supervisor shall:

- parse required environment variables
- print `USER ID: <HAL_USER_ID>` immediately
- start heartbeat before long operations
- enforce time, token, model-call, tool-call, worker, and hint budgets
- initialize state and artifact directories
- handle `SIGTERM` and `SIGINT`
- retry transient integration failures with bounded backoff
- terminate cleanly with a structured reason

## Bootstrap

The system shall:

- parse single and namespaced target variables
- retrieve challenge status
- inspect startup environment for the smoke-test flag
- request free hint zero when available
- validate target reachability
- run category-appropriate deterministic probes
- record every bootstrap event

## Model Discovery

- query `/v1/models` when available
- map selected model to a local profile
- probe protocol support when profile confidence is low
- use a conservative plain-text fallback for unknown models
- never assume function-call support

## Model Adapter Requirements

Every adapter defines:

- protocol
- prompt compiler
- parser
- repair strategy
- context soft limit
- output token limit
- temperature
- supported roles
- maximum advertised skills
- known failure behavior

## Tool Execution

Every command shall have:

- validated schema
- scope check
- permission check
- normalized fingerprint
- timeout
- stdout and stderr limits
- process-group cleanup
- action event before execution
- completion event after execution
- optional artifact persistence

## Flag Submission

Only the supervisor may submit.

A flag candidate must:

- appear exactly in an observation or artifact
- reference evidence provenance
- not be previously rejected
- comply with attempt limits
- match known format or challenge-specific rules

## Hint Policy

- hint zero may be automatic
- paid hints are supervisor-only
- maximum one paid hint per detonation
- paid hint requires no recent information gain
- paid hint requires exhausted low-cost actions
- paid hint requires two evaluator recommendations
- paid hint requires sufficient expected-value improvement

## Recovery Requirements

Detect and recover from:

- malformed model output
- repeated commands
- semantically repeated actions
- model timeout
- MCP timeout
- shell timeout
- output overflow
- worker crash
- graph lock
- partial artifact write
- context pressure
- plan exhaustion
- rejected flags
- heartbeat failure

## Performance

- startup under 15 seconds excluding external queueing
- graph query latency suitable for per-turn compilation
- no unbounded retries
- no unbounded output buffering
- no full-artifact insertion into prompts by default
- image target at or below 1.5 GB
