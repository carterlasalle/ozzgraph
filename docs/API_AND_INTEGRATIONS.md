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
halctl ctfs --json
halctl challenges [--ctf-id 'ctf-1'] --json
halctl challenge show --json
halctl status --json
halctl submit --flag 'flag{...}' --json
halctl hint --index 0 --json
halctl scoreboard --json
halctl exit --reason solved
```

Internal client (the official HalCTF MCP tool set — `list_ctfs`,
`challenges`, `status`, `submit_flag`, `request_hint`, `scoreboard`):

```python
class HalClient:
    async def list_ctfs(self) -> CtfList: ...
    async def list_challenges(
        self,
        ctf_id: str | None = None,
    ) -> ChallengeList: ...
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
| `OZZGRAPH_MCP_BASE_URL` | `http://127.0.0.1:9000/mcp` | Base URL including the MCP endpoint path — the FIRST of the deterministic V09 discovery candidates. |
| `HAL_MCP_ENDPOINT` / `HAL_ENDPOINT` / `MCP_ENDPOINT` | — | Additional endpoint candidates, consulted in order after `OZZGRAPH_MCP_BASE_URL` (first non-blank wins; `OPENAI_BASE_URL` is the model service and is never a candidate; see `ozzgraph.config.discover_halctf_endpoint`). |
| `OZZGRAPH_MCP_TIMEOUT_S` | `60` | Request timeout in seconds |
| `OZZGRAPH_MCP_MAX_RETRIES` | `3` | Retries for transient failures; `0` disables, bounded to max 10 |
| `OZZGRAPH_HAL_PRIVILEGED` | *(unset)* | Supervisor flag; only a privileged `halctl`/`HalClient` may submit flags, buy paid hints, or exit the run |
| `OZZGRAPH_CHALLENGE_ID` / `HAL_CTF_ID` / `HAL_CHALLENGE_ID` | *(unset)* | Challenge id used by the `halctl` subcommands that need one (first non-blank wins) |

The wire protocol is JSON-RPC 2.0 (`ctf.list`, `challenge.list`,
`challenge.get`, `challenge.status`, `flag.submit`, `hint.request`,
`scoreboard.get`, `exit`). Every upstream response is normalized into an
internal versioned schema
(`CtfList`/`ChallengeList`/`Challenge`/`ChallengeStatus`/`SubmissionResult`/`HintResult`/`Scoreboard`)
so upstream changes do not leak throughout the codebase.
`ChallengeStatus` carries the smoke-flag signal and the deterministic
scoring breakdown; `HintResult` carries the platform-reported per-hint
cost.

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
the given phase, sorted by `skill_id` (deterministic). `list_for_category`
(HAL-009) returns the summaries of the skills the
`~ozzgraph.techniques.TechniqueClassifier` routes to a challenge category
string (e.g. `"Web / SSRF"`, `"SQL Injection"`, `"Forensics"`, `"Cloud
IAM"`) — an unknown/absent category degrades deterministically to the
recon/enum core, and a classifier mapping that references an unregistered
skill id raises `SkillRegistryError` (fail loudly). `load` returns the
full skill card for a selected skill and raises `SkillRegistryError` for
an unknown `skill_id`. `parsers_for` resolves the skill's `(source, kind)`
parser mappings to live `Parser` instances from `PARSERS`; a skill mapping
an unregistered parser key is a broken registry entry and also raises
`SkillRegistryError` (never silently skipped, never an empty list).
`timeout_for` returns the skill's default timeout in seconds. All these
lookup errors are the typed `SkillRegistryError` (fail loudly, AGENTS.md
rule #9).

Initial skill packs (PR17) — 9 skills across RECON, ENUMERATION, and
EXPLOITATION (V01, docs/adr/0008: the FLAG_HUNT packs left with the
removed phases; the halctf environment owns them in V09), plus the
HAL-009 Tottori lesson packs (2026-08-09) — 17 skills total:

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
| `exploit_sqli_enumeration` | EXPLOITATION | 90 | shell/text |
| `exploit_jwt` | EXPLOITATION | 90 | shell/text |
| `exploit_ssrf` | EXPLOITATION | 90 | shell/text |
| `exploit_xxe` | EXPLOITATION | 90 | shell/text |
| `exploit_deserialization` | EXPLOITATION | 90 | shell/text |
| `exploit_protocol_reversing` | EXPLOITATION | 90 | shell/text |
| `forensics_file_analysis` | ENUMERATION, POST_EXPLOITATION | 90 | shell/text |
| `exploit_cloud_iam` | EXPLOITATION, POST_EXPLOITATION | 90 | shell/text |

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
| `MissingRequiredStateError` | an accepted submission has no `SUBMISSION SUBMITS FLAG_CANDIDATE` edge, or a verified, non-rejected flag candidate has no `FLAG_CANDIDATE OBSERVED_IN EVIDENCE` edge (AGENTS.md data invariants) |

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
| `flag_candidate.verified` | flag candidate has observed provenance (HalCTF-owned; the kernel no longer routes on it, V01) |
| `flag_candidate.rejected` | platform rejected the candidate (PR22) — it is never re-submitted |
| `objective.completed` | objective satisfied — the generic DONE predicate (V01) |
| `submission.accepted` | submission was accepted (terminal signal) |

Transition predicates, in evaluation order (V01, docs/adr/0008:
FLAG_HUNT / VERIFY_AND_SUBMIT left the kernel; the generic DONE
predicate is `all_objectives_completed`):

| # | Predicate | Graph state it matches | Phase |
|---|---|---|---|
| 1 | `graph_is_empty` | no entities at all | `BOOTSTRAP` |
| 2 | `has_accepted_submission` | a `submission` with `accepted: true` and its `SUBMISSION SUBMITS FLAG_CANDIDATE` edge | `DONE` |
| 3 | `all_objectives_completed` | every `objective` entity with `completed: true` (and at least one objective) | `DONE` |
| 4 | `targets_unconfirmed` | no `target`, or a non-pivot `target` without `confirmed: true` | `RECON` |
| 5 | `has_uncharacterized_services` | a `service` without `characterized: true` | `ENUMERATION` |
| 6 | `has_supported_exploitable_hypothesis` | a `hypothesis` with `exploitable: true` and an incoming `EVIDENCE SUPPORTS HYPOTHESIS` edge | `EXPLOITATION` |
| 7 | `has_new_access` | a `credential` with `valid: true` and `explored` not true | `POST_EXPLOITATION` |
| 8 | `has_new_reachable_targets` | a `target` with `pivot: true` and `reachable: true` | `PIVOT` |
| 9 | `default_replan` | any other non-empty graph | `REPLAN` |

The predicate list mirrors docs/ARCHITECTURE.md ("Phase Transition
Examples") with two additions: `DONE` is terminal and evaluated first
(accepted submission or all objectives completed), and `BOOTSTRAP` is
the empty-graph default.
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

## Executor

The executor (PR20) is the bounded one-action-per-turn loop
(docs/ARCHITECTURE.md, "Executor"; PR step 20): it consumes the
graph-driven `PhaseRouter` (PR18) and the deterministic `Planner`
(PR19), validates the model's untrusted action proposal against a
strict output contract, bounds the approved action (skill default
timeout, output limit, policy-gate fingerprint), records the attempted
action before execution (AGENTS.md Security Boundaries step 10; Data
Invariant "Every Observation references an Action"), and returns
exactly ONE typed `ActionRequest` — never a list. Nothing is wired
into the supervisor yet.

```python
class Executor:
    def __init__(
        self,
        *,
        budgets: Budgets,
        run_id: str,
        event_log: EventLog | None = None,
        registry: SkillRegistry | None = None,
        router: PhaseRouter | None = None,
        planner: Planner | None = None,
        policy: ScopePolicy | None = None,
        store: FingerprintStore | None = None,
    ) -> None: ...

    async def turn(
        self,
        graph: StateGraph,
        model_output: object,
        *,
        failed_actions: Sequence[FailedAction] = (),
    ) -> ExecutorTurn: ...
```

One `Executor.turn` checks every bounded budget dimension (runtime,
tokens, model calls, tool calls) and raises
`~ozzgraph.budgets.BudgetExceeded` before anything else (AGENTS.md rule
#9); routes the graph and plans under the routed phase (graph-state
predicates, never action counts — rule #8); selects the next plan step
that has no failed attempt (a plan whose every step has failed raises
`PlanExhaustedError`); validates the model output against the strict
`ModelAction` contract; runs the scope policy gate
(`~ozzgraph.policy.ScopePolicy`, Security Boundaries steps 3-7) to
obtain the action's normalized fingerprint; rejects any fingerprint
that was already attempted (failed history) or already recorded
(`~ozzgraph.policy.FingerprintStore`, step 8); attaches the skill's
default timeout and the module output limit (step 9); persists the
plan as graph entities the first time a plan id is seen; and records
the attempted action as an `action` graph entity keyed by its
fingerprint plus an `executor.action_attempted` run event (step 10).
An approved turn consumes exactly one model call (the call that
produced the proposal) and one tool call (the action this turn will
execute) through the injected `~ozzgraph.budgets.Budgets`.

Schema fields (all pydantic v2 models with `extra="forbid"`):

| Model | Field | Meaning |
|---|---|---|
| `ModelAction` | `action` | The model's proposed action text — a bounded command line or `halctl` invocation (1..`MAX_ACTION_LENGTH` chars) |
| `ModelAction` | `skill_id` | The skill the model selected from the advertised summaries (1..64 chars) |
| `FailedAction` | `fingerprint` | sha256 hex digest of a failed action's canonical form (never retried) |
| `FailedAction` | `reason` | Why it failed (e.g. `timeout`, `output_limit`, `error`) |
| `FailedAction` | `plan_step_id` | The plan step the failed action belonged to, when planned |
| `ActionRequest` | `action` | The bounded action text for the tool plane |
| `ActionRequest` | `skill_id` | The skill that bounds and guides the action |
| `ActionRequest` | `timeout_seconds` | The skill's default action timeout (>= 1) |
| `ActionRequest` | `output_limit` | Per-stream output cap in characters (>= 1) |
| `ActionRequest` | `fingerprint` | sha256 hex digest of the action's canonical form, from the policy gate |
| `ActionRequest` | `phase` | The routed phase the action serves |
| `ActionRequest` | `plan_id` | The plan the action serves, when one was produced |
| `ActionRequest` | `plan_step_id` | The plan step the action implements, when planned |
| `ActionRequest` | `hypothesis_id` | The hypothesis the step tests, when planned |
| `ExecutorTurn` | `phase` | The routed phase this turn served |
| `ExecutorTurn` | `predicate` | The transition predicate that matched the graph state |
| `ExecutorTurn` | `action` | The single bounded action (never a list) |
| `ExecutorTurn` | `budget` | `BudgetAccounting` snapshot after this turn's consumption |
| `BudgetAccounting` | `tokens_used` / `model_calls_used` / `tool_calls_used` | Cumulative usage so far |
| `BudgetAccounting` | `remaining_tokens` / `remaining_model_calls` / `remaining_tool_calls` | Remaining allowance; `None` when unbounded |

`turn` fails loudly through the typed `ExecutorError` hierarchy (a
`RuntimeError` subclass):

| Error | Raised when |
|---|---|
| `MalformedOutputError` | the model output is not a JSON object string or a mapping, or violates the `ModelAction` contract (missing/extra/wrong-typed/over-long fields) |
| `InvalidSkillError` | the model selected a skill that is unknown, does not cover the routed phase, or is not the plan step's assigned skill |
| `DuplicateFingerprintError` | the action's fingerprint was already attempted (failed history) or already recorded (the duplicate store) |
| `PlanExhaustedError` | a plan exists and every one of its steps has a failed attempt |

The scope gate's own typed rejections (allowlist, platform/public-
internet blocks, family/phase permissions) propagate unchanged as
`~ozzgraph.policy.ScopeViolationError` subclasses; budget exhaustion
propagates as `~ozzgraph.budgets.BudgetExceeded`.

Module constants:

| Constant | Value | Meaning |
|---|---|---|
| `MAX_ACTION_LENGTH` | `4096` | Hard bound on one action's text length (mirrors the policy gate's command-length ceiling) |
| `DEFAULT_OUTPUT_LIMIT` | `65536` | Default per-stream output cap (characters) attached to every action |
| `EXECUTOR_ACTION_ATTEMPTED` | `"executor.action_attempted"` | Run event for every approved, recorded attempt |
| `EXECUTOR_PLAN_PERSISTED` | `"executor.plan_persisted"` | Run event when a plan is first persisted as entities |
| `ENTITY_ACTION` / `ENTITY_PLAN` / `ENTITY_PLAN_STEP` | `"action"` / `"plan"` / `"plan_step"` | Entity types the executor writes (lowercase, per docs/DATA_STRATEGY.md) |
| `EDGE_PLANSTEP_TESTS_HYPOTHESIS` | `"PLANSTEP TESTS HYPOTHESIS"` | Edge type linking a plan step to the hypothesis it tests (uppercase) |

Plans are persisted as graph entities the first time a plan id is seen
(a plan id already in the graph is never rewritten): one `plan`
entity (payload: `phase`, `step_count`, ranked `hypotheses`,
`completion_conditions`, `abandonment_conditions`), one `plan_step`
entity per step (payload: `hypothesis_id`, `objective`, `skill_id`,
`completion_condition`, `abandon_condition`), and a `PLANSTEP TESTS
HYPOTHESIS` edge from each step to the hypothesis it tests
(service-characterization steps have no hypothesis and no edge). Every
mutation is mirrored to the append-only event log as a
`graph.entity_created` / `graph.edge_created` event carrying the same
timestamp, so replaying the log reconstructs the identical graph hash.
Plan ids derive from the graph hash (PR19), and the executor's own
persistence is part of that state, so as the graph evolves across
turns the persisted plan entities form the run's plan timeline; the
evaluator (PR21) interprets them. Attempted actions are recorded as
`action` entities keyed by fingerprint (`action-<fingerprint>`) with
the full bounded action payload before the turn returns; the tool
plane attaches observations to them later.

Models never call raw MCP (AGENTS.md rule #5): the executor only ever
produces action TEXT — a command line or a `halctl` invocation that
the tool plane runs through the policy gate and the bounded shell
runner. The executor constructs no MCP client and imports no MCP
surface.

## Flags and Submission

Flag handling (PR22) is the Phase 8 slice "candidate extraction,
provenance validation, supervisor submission, rejected candidate
tracking" (docs/IMPLEMENTATION_PLAN.md step 22; hint policy is PR23).
Two small modules implement it: `ozzgraph.flags` owns deterministic,
provenance-gated candidate extraction, and `ozzgraph.submissions`
owns the supervisor-only submission coordinator. Neither module
touches MCP directly: the coordinator drives the privileged
`HalClient.submit_flag` — the only caller of the wire surface in the
kernel — and every model-visible path (skills, router, executor) can
only produce action text, never a submission.

### Candidate extraction (`ozzgraph.flags`)

A flag candidate is created only when the flag text appears VERBATIM
in an observation (or an artifact that observation references) AND
that observation is backed by at least one evidence entity linked via
`EVIDENCE EXTRACTED_FROM OBSERVATION` (AGENTS.md rule #3: every
confirmed fact has provenance; docs/DATA_STRATEGY.md). A bare model
claim is never a candidate.

```python
class FlagCandidate(BaseModel):
    flag: str  # exact flag text, matched verbatim
    entity_id: str  # "flag-<sha256(flag)>" — deterministic, unique per string
    source_observation_id: str  # the observation the flag appeared in
    evidence_ids: tuple[str, ...]  # backing evidence, ordered by edge id


def flag_candidate_id(flag: str) -> str: ...  # "flag-" + sha256 hex


class FlagCandidateExtractor:
    def __init__(
        self,
        *,
        run_id: str = "flags",
        event_log: EventLog | None = None,
        pattern: str = DEFAULT_FLAG_PATTERN,  # r"flag\{[^{}\s]+\}"
        max_attempts: int = DEFAULT_MAX_SUBMISSIONS,  # 3
        artifact_store: ArtifactStore | None = None,
    ) -> None: ...

    async def extract(self, graph: StateGraph) -> tuple[FlagCandidate, ...]: ...
```

`extract` scans observation entities in id order, resolves their
backing evidence (either edge direction), scans every string in the
observation payload recursively plus the contents of referenced
artifacts (bounded, missing artifacts skipped), and persists one
`flag_candidate` entity per NEW flag string, each with a
`FLAG_CANDIDATE OBSERVED_IN EVIDENCE` edge to every backing evidence
entity. A candidate that already exists — verified, rejected, or at
its attempt budget — is never re-created (idempotent by hash; a
rejected flag is never resurrected). Every mutation is mirrored as a
`graph.entity_created` / `graph.edge_created` event sharing one
timestamp, and each found candidate also emits a
`flags.candidate_found` run event (producer `flags`), so replaying the
log reconstructs the identical graph hash.

`flag_candidate` payload contract (the router reads `verified` and
`rejected` as strict booleans):

| Field | Type | Meaning |
|---|---|---|
| `flag` | `str` | the exact flag text |
| `verified` | `bool` | `true` — the candidate has observed provenance |
| `source_observation_id` | `str` | the observation the flag appeared in |
| `evidence_ids` | `list[str]` | evidence entities backing that observation |
| `rejected` | `bool` | `false` at creation; `true` after a platform rejection — never re-submitted |
| `attempts` | `int` | `0` at creation; counts platform rejections |

Extraction errors (all `RuntimeError` subclasses, AGENTS.md rule #9):

| Error | Raised when |
|---|---|
| `FlagsError` | base error for the extraction layer |
| `InvalidFlagPatternError` | the configured flag pattern is not a valid regular expression (at construction) |
| `FlagsStateError` | an existing candidate's `rejected` / `attempts` payload field is wrong-typed (never coerced) |

### Submission coordinator (`ozzgraph.submissions`)

`SubmissionCoordinator` is the only caller of `submit_flag` in the
kernel. It finds the graph's verified, non-rejected flag candidate,
validates its `FLAG_CANDIDATE OBSERVED_IN EVIDENCE` edge, enforces the
attempt budgets, and drives the privileged client.

```python
class SubmissionCoordinator:
    def __init__(
        self,
        *,
        client: SubmissionClient,  # must be privileged; HalClient satisfies the protocol
        run_id: str,
        challenge_id: str,
        event_log: EventLog | None = None,
        max_submissions: int = DEFAULT_MAX_SUBMISSIONS,  # 3
    ) -> None: ...

    async def submit_verified_candidate(self, graph: StateGraph) -> SubmissionResult: ...
```

`submit_verified_candidate` flow (the executor's "record the attempt
before execution" pattern): find the candidate (id order) -> refuse
loudly if the client is not privileged or an attempt budget is
exhausted -> record `submission.attempted` -> call
`client.submit_flag` -> persist the `submission` entity
(`submission-<seq>`) plus its `SUBMISSION SUBMITS FLAG_CANDIDATE`
edge (same-timestamp `graph.*` events) -> on acceptance return the
typed `SubmissionResult` (the router's `has_accepted_submission`
predicate then routes DONE); on rejection mark the candidate
`rejected: true`, increment its `attempts` (mirrored as a
`graph.entity_updated` event), and raise `SubmissionRejectedError` —
the flag is never re-submitted and the router re-routes away from
VERIFY_AND_SUBMIT.

Submission is always serialized (AGENTS.md rule #7): one candidate per
coordinator call, one wire call per attempt, and the per-candidate and
run-total caps (`max_submissions`, default 3) are enforced before any
wire call.

`submission` payload contract:

| Field | Meaning |
|---|---|
| `challenge_id` | the challenge the flag was submitted to (platform echo) |
| `flag` | the submitted flag text |
| `accepted` | strict bool — the router's terminal signal (`true` -> DONE) |
| `message` | the platform's verdict message |
| `points` | points awarded (0 when rejected) |
| `candidate_id` | the submitted `flag_candidate` entity id |

Submission errors (all `RuntimeError` subclasses):

| Error | Raised when |
|---|---|
| `SubmissionError` | base error for the submission layer |
| `SubmissionPrivilegeError` | the injected client is not privileged (supervisor-only, AGENTS.md invariant 5) |
| `SubmissionLimitError` | the candidate's attempt count or the run's total submission count reached `max_submissions` (budget-style, before the wire) |
| `SubmissionStateError` | a candidate payload field the coordinator reads is wrong-typed |
| `SubmissionRejectedError` | the platform rejected the flag; carries `candidate_id`, `flag`, `message` |
| `MissingRequiredStateError` (router) | no verified, non-rejected candidate exists, or a verified candidate lacks its provenance edge |

### Events and constants

| Constant | Value | Meaning |
|---|---|---|
| `FLAGS_PRODUCER` | `"flags"` | producer of extractor run events |
| `FLAGS_CANDIDATE_FOUND` | `"flags.candidate_found"` | run event per new candidate |
| `SUBMISSIONS_PRODUCER` | `"submissions"` | producer of coordinator run events |
| `SUBMISSION_ATTEMPTED` | `"submission.attempted"` | recorded before the wire call |
| `SUBMISSION_ACCEPTED` | `"submission.accepted"` | platform accepted the flag |
| `SUBMISSION_REJECTED` | `"submission.rejected"` | platform rejected the flag |
| `EDGE_FLAG_CANDIDATE_OBSERVED_IN_EVIDENCE` | `"FLAG_CANDIDATE OBSERVED_IN EVIDENCE"` | candidate -> evidence provenance edge |
| `EDGE_SUBMISSION_SUBMITS_FLAG_CANDIDATE` | `"SUBMISSION SUBMITS FLAG_CANDIDATE"` | submission -> candidate edge |
| `DEFAULT_FLAG_PATTERN` | `r"flag\{[^{}\s]+\}"` | default `flag{...}` format (no braces or whitespace inside) |
| `DEFAULT_MAX_SUBMISSIONS` | `3` | default per-candidate and run-total attempt cap |

Run-only events never carry the raw flag text: `flags.candidate_found`
and the `submission.*` verdict events persist only `flag_sha256` +
`flag_length` digests (FLAGLEAK-001) — the plaintext flag lives only in
the replay-required `graph.*` entity payloads.

Configuration knobs (validated at load time — an invalid regex or
non-integer cap is a `ConfigError`):

| Env var | Default | Meaning |
|---|---|---|
| `OZZGRAPH_FLAG_PATTERN` | `r"flag\{[^{}\s]+\}"` | Regular expression the extractor matches observation/artifact text against (challenge-specific formats override the safe `flag{...}` default) |
| `OZZGRAPH_MAX_SUBMISSIONS` | `3` | Attempt cap, per candidate and in total (budget-style: refused before any wire call) |

### Supervisor integration

`Supervisor.submit_verified_candidate(graph, challenge_id=None, *,
client=None)` is the supervisor-owned entry point (AGENTS.md invariant
5): it resolves the challenge id (explicit argument or
`OZZGRAPH_CHALLENGE_ID`), constructs a supervisor-owned privileged
`HalClient` when none is injected, drives the coordinator, and closes
the client it owns. Models and workers never hold a client that could
submit.

## Hint Policy

Hint handling (PR23) is the Phase 8 slice "free hint, paid-hint
policy" (docs/IMPLEMENTATION_PLAN.md step 23). Hint zero is free and
automatic — bootstrap requests it at startup and records
`bootstrap.hint_requested`; paid hints (`index > 0`) are
supervisor-only and gated by a deterministic policy. Two small pieces
implement the paid side: `ozzgraph.hints.HintPolicy` is the pure,
fail-closed gate over the authoritative graph state, and
`ozzgraph.hints.HintCoordinator` is the ONLY kernel caller of
`request_hint` for `index > 0`, mirroring how
`SubmissionCoordinator` owns `submit_flag`. Neither module touches MCP
directly: the coordinator drives the privileged `HalClient.request_hint`
and every model-visible path can only produce action text, never a
purchase.

### The gate (`ozzgraph.hints.HintPolicy`)

The gate is a pure decision function: `evaluate(graph, request)` reads
the graph and returns a typed `PaidHintDecision` — it never mutates
state, never emits events, and never touches the wire. Hint zero
(`index == 0`) is approved unconditionally (rules
`{"free_hint": true}`) — hint zero may be automatic
(docs/TECHNICAL_REQUIREMENTS.md). Every paid hint is evaluated against
all five rules below; the decision carries one boolean per rule (the
`rules` map) plus every denial reason. The gate is fail-closed: any
unrepresentable or unknown state (no plan entity, a corrupt
`step_count` payload, an inconsistent step set, no
evaluation/purchase anchor) denies with a documented reason — the gate
never coerces state.

```python
class PaidHintRequest(BaseModel):
    challenge_id: str
    index: int  # 0 = free hint (never gated); > 0 = paid hint


class PaidHintDecision(BaseModel):
    approved: bool
    index: int
    rules: dict[str, bool]  # one boolean per gate rule (RULE_* constants)
    reasons: tuple[str, ...]  # denial reasons; empty when approved
    expected_value_gain: float | None  # (1 - progress) * min(1, attempts/6)


class HintPolicy:
    def __init__(
        self,
        *,
        run_id: str = "hints",
        event_log: EventLog | None = None,
        max_hints: int = DEFAULT_MAX_HINTS,  # 1 — one paid hint per detonation
        min_ev_gain: float = 0.5,
        ev_stall_floor: int = 6,
        required_recommendations: int = 2,
        information_gain_types: tuple[str, ...] = ("fact", "evidence", "observation"),
    ) -> None: ...

    async def evaluate(self, graph: StateGraph, request: PaidHintRequest) -> PaidHintDecision: ...

    async def record_evaluator_recommendation(
        self, graph: StateGraph, evaluation_id: str, reason: str = ...
    ) -> str: ...  # idempotent per evaluation; returns the entity id
```

The gate rules (all deterministic predicates over graph entities and
their authoritative `created_at` timestamps — replay reconstructs them
exactly):

| Rule | `rules` key | Deterministic predicate (passes iff ...) |
|---|---|---|
| Paid-hint budget | `budget_available` | the persisted `hint_purchase` entity count is below `max_hints` (default 1). The count of `hint_purchase` entities IS the paid-hint ledger, so the count can never exceed the configured maximum (AGENTS.md data invariant). |
| No recent information gain | `no_recent_information_gain` | no `fact`, `evidence`, or `observation` entity has `created_at` strictly after the anchor, where the anchor is the LATER of the latest `hint_purchase` entity's `created_at` and the latest `evaluation` entity's `created_at`. With neither a purchase nor an evaluation the rule is False (fail-closed: no anchor, no evidence the state was ever assessed). |
| Exhausted low-cost actions | `low_cost_actions_exhausted` | every `plan_step` entity of the latest `plan` entity (the greatest `(created_at, id)`, the evaluator's PR21 rule) has at least one attempted `action` entity bound via its `plan_step_id` payload. A missing plan, a non-integer `step_count`, or a step set whose size mismatches `step_count` is unrepresentable and denies. |
| Two evaluator recommendations | `two_evaluator_recommendations` | at least 2 `hint_recommendation` entities exist. Each is idempotent per evaluation (`hint-rec-<sha256(evaluation_id)>`), so 2 records mean 2 DISTINCT evaluations recommended a hint. |
| Sufficient expected-value improvement | `sufficient_expected_value` | `(1 - progress) * min(1, attempts / EV_STALL_FLOOR) >= min_ev_gain` (0.5), where `progress` is the fraction of the latest plan's steps marked `completed` in the latest evaluation's `step_outcomes` (0.0 when no evaluation exists or its `step_outcomes` is missing/not a list) and `attempts` is the number of `action` entities bound to the latest plan via their `plan_id` payload. Intuition: the value of a hint scales with the remaining unsolved fraction times how much cheap effort already failed to move the run — "at most half progressed AND stalled at least halfway to the exhaustion floor". |

### Recommendation records

The evaluator (PR21) persists `evaluation` entities but emits no
hint-recommendation signal, so the policy layer owns the minimal
deterministic recommendation record. `record_evaluator_recommendation`
persists one `hint_recommendation` entity
(`hint-rec-<sha256(evaluation_id)>`, idempotent — the same evaluation
can never recommend twice) that must reference a real `evaluation`
entity (fail loudly otherwise), mirrored as a `graph.entity_created`
event plus a `hint.recommendation_recorded` run event.

### Coordinator (`ozzgraph.hints.HintCoordinator`)

`HintCoordinator` is the only caller of `request_hint` for paid hints
in the kernel. It enforces the supervisor-only boundary (a
non-privileged client raises `HintPrivilegeError` before the gate
runs), evaluates the gate, and only then requests, persists, and
records the purchase.

```python
class HintCoordinator:
    def __init__(
        self,
        *,
        client: HintClient,  # must be privileged; HalClient satisfies the protocol
        run_id: str,
        challenge_id: str,
        event_log: EventLog | None = None,
        max_hints: int = DEFAULT_MAX_HINTS,  # 1
        policy: HintPolicy | None = None,
    ) -> None: ...

    async def check_then_request(self, graph: StateGraph, index: int) -> HintResult: ...
```

`check_then_request` flow: free hint zero passes straight to the client
(no gate, no privilege requirement, no purchase entity — bootstrap
owns the automatic request); any paid hint is serialized under the
coordinator's `asyncio.Lock` (AGENTS.md rule #7 — paid hints are
always serialized): refuse loudly if the client is not privileged ->
evaluate the gate -> on denial record `hint.policy_denied` (rule
breakdown + reasons) and raise `HintPolicyDeniedError` -> on approval
record `hint.policy_approved`, then `hint.purchase_attempted` BEFORE
the wire call (the executor's "record the attempt before execution"
boundary) -> call `client.request_hint` -> validate the platform's
`paid` verdict (a `paid: false` answer to a paid request raises
`HintStateError` and persists nothing) -> persist the
`hint_purchase` entity -> record `hint.purchase_succeeded` (a
`HalServiceError` records `hint.purchase_failed` and re-raises; no
purchase is persisted). The lock plus the in-lock budget re-read
guarantee concurrent gate evaluations never double-purchase — the
paid-hint count never exceeds `max_hints`.

`hint_purchase` payload contract (entities are entity-only — the count
of entities IS the paid-hint ledger):

| Field | Type | Meaning |
|---|---|---|
| `challenge_id` | `str` | the challenge the hint was bought for |
| `index` | `int` | the hint index (`> 0`) |
| `paid` | `bool` | strict `True` — the platform's verdict, validated |
| `hint` | `str` | the platform's hint text |

Hint-policy errors (all `RuntimeError` subclasses):

| Error | Raised when |
|---|---|
| `HintError` | base error for the hint-policy layer |
| `HintPrivilegeError` | the injected client is not privileged (supervisor-only, AGENTS.md invariant 5) |
| `HintPolicyDeniedError` | the gate denied the paid hint; carries the full `PaidHintDecision` |
| `HintStateError` | the platform answered a paid request with `paid: false`, or a recommendation references an unknown evaluation |

### Events and constants

| Constant | Value | Meaning |
|---|---|---|
| `HINTS_PRODUCER` | `"hints"` | producer of hint-policy run events |
| `HINT_POLICY_DENIED` | `"hint.policy_denied"` | gate denial; payload carries `index`, `approved: false`, the `rules` breakdown, `reasons`, `expected_value_gain` |
| `HINT_POLICY_APPROVED` | `"hint.policy_approved"` | gate passed; payload carries the same rule breakdown |
| `HINT_PURCHASE_ATTEMPTED` | `"hint.purchase_attempted"` | recorded before the wire call |
| `HINT_PURCHASE_SUCCEEDED` | `"hint.purchase_succeeded"` | platform returned the paid hint; payload carries `purchase_id` |
| `HINT_PURCHASE_FAILED` | `"hint.purchase_failed"` | wire failure or a `paid: false` verdict; payload carries the error |
| `HINT_RECOMMENDATION_RECORDED` | `"hint.recommendation_recorded"` | a recommendation was persisted |
| `ENTITY_HINT_PURCHASE` | `"hint_purchase"` | persisted paid-hint purchase (`hint-purchase-<seq>`, deterministic) |
| `ENTITY_HINT_RECOMMENDATION` | `"hint_recommendation"` | evaluator recommendation (`hint-rec-<sha256(evaluation_id)>`) |
| `RULE_BUDGET` etc. | `"budget_available"` ... | gate rule names on `PaidHintDecision.rules` |
| `DEFAULT_MAX_HINTS` | `1` | default paid-hint budget (one per detonation) |

Configuration knobs:

| Env var | Default | Meaning |
|---|---|---|
| `OZZGRAPH_MAX_HINTS` | `1` | Paid-hint budget — the persisted `hint_purchase` count must stay below it (validated at load time; `ge=1`) |

### Supervisor integration

`Supervisor.request_paid_hint(graph, index, challenge_id=None, *,
client=None)` is the supervisor-owned entry point (AGENTS.md invariant
5), mirroring `submit_verified_candidate`: it requires `index >= 1`
(hint zero is bootstrap's job), resolves the challenge id, constructs a
supervisor-owned privileged `HalClient` when none is injected, drives
the coordinator, and closes the client it owns. Models and workers
never hold a client that could buy a paid hint.

## Scheduler

The scheduler (PR24) is the first slice of Phase 9 "Workers"
(docs/IMPLEMENTATION_PLAN.md, step 24; docs/ARCHITECTURE.md,
"Scheduler"): a task DAG with explicit dependencies and conflict keys,
a bounded-parallel scheduler, and the structured-findings / worker-run
contracts — the Phase 9 exit "independent tasks run concurrently,
conflicting tasks serialize". The reducer (step 26) and specialist
workers (step 25) are separate later PRs and are NOT implemented here:
findings stay embedded in their `worker_run` records and are never
merged into the graph as authoritative state by the scheduler.

```python
class Task(BaseModel):
    id: str  # caller-supplied stable id; used as the task entity id
    depends_on: tuple[str, ...]  # explicit dependency task ids
    conflict_keys: tuple[str, ...]  # overlapping keys => mutually exclusive
    plan_step_id: str | None  # TASK IMPLEMENTS PLANSTEP edge, when set
    hypothesis_id: str | None  # WORKER_RUN EXPLORED HYPOTHESIS edge, when set


class Finding(BaseModel):
    task_id: str  # provenance: the task that produced the finding
    source: str  # provenance: where the finding came from
    evidence_ids: tuple[str, ...]  # >= 1 evidence/artifact reference (mandatory)
    summary: str  # bounded prose — never authoritative state by itself
    confidence: float  # 0.0..1.0, default 0.0


class TaskDAG:
    def __init__(self, tasks: Sequence[Task]) -> None: ...  # validates eagerly
    def ready_order(self, completed: Collection[str]) -> tuple[str, ...]: ...
    def topological_order(self) -> tuple[Task, ...]: ...


class Scheduler:
    def __init__(
        self,
        dag: TaskDAG,
        *,
        runner: TaskRunner,  # async def run_task(task) -> TaskOutcome
        max_workers: int,  # the existing OZZGRAPH_MAX_WORKERS knob (default 4)
        run_id: str,
        event_log: EventLog | None = None,
    ) -> None: ...
    async def run(self, graph: StateGraph) -> SchedulerResult: ...
```

`TaskDAG` construction validates the whole DAG and fails loudly
(AGENTS.md rule #9): duplicate task ids, dependencies on unknown tasks,
and cycles (including self-dependencies) are rejected before anything
runs. `ready_order` returns the dependency-complete tasks sorted by
stable id, and the scheduler dispatches in exactly that order, so the
same DAG always yields the same start sequence — schedules are
deterministic and reproducible.

`Scheduler.run` persists the DAG as `task` entities (idempotently,
plus `TASK IMPLEMENTS PLANSTEP` edges), then runs a dispatch loop: at
each step it starts every dependency-complete, non-conflicting task in
deterministic id order up to `max_workers` concurrent runs. Tasks with
overlapping conflict keys are mutually exclusive and serialize;
independent tasks run concurrently. Each task produces a `WorkerRun`
with a stable id (`worker-run-<sha256(run_id:task_id)>`), recorded
before the runner executes (the executor's "record the attempt before
execution" boundary), updated with its status, findings, and error on
completion, plus the `WORKER_RUN EXPLORED HYPOTHESIS` edge when the
task explores a hypothesis. A runner crash becomes a structured failed
worker run — never silent, never retried. Every mutation is mirrored to
the event log as a same-timestamp `graph.*` event, so replay
reconstructs the identical graph hash.

Supervisor-only serialization (AGENTS.md rule #7 — flag submission and
paid hints are ALWAYS serialized): a task carrying the reserved
`SERIALIZED_CONFLICT_KEY` (`"serialized"`) conflicts with every other
task, including other serialized tasks. `serialized_task(...)` is the
dedicated hook the supervisor uses; the gate is deterministic and
fail-closed — a serialized task starts only when nothing else is
running.

`TaskOutcome` is the runner's typed contract: a succeeded outcome with
its findings or a failed outcome with a structured error. Findings must
be attributed to the task that produced them (a misattributed finding
is rejected loudly — AGENTS.md data invariant "a worker cannot mutate
state outside its declared task scope"), and a finding MUST reference
at least one evidence/artifact id — a finding without evidence is
free-form model prose (AGENTS.md rule #3).

Scheduler errors (all `RuntimeError` subclasses):

| Error | Raised when |
|---|---|
| `SchedulerError` | base error for the scheduler layer (including the defensive no-progress deadlock check in `run`) |
| `TaskDAGError` | base error for task-DAG construction failures |
| `DuplicateTaskError` | two tasks in the DAG share one id |
| `MissingDependencyError` | a task depends on a task that is not in the DAG |
| `TaskCycleError` | the dependency graph is cyclic (including self-dependencies) |
| `TaskNotFoundError` | a task id lookup misses the DAG |

### Events and constants

| Constant | Value | Meaning |
|---|---|---|
| `SCHEDULER_PRODUCER` | `"scheduler"` | producer of scheduler run events |
| `SCHEDULER_RUN_STARTED` | `"scheduler.run_started"` | a schedule started; payload carries `task_count` |
| `SCHEDULER_TASK_STARTED` | `"scheduler.task_started"` | one task dispatched; payload carries `task_id`, `worker_run_id`; event carries `task_id`/`worker_id` |
| `SCHEDULER_TASK_COMPLETED` | `"scheduler.task_completed"` | a task finished with a succeeded outcome |
| `SCHEDULER_TASK_FAILED` | `"scheduler.task_failed"` | a task finished with a failed outcome |
| `SCHEDULER_RUN_COMPLETED` | `"scheduler.run_completed"` | a schedule finished; payload carries `worker_runs`, `succeeded`, `failed` |
| `ENTITY_TASK` | `"task"` | persisted DAG node; entity id is the caller-supplied task id |
| `ENTITY_WORKER_RUN` | `"worker_run"` | one scheduled task execution (`worker-run-<sha256>`) |
| `EDGE_TASK_IMPLEMENTS_PLANSTEP` | `"TASK IMPLEMENTS PLANSTEP"` | task → plan step |
| `EDGE_WORKER_RUN_EXPLORED_HYPOTHESIS` | `"WORKER_RUN EXPLORED HYPOTHESIS"` | worker run → hypothesis |
| `SERIALIZED_CONFLICT_KEY` | `"serialized"` | reserved conflict key: always serialized (rule #7) |

Configuration knobs: none new — the scheduler reuses the existing
`max_workers` config field (`OZZGRAPH_MAX_WORKERS`, default 4). Nothing
is wired into the supervisor's idle loop or main flow; the scheduler is
delivered as the component plus its contracts (PR step 24).

## Workers

The workers module (PR25) implements the second slice of Phase 9
"Workers" (docs/IMPLEMENTATION_PLAN.md, step 25; docs/ARCHITECTURE.md,
"Scheduler" + "Parallelism"): the declarative worker-scope model and
the concrete scope-limited specialist workers the scheduler drives. It
enforces the AGENTS.md data invariant "a worker cannot mutate state
outside its declared task scope" deterministically and fail-closed
(see docs/adr/0005). The reducer (step 26) and supervisor wiring are
separate later PRs.

```python
class WorkerScope(BaseModel):
    name: str  # stable scope name, for errors and logs
    command_families: tuple[str, ...]  # subset of policy families; "shell" NOT implied
    phases: tuple[Phase, ...]  # canonical Phase order, deduplicated
    mutating: bool  # read-only vs mutating work
    target_allowlist: tuple[str, ...]  # optional narrowing (hostnames/IPs/CIDRs)


class WorkerTask(BaseModel):
    task: Task  # the scheduler's DAG node (looked up by task.id)
    command: str  # exactly one bounded action (AGENTS.md rule #4)
    phase: Phase  # the phase this action serves (within required_scope.phases)
    required_scope: WorkerScope  # the scope this task needs
    timeout_seconds: int  # bounded action budget
    output_limit: int  # per-stream output cap
    working_directory: str


class SpecialistWorker(ABC):  # implements the TaskRunner protocol
    scope: ClassVar[WorkerScope]  # declared by each concrete worker
    worker_id: ClassVar[str]
    default_confidence: ClassVar[float]

    def assign(self, work: WorkerTask) -> None: ...  # scope gate, before scheduling
    async def run_task(self, task: Task) -> TaskOutcome: ...  # enforced run
```

A concrete worker declares its `scope`, `worker_id`, and
`default_confidence` as class attributes; everything else is inherited.
Built-in specialists:

| Worker | Scope | Purpose |
|---|---|---|
| `ReconWorker` | read-only, `recon`+`shell`, BOOTSTRAP/RECON/ENUMERATION/PIVOT | service enumeration, host discovery, read-only probing (safe parallel work) |
| `ArtifactAnalysisWorker` | read-only, `shell`, ENUMERATION/POST_EXPLOITATION/FLAG_HUNT | bounded artifact analysis (`file`, `strings`, `binwalk`, ...) |
| `SubmissionWorker` | mutating, `shell`, VERIFY_AND_SUBMIT | supervisor-serialized submission wrapper — refuses any task without the reserved `serialized` conflict key |

Execution pipeline (deterministic, fail-closed): `run_task` looks the
task up in the worker's assignments and re-checks scope coverage, then
rejects the action itself — a command whose classified family is
outside the declared families, or a mutating-family command on a
read-only worker, raises a typed `WorkerScopeError` BEFORE anything
executes. Approved commands pass the scope-narrowed policy (when target
narrowing is declared) and the operator `ScopePolicy` (the worker's
families as `worker_scope`), have their fingerprint recorded (loop
prevention), and run through the bounded shell runner. On success the
bounded output is stored as content-addressed artifacts and returned as
one structured `Finding` (reused from the scheduler) with provenance
(task id, worker source) and mandatory evidence references — never
free-form prose as state. A nonzero exit or timeout returns a structured
failed `TaskOutcome`. When driven through the scheduler, a worker scope
violation becomes a structured failed `worker_run` (never silent, never
retried).

Supervisor-only serialization composes (AGENTS.md rule #7):
`SubmissionWorker` can only ever run tasks created with
`serialized_task()` — its gate requires the reserved
`SERIALIZED_CONFLICT_KEY`, and the scheduler already serializes such
tasks against every other task. Only the supervisor may wire this
worker (rule #5); nothing is wired in this PR.

Worker errors (all `RuntimeError` subclasses):

| Error | Raised when |
|---|---|
| `WorkerError` | base error for the worker layer (missing scope/worker_id/confidence declaration) |
| `WorkerScopeError` | base error for every scope rejection (fail closed) |
| `EmptyWorkerScopeError` | a scope declares no command families or no phases |
| `UnknownCommandFamilyError` | a scope declares a family the policy gate does not know |
| `ReadOnlyScopeError` | a read-only scope declares a mutating family |
| `TaskOutOfScopeError` | a task is not covered by the worker's scope, or is unassigned |
| `FamilyOutOfScopeError` | a command classifies into a family outside the declared families |
| `ReadOnlyViolationError` | a read-only worker attempts a mutating-family command |
| `SerializationRequiredError` | a serialized worker refuses a task without the `serialized` conflict key |
| `WorkerTaskError` | base error for task-assignment failures |
| `DuplicateAssignmentError` | a worker is assigned the same task id twice |

Determinism: the mutating partition is a module-level constant
(`MUTATING_COMMAND_FAMILIES = {"exploit"}`), artifact ids are
content-addressed sha256 digests, finding summaries derive from the
command, exit code, and evidence ids, and confidence is a per-worker
declared constant — no randomness, no wall-clock decisions, no dynamic
imports. Workers persist nothing; the scheduler owns the `task` and
`worker_run` entities and the event log (no new entities, edges, or
events in this PR). Configuration knobs: none new.

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

The dashboard is separate from the competition image: it lives in
`dashboard/` (Yarn + strict TypeScript, zero runtime dependencies) and is
never part of the Python package or image. It reads the kernel's real run
state — `actions.jsonl`, `graph.db`, and the artifact store — strictly
read-only, over a small JSON HTTP API.

### Setup and run

```bash
# One-time toolchain setup (Yarn 4 via corepack; node >= 22.5 for node:sqlite)
corepack prepare yarn@stable --activate

cd dashboard
yarn install          # dev deps only: typescript, eslint, vitest
yarn lint && yarn typecheck && yarn test && yarn build
yarn start --runs-dir ../state --host 127.0.0.1 --port 8787
# or: OZZGRAPH_DASHBOARD_RUNS_DIR=../state yarn start
```

Configuration precedence is CLI flag > environment > default:

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--runs-dir <path>` | `OZZGRAPH_DASHBOARD_RUNS_DIR` | `state` | Runs root directory (resolved against the working directory) |
| `--host <host>` | `OZZGRAPH_DASHBOARD_HOST` | `127.0.0.1` | Bind host |
| `--port <port>` | `OZZGRAPH_DASHBOARD_PORT` | `8787` | Bind port (1-65535) |

### Run discovery model

The runs root holds one directory per run; a directory is a run when it
directly contains `actions.jsonl` and/or `graph.db` (the kernel's per-run
state files), and its run id is the directory name. The root itself is
also tolerated as a single run when it directly contains either file (the
common single-run layout where the kernel's `state` dir is the runs
root); its run id is the root directory's basename. Hidden and symlinked
directories are never treated as runs. `GET /api/runs` lists runs sorted
by run id; run ids and artifact ids are validated as single path
segments — `..`, absolute paths, separators, and control characters are
rejected with `400` before any filesystem access.

### Endpoints

```http
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/graph
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/artifacts/{artifact_id}
GET  /api/runs/{run_id}/metrics
POST /api/runs/{run_id}/replay
GET  /healthz
```

`GET /api/runs` — `200` with every discovered run:

```json
{"runs": [{"run_id": "state", "event_count": 12, "artifact_count": 2,
           "last_modified": "2026-08-07T17:08:09.320Z",
           "has_graph": true, "has_events": true}]}
```

`event_count` counts parseable `actions.jsonl` events; `artifact_count`
uses the kernel's `artifacts.json` index when present (array or dict),
else the artifact directory listing (excluding the index and `.tmp`
scratch files); `last_modified` is the newest of the run's state files.

`GET /api/runs/{run_id}` — `200` with the same summary object for one
run, or `404 run_not_found`.

`GET /api/runs/{run_id}/graph` — `200` with the full graph read from
`graph.db` (opened strictly read-only; parameterized queries only):

```json
{"run_id": "state", "schema_version": 2,
 "entities": [{"id": "run-abc", "type": "run", "data": {"status": "active"},
               "created_at": "2026-08-06T15:15:00.123456+00:00",
               "updated_at": "2026-08-06T15:15:00.123456+00:00"}],
 "edges": [{"id": "edge-1", "type": "ACTION PRODUCED OBSERVATION",
            "src_id": "action-1", "dst_id": "run-abc", "data": {},
            "created_at": "2026-08-06T15:15:00.123456+00:00"}],
 "entity_count": 1, "edge_count": 1,
 "graph_hash": "4d3ec0bc241bd4756eb46ef330a79aad9a627069210313331a2e13fe2271d315"}
```

`graph_hash` is computed with the kernel's exact canonicalization (see
`src/ozzgraph/state_graph.py` `graph_hash`), so it matches the hash a
kernel replay of the same run produces. `404 graph_not_found` when the
run has no `graph.db`; `500 graph_read_failed` when the database exists
but cannot be read (details logged server-side only).

`GET /api/runs/{run_id}/events` — `200` with every parseable
`actions.jsonl` line in file order; malformed lines are skipped
gracefully and counted in `skipped`:

```json
{"run_id": "state", "event_count": 12, "skipped": 0, "events": [...]}
```

`GET /api/runs/{run_id}/artifacts/{artifact_id}` — `200` with the raw
artifact bytes from the run's `artifacts/` directory, `Content-Type`
from the file extension (default `application/octet-stream`),
`X-Content-Type-Options: nosniff`, `Cache-Control: no-store`; `404
artifact_not_found` when missing.

`GET /api/runs/{run_id}/metrics` — `200` with metrics derived
deterministically from the event log: `total_events`, `skipped`,
`event_type_counts`, `producer_counts`, `action_count` (created `action`
entities), `model_call_count` (created `model_call` entities),
`first_event_at` / `last_event_at`, and `duration_ms` (span between the
first and last valid event timestamps).

`POST /api/runs/{run_id}/replay` — `200` after applying every
`actions.jsonl` line in order to an in-memory entity/edge map using the
TypeScript port of the kernel's replay (`src/ozzgraph/replay.py`
semantics): only the five `graph.*` mutation event types are applied,
non-graph events are ignored, and any malformed line or invalid
graph-event payload aborts the replay with `400 replay_malformed_event`
(never silently skipped — the dashboard mirrors the kernel's fail-loudly
behavior). The response carries the final counts and the canonical graph
hash, which is byte-identical to the kernel's for kernel-produced logs:

```json
{"event_count": 12, "entity_count": 6, "edge_count": 3,
 "graph_hash": "e4905ca79b445c001e606b8e943bfec8e91cc55db0219ce572f653b91a034e53"}
```

A request body, when present, must be valid JSON (`400
invalid_json_body` otherwise). Entity/edge deletion cascade-deletes
touching edges, exactly like the kernel's `ON DELETE CASCADE`.

`GET /healthz` — `200 {"ok": true}`.

### Error shape and security

Every error response is structured JSON with no stack traces (details go
to the server's stderr):

```json
{"error": {"code": "run_not_found", "message": "unknown run 'nope'"}}
```

Status codes: `400` (`invalid_run_id`, `invalid_artifact_id`,
`invalid_json_body`, `replay_malformed_event`), `404` (`run_not_found`,
`graph_not_found`, `artifact_not_found`, `route_not_found`), `405`
(`method_not_allowed`), `413` (`payload_too_large`), `500`
(`internal_error`, `graph_read_failed`). The dashboard never writes to
the kernel's database or artifact store — `graph.db` is opened with
SQLite's read-only flag and `PRAGMA query_only`; the replay runs purely
in memory.

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
