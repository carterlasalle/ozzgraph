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

## Deterministic Bootstrap

Bootstrap reconnaissance runs once at supervisor startup — after `start()`
and heartbeat setup, before the main idle loop — with no model involvement
(docs/TECHNICAL_REQUIREMENTS.md, "Bootstrap"; docs/adr/0002). It parses
targets, retrieves challenge status, processes the smoke-test flag and free
hint zero, and probes every target.

```python
class BootstrapRunner:
    def __init__(
        self,
        *,
        config: OzzGraphConfig,
        run_id: str,
        event_log: EventLog,
        client: HalClient,  # supervisor-owned, privileged
        environ: Mapping[str, str] | None = None,
        probe_runner: ProbeRunner | None = None,
    ) -> None: ...

    async def run(self) -> None: ...


def load_targets(environ: Mapping[str, str]) -> Targets: ...
```

Configuration is read from the environment:

| Env var | Default | Meaning |
|---|---|---|
| `OZZGRAPH_TARGET` | *(unset)* | Single challenge target (a URL, hostname, or IP) |
| `OZZGRAPH_TARGET_<NS>` | *(unset)* | Namespaced targets; `NS` is `HTTP`, `HTTPS`, or `DNS` (e.g. `OZZGRAPH_TARGET_HTTP`) |
| `OZZGRAPH_SMOKE_FLAG` | *(unset)* | When set, submitted once at startup through the privileged `HalClient` as a pipeline smoke test |
| `OZZGRAPH_CHALLENGE_ID` | *(unset)* | Challenge id used for status retrieval, the free hint, and smoke submission (see HalCTF Integration) |
| `OZZGRAPH_TARGET_ALLOWLIST` | *(empty — fail closed)* | Hosts/IPs/CIDRs probes may address (see `OzzGraphConfig.target_allowlist`) |

Every bootstrap step appends exactly one structured event (producer
`bootstrap`): `bootstrap.targets_parsed`, `bootstrap.challenge_status`,
`bootstrap.smoke_submitted`, `bootstrap.hint_requested`,
`bootstrap.hint_unavailable`, `bootstrap.reachability`, `bootstrap.probe_run`,
and `bootstrap.failed`. Hal service failures during a step are recorded in
that step's payload and are not fatal; configuration errors (malformed
target variables, unknown namespace, smoke flag without a challenge id)
record `bootstrap.failed` and terminate the run with a structured `FAILED`
reason.

Probes are deterministic and policy-gated. Each target maps to one fixed
command — `curl -sS --max-time 5 -I <url>` for HTTP/HTTPS targets,
`dig +short +time=2 +tries=1 <host> A` for DNS targets — executed through
`ShellRunner` with explicit timeouts and output limits. Every probe passes
the scope policy (target allowlist — empty means fail closed — plus
platform/public-internet blocks and fingerprints) before execution, and
probe outcomes are recorded as `bootstrap.reachability` /
`bootstrap.probe_run` events, never exceptions.

## Observation and Parser

Raw tool output stays outside model context; parsers normalize it into
compact observations carrying summaries and artifact handles
(docs/ARCHITECTURE.md, "Artifact Pipeline"). The parser layer implements
the `ACTION PRODUCED OBSERVATION` and `OBSERVATION STORED_AS ARTIFACT`
relationships from docs/DATA_STRATEGY.md.

```python
class Parser(ABC):
    source: ClassVar[str]  # registry key, e.g. "shell" or "halctl"
    kind: ClassVar[str]  # parse kind, e.g. "text" or "json"

    def parse(self, raw: ToolResult | str) -> Observation: ...


class Observation(BaseModel):
    action_id: str  # producing Action; "" for raw-str parses
    source: str  # e.g. "shell", "halctl:status"
    kind: str  # parse kind ("text", "json")
    summary: str  # compact summary for model context
    data: dict[str, object]  # validated structured payload
    artifact_ids: list[str]  # OBSERVATION STORED_AS ARTIFACT handles
    truncated: bool  # any stream cut by its output limit
    truncated_streams: list[str]
    exit_code: int | None
    ok: bool | None
    malformed: bool  # unparseable or schema-violating output
    parse_error: str | None  # structured parse failure detail


PARSERS: dict[tuple[str, str], Parser]


def register_parser(parser: Parser) -> None: ...
def get_parser(source: str, kind: str) -> Parser: ...
```

`Observation` includes:

- action ID linking to the producing Action (empty only for raw-string
  parses, which callers must attribute before the graph layer)
- source and parse kind (e.g. `shell`/`text`, `halctl:status`/`json`)
- compact human-readable summary for model context (never the raw output)
- validated structured payload
- artifact handles (`OBSERVATION STORED_AS ARTIFACT`)
- truncation carry-through from `ToolResult.truncation_state`
- exit code and ok flag where applicable
- `malformed` flag and structured `parse_error` for unparseable output

Built-in parsers:

| Parser | source | kind | Input |
|---|---|---|---|
| `ShellTextParser` | `shell` | `text` | generic shell stdout/stderr |
| `HalctlJsonParser` | `halctl` | `json` | halctl single-JSON-document output |

`ShellTextParser` is line-based: ANSI escapes are stripped, control
characters are escaped to visible `\xNN` forms in summaries, and the
structured payload carries line/character counts plus first/last lines.
Target output is untrusted (AGENTS.md Security Boundaries): fake system
instructions, shell control noise, and huge output become labeled data
(summaries carry an "untrusted" prefix) and never crash the parser.

`HalctlJsonParser` classifies halctl's document shapes (challenge /
status / submission / hint / scoreboard / exit / error) into
`source="halctl:<document>"` observations with validated structured
data. Malformed JSON, non-object documents, trailing garbage, and shape
violations produce `malformed=True` with a structured `parse_error` and
a bounded diagnostic excerpt — never a raised exception. The registry is
a plain deterministic dict keyed by `(source, kind)`, populated at
import with the two built-in parsers; it is explicitly not a plugin
system. `parse()` raises only `ParserArgumentError` for caller mistakes
(e.g. non-ToolResult/non-str input); target output never raises.

## Skill Registry

Skills load lazily (AGENTS.md rule #6): the context compiler advertises
compact per-phase summaries, and the full skill card is fetched only after
the model selects a skill. The registry is a plain deterministic dict
(`SKILLS`, keyed by `skill_id`) populated at import — explicitly not a
plugin system, no discovery, no dynamic imports (PR17).

```python
class Phase(str, Enum):
    BOOTSTRAP = "BOOTSTRAP"
    RECON = "RECON"
    ENUMERATION = "ENUMERATION"
    EXPLOITATION = "EXPLOITATION"
    POST_EXPLOITATION = "POST_EXPLOITATION"
    PIVOT = "PIVOT"
    FLAG_HUNT = "FLAG_HUNT"
    VERIFY_AND_SUBMIT = "VERIFY_AND_SUBMIT"
    REPLAN = "REPLAN"
    DONE = "DONE"


class SkillSummary(BaseModel):
    skill_id: str  # bounded identifier (<= 64 chars)
    name: str  # short human name (<= 80 chars)
    phases: tuple[Phase, ...]  # phases this skill covers, canonical order
    description: str  # one-line advertisement, bounded (<= 200 chars)


class Skill(BaseModel):
    skill_id: str
    name: str
    phases: tuple[Phase, ...]  # deduplicated, ordered by the Phase enum
    description: str  # same one-liner the summary advertises
    card: str  # full skill card, loaded lazily on selection
    timeout_seconds: int  # default action timeout (>= 1)
    parsers: tuple[tuple[str, str], ...]  # (source, kind) keys into PARSERS


class SkillRegistry:
    def list_summaries(self, phase: Phase) -> list[SkillSummary]: ...
    def load(self, skill_id: str) -> Skill: ...
    def parsers_for(self, skill_id: str) -> list[Parser]: ...
    def timeout_for(self, skill_id: str) -> int: ...
```

`Phase` values are the uppercase phase names the policy gate already uses
(`ozzgraph.policy.PHASES`), so phase router (PR18), policy, and skills
share one vocabulary. `list_summaries` returns only the skills covering
the given phase, sorted by `skill_id` (deterministic). `load` returns the
full skill card for a selected skill and raises `SkillRegistryError` for
an unknown `skill_id`. `parsers_for` resolves the skill's `(source, kind)`
parser mappings to live `Parser` instances from `PARSERS`; a skill mapping
an unregistered parser key is a broken registry entry and also raises
`SkillRegistryError` (never silently skipped, never an empty list).
`timeout_for` returns the skill's default timeout in seconds. All three
lookup errors are the typed `SkillRegistryError` (fail loudly, AGENTS.md
rule #9).

Initial skill packs (PR17) — 12 skills across RECON, ENUMERATION,
EXPLOITATION, FLAG_HUNT, and VERIFY_AND_SUBMIT:

| skill_id | phases | timeout (s) | parsers |
|---|---|---|---|
| `recon_dns_enum` | RECON | 60 | shell/text |
| `recon_http_fingerprint` | RECON | 60 | shell/text |
| `recon_port_probe` | RECON | 90 | shell/text |
| `enum_web_content` | ENUMERATION | 90 | shell/text |
| `enum_service_version` | ENUMERATION | 60 | shell/text |
| `enum_http_application` | ENUMERATION | 90 | shell/text |
| `exploit_parameter_injection` | EXPLOITATION | 90 | shell/text |
| `exploit_command_injection` | EXPLOITATION | 90 | shell/text |
| `exploit_auth_bypass` | EXPLOITATION | 60 | shell/text |
| `flag_hunt_filesystem` | FLAG_HUNT | 60 | shell/text |
| `flag_hunt_web_artifacts` | FLAG_HUNT | 60 | shell/text |
| `flag_hunt_submit` | FLAG_HUNT, VERIFY_AND_SUBMIT | 30 | halctl/json |

Each skill card is bounded prompt text: purpose, bounded command guidance
consistent with the policy gate's command families, and an explicit
"Do NOT" list. Parser mappings are consistent with the two built-in
parsers (`shell`/`text`, `halctl`/`json`). Nothing is wired into the
supervisor yet — graph-driven phase routing owns that (PR18).

## Phase Router

Phases are derived from graph state, not action counts (AGENTS.md rule
#8): the router evaluates a fixed, documented table of graph-state
predicates — presence or absence of typed entities, typed edges, and
payload fields in the SQLite graph — and returns the first match. It
holds no counters, reads no timestamps, and never consults a stored
`phase` payload field, so the same graph state always routes to the
same phase (PR18).

```python
class PhaseRoute(BaseModel):
    phase: Phase  # the next phase to execute
    predicate: str  # name of the transition predicate that matched
    skills: tuple[SkillSummary, ...]  # registry summaries covering the phase


class PhaseRouter:
    def __init__(self, registry: SkillRegistry | None = None) -> None: ...
    def skills_for(self, phase: Phase) -> tuple[SkillSummary, ...]: ...
    async def route(self, graph: StateGraph) -> PhaseRoute: ...
```

`route` evaluates `TRANSITIONS` top to bottom (first match wins) and
always terminates: the leading emptiness predicate routes the empty
graph to `BOOTSTRAP`, and the trailing default routes any other
unmatched non-empty graph to `REPLAN`. Terminal states outrank working
phases: an accepted submission wins (`DONE`) over any pending work, and
a verified flag candidate wins (`VERIFY_AND_SUBMIT`) over recon.
`skills_for` is the SkillRegistry interop surface (AGENTS.md rule #6):
it returns `registry.list_summaries(phase)`, and `route` attaches those
summaries to the returned `PhaseRoute` so a downstream planner selects
skills without a second lookup.

`route` fails loudly (AGENTS.md rule #9) through the typed
`PhaseRouterError` hierarchy (a `RuntimeError` subclass):

| Error | Raised when |
|---|---|
| `InvalidGraphStateError` | a payload field the router reads is present but not a bool (e.g. `confirmed: "yes"`) |
| `MissingRequiredStateError` | an accepted submission has no `SUBMISSION SUBMITS FLAG_CANDIDATE` edge, or a verified flag candidate has no `FLAG_CANDIDATE OBSERVED_IN EVIDENCE` edge (AGENTS.md data invariants) |

Payload conventions (entity types lowercase, edge types uppercase, per
docs/DATA_STRATEGY.md):

| Field | Meaning |
|---|---|
| `target.confirmed` | recon complete for the target |
| `target.pivot` | target discovered from the foothold (PIVOT routing) |
| `target.reachable` | reachability confirmed (PIVOT routing) |
| `service.characterized` | enumeration complete for the service |
| `hypothesis.exploitable` | hypothesis has an exploitation direction |
| `credential.valid` | credential grants usable access |
| `credential.explored` | post-exploitation already consumed the access |
| `flag_candidate.verified` | flag candidate has observed provenance |
| `submission.accepted` | submission was accepted (terminal signal) |

Transition predicates, in evaluation order:

| # | Predicate | Graph state it matches | Phase |
|---|---|---|---|
| 1 | `graph_is_empty` | no entities at all | `BOOTSTRAP` |
| 2 | `has_accepted_submission` | a `submission` with `accepted: true` and its `SUBMISSION SUBMITS FLAG_CANDIDATE` edge | `DONE` |
| 3 | `has_verified_flag` | a `flag_candidate` with `verified: true` and its `FLAG_CANDIDATE OBSERVED_IN EVIDENCE` edge | `VERIFY_AND_SUBMIT` |
| 4 | `targets_unconfirmed` | no `target`, or a non-pivot `target` without `confirmed: true` | `RECON` |
| 5 | `has_uncharacterized_services` | a `service` without `characterized: true` | `ENUMERATION` |
| 6 | `has_supported_exploitable_hypothesis` | a `hypothesis` with `exploitable: true` and an incoming `EVIDENCE SUPPORTS HYPOTHESIS` edge | `EXPLOITATION` |
| 7 | `has_new_access` | a `credential` with `valid: true` and `explored` not true | `POST_EXPLOITATION` |
| 8 | `has_new_reachable_targets` | a `target` with `pivot: true` and `reachable: true` | `PIVOT` |
| 9 | `has_access_but_no_flag` | a `credential` with `valid: true` and no verified `flag_candidate` | `FLAG_HUNT` |
| 10 | `default_replan` | any other non-empty graph | `REPLAN` |

The predicate list mirrors docs/ARCHITECTURE.md ("Phase Transition
Examples") with two additions: `DONE`/`VERIFY_AND_SUBMIT` are terminal
states evaluated first, and `BOOTSTRAP` is the empty-graph default.
The executor (PR20) consumes `PhaseRouter`; nothing is wired into the
supervisor yet.

## Planner

The planner (PR19) is the first slice of Phase 7
(Planner–Executor–Evaluator). It runs ONLY when the graph is in a
branching state — multiple strategic paths: at least
`MIN_STRATEGIC_PATHS` (default 2) evidenced hypotheses, or at least 2
uncharacterized services — decided by graph predicates (AGENTS.md rule
#8), never action counts. A non-branching graph yields a typed
`NoPlanDecision`; the planner never fabricates a plan.

```python
class Hypothesis(BaseModel):
    id: str  # hypothesis entity id
    phase: Phase  # scope: the routed phase the plan serves
    objective: str  # scope: bounded statement of the hypothesis claim
    rank: int  # 1-based priority position (1 = highest)
    confidence: float  # 0.0..1.0 (payload `confidence`, missing defaults to 0.0)
    supporting_evidence: tuple[str, ...]  # EVIDENCE SUPPORTS HYPOTHESIS source ids
    contradicting_evidence: tuple[str, ...]  # EVIDENCE CONTRADICTS HYPOTHESIS source ids
    exploitation_direction: str | None  # payload `exploitation_direction`, if any


class AbandonCondition(BaseModel):
    condition: str  # deterministic predicate text (evaluated by the evaluator, PR21)
    scope: Phase | str | None  # hypothesis/service id or plan phase; None = global to its carrier
    rationale: str | None  # why the predicate triggers abandonment (optional)


class PlanStep(BaseModel):
    id: str  # plan-scoped step id (`<plan id>-step-<n>`)
    hypothesis_id: str | None  # tested hypothesis; None for service-characterization steps
    objective: str  # bounded action objective
    skill_id: str  # selected skill (round-robin over the route's phase skills)
    completion_condition: str
    abandon_condition: AbandonCondition


class Plan(BaseModel):
    id: str  # run-scoped, deterministic: plan-<phase>-<graph-hash prefix>
    phase: Phase
    hypotheses: tuple[Hypothesis, ...]  # ranked: confidence, then evidence weight, then id
    steps: tuple[PlanStep, ...]  # ordered, bounded by MAX_PLAN_STEPS
    completion_conditions: tuple[str, ...]
    abandonment_conditions: tuple[AbandonCondition, ...]  # plan-level abandon conditions
    skills: tuple[SkillSummary, ...]  # the route's phase skills (registry summaries)


class NoPlanDecision(BaseModel):
    phase: Phase
    reason: str  # deterministic explanation of why no plan was produced


class Planner:
    def __init__(self, registry: SkillRegistry | None = None) -> None: ...
    def skills_for(self, phase: Phase) -> tuple[SkillSummary, ...]: ...
    async def plan(self, graph: StateGraph, route: PhaseRoute) -> Plan | NoPlanDecision: ...
```

`plan` first evaluates the branching predicate (evidenced hypotheses
`>= MIN_STRATEGIC_PATHS` or uncharacterized services `>=
MIN_STRATEGIC_PATHS`) and returns `NoPlanDecision` for non-branching
graphs. On a branching graph it ranks every hypothesis entity by
confidence (descending), then net evidence weight (supporting minus
contradicting evidence counts, descending), then entity id (ascending)
as the final tiebreak, builds one bounded step per ranked hypothesis
(plus one per uncharacterized service, `hypothesis_id=None`), caps the
step list at `MAX_PLAN_STEPS` (default 5), and assigns skills
round-robin from `route.skills` in the registry's deterministic sorted
order. The plan id derives from the graph hash, so the same graph state
always yields the same plan. No randomness, no model calls, no graph
writes — the executor (PR20) persists plans as entities.

`plan` fails loudly (AGENTS.md rule #9) through the typed
`PlannerError` hierarchy (a `RuntimeError` subclass):

| Error | Raised when |
|---|---|
| `InvalidGraphStateError` | a payload field the planner reads is present but wrong-typed or out of range (e.g. `confidence: "high"`, `confidence: 5.0`, `exploitation_direction: 42`) |
| `MissingRequiredStateError` | a hypothesis entity has no evidence refs (no incoming `EVIDENCE SUPPORTS HYPOTHESIS` or `EVIDENCE CONTRADICTS HYPOTHESIS` edge) while a plan is being built |
| `PlannerSkillUnavailableError` | the routed phase has no skill packs (e.g. `REPLAN`), so no step could receive a skill |

Payload conventions for the planner's graph reads (entity types
lowercase, edge types uppercase, per docs/DATA_STRATEGY.md):

| Field | Meaning |
|---|---|
| `hypothesis.confidence` | float in [0.0, 1.0]; missing defaults to 0.0 (weak) |
| `hypothesis.objective` | bounded statement of the claim; missing derives from the hypothesis id |
| `hypothesis.exploitation_direction` | bounded exploitation direction; optional |
| `service.characterized` | strict bool (same field the phase router reads) |

Module constants: `MIN_STRATEGIC_PATHS = 2` (branching floor),
`MAX_PLAN_STEPS = 5` (step cap), `PLAN_COMPLETION_CONDITIONS` (plain
plan-level completion strings) and `PLAN_ABANDONMENT_CONDITIONS`
(plan-level `AbandonCondition` instances, scope `None` — they apply to
the plan as a whole), which the evaluator, PR21, interprets. The
executor (PR20) and evaluator (PR21) consume `Planner`; nothing is
wired into the supervisor yet.

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
