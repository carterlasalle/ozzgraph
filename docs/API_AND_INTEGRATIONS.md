# API and Integrations

## OpenAI-Compatible Model Service

Required endpoints:

```http
GET /v1/models
POST /v1/chat/completions
```

The client (`src/ozzgraph/model_client.py`) shall support:

- streaming and non-streaming responses
- bounded retries with exponential backoff
- request timeouts
- token usage extraction (missing or malformed `usage` fails loudly with a typed error)
- model ID logging
- structured-output requests where supported (`response_format` passthrough)
- provider error normalization

```python
class ModelService:
    async def list_models(self) -> list[ModelInfo]: ...
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
    async def stream_complete(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...
```

Configuration is constructor-injected with environment fallback; secrets
never enter the config model:

| Env var | Default | Meaning |
|---|---|---|
| `OZZGRAPH_MODEL_BASE_URL` | `http://127.0.0.1:8000/v1` | Base URL including the API prefix |
| `OZZGRAPH_MODEL_API_KEY` | *(unset)* | Optional bearer token (`Authorization: Bearer`) |
| `OZZGRAPH_MODEL_TIMEOUT_S` | `60` | Request timeout in seconds |
| `OZZGRAPH_MODEL_MAX_RETRIES` | `3` | Retries for transient failures; `0` disables, bounded to max 10 |

Streaming (`stream_complete`) returns an async iterator of typed
`ModelStreamEvent` objects — one per content delta plus a final event
carrying `usage` when the provider returns it. An async iterator was chosen
over an async context manager so callers can `async for` directly and the
iterator owns response acquisition and cleanup. Retries are bounded to the
request-acquisition phase; a stream already in flight is never re-sent.

Every failure is raised as a single typed `ModelServiceError` carrying
`provider`, `status_code`, `retryable`, and `message`. Retries apply only to
transient failures (HTTP 429, HTTP ≥ 500, and httpx transport errors);
4xx statuses (400/401/403/404/422) never retry, and backoff is exponential
and bounded (no infinite retry). Whenever an `EventLog` is provided, a
`model_failure` event (producer `model_client`, payload
`provider`/`status`/`attempts`) is appended to the run log alongside the
raised error.

## HalCTF Integration

Models do not call MCP directly.

A local terminal-native adapter exposes:

```bash
halctl challenge show --json
halctl status --json
halctl submit --flag 'flag{...}' --json
halctl hint --index 0 --json
halctl scoreboard --json
halctl exit --reason solved
```

Internal client:

```python
class HalClient:
    async def get_challenge(self, challenge_id: str) -> Challenge: ...
    async def get_status(self, challenge_id: str) -> ChallengeStatus: ...
    async def submit_flag(
        self,
        challenge_id: str,
        flag: str,
    ) -> SubmissionResult: ...
    async def request_hint(
        self,
        challenge_id: str,
        index: int,
    ) -> HintResult: ...
    async def graceful_exit(self, reason: str) -> None: ...
```

Privileged methods:

- submit flag
- purchase paid hint
- graceful exit

Only the supervisor may invoke privileged methods.

Configuration is constructor-injected with environment fallback; the
``OZZGRAPH_HAL_PRIVILEGED`` variable marks a supervisor-owned client:

| Env var | Default | Meaning |
|---|---|---|
| `OZZGRAPH_MCP_BASE_URL` | `http://127.0.0.1:9000/mcp` | Base URL including the MCP endpoint path |
| `OZZGRAPH_MCP_TIMEOUT_S` | `60` | Request timeout in seconds |
| `OZZGRAPH_MCP_MAX_RETRIES` | `3` | Retries for transient failures; `0` disables, bounded to max 10 |
| `OZZGRAPH_HAL_PRIVILEGED` | *(unset)* | Supervisor flag; only a privileged `halctl`/`HalClient` may submit flags, buy paid hints, or exit the run |
| `OZZGRAPH_CHALLENGE_ID` | *(unset)* | Challenge id used by the `halctl` subcommands that need one |

The wire protocol is JSON-RPC 2.0 (`challenge.get`, `challenge.status`,
`flag.submit`, `hint.request`, `scoreboard.get`, `exit`). Every upstream
response is normalized into an internal versioned schema
(`Challenge`/`ChallengeStatus`/`SubmissionResult`/`HintResult`/`Scoreboard`)
so upstream changes do not leak throughout the codebase.

Every failure is raised as a single typed `HalServiceError` carrying
`provider`, `status_code`, `retryable`, and `message`. Retries apply only to
transient failures (HTTP 429, HTTP ≥ 500, JSON-RPC `-32603`, and httpx
transport errors); 4xx statuses (400/401/403/404/422) and application
JSON-RPC errors never retry, and backoff is exponential and bounded (no
infinite retry). Whenever an `EventLog` is provided, a `hal_failure` event
(producer `hal_client`, payload `provider`/`status`/`attempts`) is appended
to the run log alongside the raised error.

## Shell Runner

```python
class ShellRunner:
    async def run(
        self,
        command: str,
        *,
        timeout_seconds: int,
        stdout_limit: int,
        stderr_limit: int,
        working_directory: Path,
    ) -> ToolResult: ...
```

`ToolResult` includes:

- action ID
- command
- exit code
- stdout
- stderr
- duration
- timeout state
- truncation state
- artifact IDs

## Skill Registry

```python
class SkillRegistry:
    def list_summaries(self, phase: Phase) -> list[SkillSummary]: ...
    def load(self, skill_id: str) -> Skill: ...
    def parsers_for(self, skill_id: str) -> list[Parser]: ...
    def timeout_for(self, skill_id: str) -> int: ...
```

## State Graph

```python
class StateGraph:
    def publish_event(self, event: GraphEvent) -> None: ...
    def add_fact(self, fact: Fact) -> None: ...
    def add_hypothesis(self, hypothesis: Hypothesis) -> None: ...
    def add_evidence(self, evidence: Evidence) -> None: ...
    def relevant_subgraph(self, query: ContextQuery) -> StateView: ...
    def checkpoint(self) -> Checkpoint: ...
    def replay(self, events: Iterable[GraphEvent]) -> None: ...
```

## Optional Dashboard API

The dashboard is separate from the competition image.

```http
GET /api/runs
GET /api/runs/{run_id}
GET /api/runs/{run_id}/graph
GET /api/runs/{run_id}/events
GET /api/runs/{run_id}/artifacts/{artifact_id}
GET /api/runs/{run_id}/metrics
POST /api/runs/{run_id}/replay
```

## Integration Failure Policy

All external calls must have:

- timeout
- bounded retries
- exponential backoff
- normalized error type
- structured failure event
- clear effect on budgets
- no infinite retry behavior

## Contract Versioning

Every integration response is normalized into an internal versioned schema so upstream changes do not leak throughout the codebase.
