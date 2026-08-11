# OzzGraph v2 — General Autonomous Security-Research Harness

> Source: design review / architecture proposal. Captured verbatim from the
> CHANGES.md brief (2026-08-08). This is the planning input for the v2
> milestone, not yet an accepted implementation plan.

## The core reframing

**OzzGraph should not be a HalCTF agent. OzzGraph should be a general
autonomous security-research / vulnerability-discovery harness that happens to
support HalCTF as one runtime adapter.**

HalCTF, CTFs, flag searching, flag submission, hints, scoreboard are no longer
fundamental concepts — they become optional behaviors supplied by a HalCTF/CTF
adapter.

The core abstraction changes from:

```
I am running one CTF challenge → find target → find flag → submit flag
```

to:

```
I have an authorized assessment scope → discover assets → understand exposed
surface → form security hypotheses → test hypotheses → validate
vulnerabilities → collect reproducible evidence → determine impact → produce
findings
```

## Recommended architecture

```
                 RUNTIME ENVIRONMENT
          local │ docker │ lab │ HalCTF │ CI
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                ENVIRONMENT ADAPTER                   │
│   LocalTarget │ HalCTF │ Docker │ Project │ Network  │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────┐
│                    SUPERVISOR                        │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────┐
│                AUTONOMOUS RUNNER                     │
│   Discover → Enumerate → Analyze → Validate → Exploit│
│   Evidence → Findings ← Pivot/Expand Scope* → Report │
│   Planner ← Evaluator ← Executor                    │
└──────────────┬────────────────────────────┬──────────┘
               ▼                            ▼
      ┌─────────────────┐       ┌────────────────────┐
      │ STATE GRAPH     │       │ CONTEXT ENGINE     │
      └─────────────────┘       └────────────────────┘
                                           ▼
                                ┌────────────────────┐
                                │ MODEL ADAPTER      │
                                └────────────────────┘
                                           ▼
                                ┌────────────────────┐
                                │ TOOL PLANE         │
                                └────────────────────┘
* only inside the explicitly authorized scope
```

Key difference vs v1: **the arrows become real code paths** rather than
independently implemented components.

## v2 milestone plan (vertical-first, not horizontal)

1. `v2/generic-runtime` — EnvironmentAdapter, Scope, Target, Objective,
   LocalEnvironment, HalCTFEnvironment; remove FLAG_HUNT/VERIFY_AND_SUBMIT
   from the generic kernel.
2. `v2/autonomous-vertical-slice` — make `ozzgraph run http://127.0.0.1:3000`
   work end-to-end against a deliberately vulnerable app with NO test code
   manually driving components. **Do not build another horizontal subsystem
   until this works.**
3. `v2/tool-runtime` — Kali rolling + `kali-linux-everything`; ToolCatalog,
   ToolInventory, CapabilityRegistry, ToolProvider; skills declare
   capabilities, not binaries.
   > HAL-009 (2026-08-09): Tottori live-run exploitation lessons ported as
   > eight new skill cards in the SKILLS registry (kernel-external data,
   > AGENTS.md rule #10): `exploit_sqli_enumeration` (multi-DB engine
   > fingerprinting, UNION/boolean/error techniques, information_schema vs
   > sqlite_master), `exploit_jwt` (alg confusion, alg=none, the
   > PEM-as-HMAC-secret key-confusion lesson, kid injection), `exploit_ssrf`
   > (multi-service probing from one URL parameter, decimal/hex/octal/IPv6 IP
   > obfuscation, DNS-rebinding reasoning, file:// and gopher:// schemes),
   > `exploit_xxe` (file read via file://, SSRF via http:// entities, blind
   > exfiltration only with an authorized listener), `exploit_deserialization`
   > (pickle/yaml/Jackson/PHP sink identification, safe probes, evidence-
   > driven gadget chains — never executing untrusted payloads on the harness
   > host), `exploit_protocol_reversing` (capture, framing/length-prefix
   > analysis, one-field-at-a-time fuzzing, checksum/CRC handling),
   > `forensics_file_analysis` (carving, strings/entropy, steganography
   > checks, archive/disk-image enumeration, timeline reconstruction), and
   > `exploit_cloud_iam` (metadata service at 169.254.169.254, role chaining,
   > credential validation — authorized challenge cloud scopes only). New
   > `ozzgraph.techniques.TechniqueClassifier` maps a challenge category
   > string (e.g. "Web / SSRF", "SQL Injection", "Forensics", "Cloud IAM") to
   > the deterministic subset of skill ids via case-insensitive substring
   > rules (`sql` -> sqli + parameter injection, `web`/`jwt` -> jwt +
   > auth_bypass, `ssrf` -> ssrf + http application, `forensic` -> forensics,
   > `cloud`/`iam` -> cloud IAM, ...); unknown/absent categories degrade
   > deterministically to the recon/enum core (never empty, never a crash).
   > `SkillRegistry.list_for_category(category)` and
   > `PhaseRouter.skills_for(phase, category=None)` expose category-routed
   > SUMMARIES only — lazy loading intact, full cards still arrive solely via
   > `SkillRegistry.load` (AGENTS.md rule #6).
4. `v2/semantic-observations` — typed parsers/projectors for the highest-value
   tools (nmap, ffuf, nuclei, netexec, semgrep, CodeQL, trivy, gitleaks, ...);
   mandatory raw-artifact persistence before summarization.
5. `v2/model-harness-matrix` — benchmark models empirically (format
   compliance, tool selection, repetition, evidence grounding, solve rates);
   choose protocols from data.
6. `v2/security-brain` — OpportunityGenerator, StrategicPlanner, TaskBuilder,
   HypothesisManager, ProgressEvaluator.
   > V06 (2026-08-08): implemented — `src/ozzgraph/security_brain.py` replaces
   > the round-robin plan call in the runner's investigate loop with the
   > opportunity-driven flow: exactly one obvious action (a single
   > uncharacterized service) executes deterministically with ZERO LLM calls;
   > more than one viable path invokes the StrategicPlanner (the model, with
   > the ranked opportunities in context); zero or one non-obvious paths keep
   > the standard model-propose path. The HypothesisManager owns the
   > hypothesis lifecycle (create -> evidence -> promote/abandon, `status`
   > payload field), and the ProgressEvaluator decides continue/pivot/finish
   > each loop iteration. The public `Planner` API is unchanged (executor and
   > evaluator still consume it).
7. `v2/specialists` — genuine narrow micro-agents; parallelize independent
   hypotheses, serialize global strategy, merge through reducer.
   > V07 (2026-08-08): implemented — genuine narrow micro-agents
   > (`src/ozzgraph/workers.py`: `SpecialistMicroAgent` + `MicroAgentTask`)
   > run a bounded, deterministic hypothesis → experiment → observation →
   > conclusion loop per task: at most `MAX_MICRO_ITERATIONS` bounded
   > experiments through the existing policy-gate/shell/artifact path, each
   > normalized with the tool parsers (`parser_for_command`), then a
   > structured `Verdict` with mandatory evidence references — ZERO model
   > calls, and the only context is the hypothesis objective plus the prior
   > observations, never the full graph. `src/ozzgraph/scheduler.py`
   > parallelizes independent hypotheses (`hypothesis_task`: the hypothesis
   > id IS the conflict key, so same-hypothesis tasks serialize while
   > independent hypotheses run concurrently under `max_workers`, driven by
   > `ready_order`); global strategy stays serialized through
   > `serialized_task` and the reserved `MUTATION_CONFLICT_KEY`. Conclusions
   > ride the scheduler `Finding` as `verdict` + `impact`
   > (CWE/assets/confidence), and `src/ozzgraph/reducer.py` merges them as
   > structured facts — verdict and impact live in the fact payload and
   > fingerprint. `src/ozzgraph/specialists.py` wires it into a
   > `SpecialistFleet` batch (narrow task build → bounded parallel schedule →
   > reducer → promote confirmed / abandon refuted → evidence-backed
   > findings + `findings.json`), and the runner (`src/ozzgraph/runner.py`)
   > dispatches a fleet batch instead of an LLM call when the brain's
   > `StrategicDecision` is a pure independent-hypothesis batch AND a fleet
   > is wired in (`specialists=`); the strategic LLM path and the
   > deterministic single-obvious-action path are unchanged.
   > HAL-010 (2026-08-09): the fleet is wired into PRODUCTION composition —
   > `Supervisor.run` composes a `SpecialistFleet` (artifacts, event log,
   > run id, scope policy, `max_workers`, `state_dir`) into the
   > `AutonomousRunner` when `OZZGRAPH_SPECIALISTS_ENABLED` is set
   > (`config.specialists_enabled`, any of `1`/`true`/`yes`/`on`, default
   > off): a pure independent-hypothesis `StrategicDecision` then dispatches
   > the bounded parallel micro-agent batch in real runs, not just in tests.
   > The fleet owns no async resources (no `aclose`), so plain construction
   > is sufficient; the default keeps the V06 model path byte-for-byte
   > unchanged (ADR-0009 consequence).
8. `v2/local-assessment` — URL/network/repository/Docker-Compose/hybrid modes,
   credentials, scope files, reporting, SARIF; default OzzGraph experience.
   > V08 (2026-08-08): implemented — `src/ozzgraph/reporting.py` renders the
   > deterministic report bundle into the run's `state_dir` at COMPLETED
   > termination: `report.md` (per-finding writeups: id, CWE, severity from
   > the impact CIA, assets, preconditions, evidence ids, reproduction,
   > confidence), `report.json` (the same finding payloads as the V02
   > `findings.json` plus graph metadata: run id, environment, model,
   > targets, scope, termination reason, entity counts), `report.sarif`
   > (SARIF 2.1.0, results mapped to CWE rules with locations from the
   > materialized evidence artifacts, driver `ozzgraph`), an `evidence/`
   > directory (copies of every finding-referenced artifact from the
   > authoritative store), and `graph.sqlite` + `events.jsonl` snapshots of
   > the authoritative `graph.db` / `actions.jsonl` — replay compatibility
   > preserved (the bundle is derived output; render failures record a
   > `runner.report_failed` event loudly). `src/ozzgraph/environments/local.py`
   > classifies `OZZGRAPH_TARGET` / scope entries into url / network / host /
   > repository / docker-compose / hybrid modes (a path containing `.git` ->
   > repository, a path containing a compose file -> docker-compose, URL ->
   > url, CIDR -> network, host/IP -> host; mixed types -> hybrid scope;
   > invalid repo/compose paths raise `ConfigError` loudly), with the mode on
   > each `Target`'s metadata and on the scope's constraints.
   > `src/ozzgraph/config.py` adds the optional scope file
   > (`OZZGRAPH_SCOPE_FILE`: JSON/YAML/TOML allowlist entries merged
   > deterministically into `target_allowlist`) and the optional credentials
   > file (`OZZGRAPH_CREDENTIALS_FILE`: `{name, kind, username?, secret_env?}`
   > records — the secret is read from the named env var at runtime and never
   > stored in the file or config; malformed files raise `ConfigError`).
   > Local remains the default experience: with no `HAL_*` configuration the
   > run uses `LocalEnvironment` (docs/adr/0010).
9. `v2/halctf-adapter` — HAL_* discovery, official tool set, smoke flag,
   scoring, hint costs, graceful completion; no kernel contamination.
   > V09 (2026-08-08, 6a7f8dc, docs/adr/0011): implemented — deterministic
   > env-based discovery (`ozzgraph.config`: HalCTF mode selected by any
   > `HAL_CTF_ID` / `HAL_CHALLENGE_ID` / `HAL_ENDPOINT` /
   > `HAL_MCP_ENDPOINT` / `MCP_ENDPOINT` / legacy `OZZGRAPH_CHALLENGE_ID`
   > variable; the MCP endpoint is the first non-blank of
   > `OZZGRAPH_MCP_BASE_URL` / `HAL_MCP_ENDPOINT` / `HAL_ENDPOINT` /
   > `MCP_ENDPOINT`). HAL-002 (2026-08-09): the endpoint became OPTIONAL —
   > an env-only detonation (HAL_TARGET_* services + HAL_CHALLENGE_*
   > metadata, no endpoint) starts without one: `load_config` and
   > `HalCTFEnvironment` no longer raise for a missing endpoint,
   > `OPENAI_BASE_URL` is NOT an endpoint candidate (it is the model
   > service at `/llm`, not the MCP server at `/mcp/`), and
   > `require_halctf_endpoint` remains a loud helper for callers that
   > genuinely need the endpoint. `HAL_USER_ID` never selects the
   > mode, so the local default — V08 `OZZGRAPH_TARGET` classification —
   > is unchanged). The official HalCTF MCP tool set is exposed by
   > `hal_client` (`OFFICIAL_HALCTF_TOOLS`: `list_ctfs` -> `ctf.list`,
   > `challenges` -> `challenge.list`, `status` -> `challenge.status`,
   > `submit_flag` -> `flag.submit`, `request_hint` -> `hint.request`,
   > `scoreboard` -> `scoreboard.get`), with `halctl ctfs` /
   > `halctl challenges` subcommands and halctl-parser document kinds.
   > Challenge status carries the smoke-flag signal and the deterministic
   > scoring breakdown (`ChallengeStatus.smoke_flag` / `scoring`),
   > `HintResult` carries the platform-reported per-hint `cost`, and the
   > paid-hint gate keeps enforcing the max-paid-hint-count invariant
   > (supervisor-only, AGENTS.md rule 5/7). **Hints/submissions fully out
   > of the kernel**: `HintPolicy`/`HintCoordinator`,
   > `SubmissionCoordinator`, `FlagCandidateExtractor`, and the new
   > `ScoreboardCoordinator` moved out of `ozzgraph.hints` /
   > `ozzgraph.submissions` / `ozzgraph.flags` (all deleted) into
   > `ozzgraph.environments.halctf`, reached only through the package
   > shim or the environment's service factories
   > (`flag_extractor` / `submission_coordinator` / `hint_coordinator` /
   > `scoreboard_coordinator`, wired to the discovered challenge id and
   > the config's budgets); the shared generic vocabulary
   > (`observation`/`evidence`) lives in `ozzgraph.entities`, and a
   > grep-enforced test proves no kernel module outside `environments/`
   > imports the moved modules. Graceful completion: the objective's
   > `success_hint` names the deterministic signal, an accepted
   > submission routes the graph DONE, and the run terminates COMPLETED
   > with the V08 report bundle. HAL-004 (2026-08-09): sidecar transport
   > adapter — `src/ozzgraph/environments/halctf/sidecar.py` adds
   > `SidecarSubmissionClient` (with the environment factory
   > `sidecar_submission_client()`), the plain-HTTP adapter at the real
   > competition sidecar's process boundary: `POST /submit` +
   > `POST /done` at `127.0.0.1:9000`, NOT JSON-RPC (verified from the
   > halctf-team-tottori deployment's live-run logs). It implements the
   > `SubmissionClient` protocol (privileged / submit_flag / aclose), so
   > the supervisor-only `SubmissionCoordinator` drives it unchanged, and
   > normalizes every observed response form into the internal
   > `SubmissionResult` schema deterministically (`ACCEPT_STATUSES` =
   > correct / accepted / solved / success / already_solved; then explicit
   > boolean verdict fields; then points > 0; wrong-typed verdicts fail
   > loudly as `HalServiceError`, never coerced). Base URL resolution is
   > env-first (explicit `OZZGRAPH_SIDECAR_BASE_URL` -> the resolved MCP
   > endpoint's ORIGIN — the sidecar shares the MCP host:port in the real
   > deployment — -> the localhost default; `OPENAI_BASE_URL` never
   > consulted); `OZZGRAPH_SIDECAR_TIMEOUT_S` / `OZZGRAPH_SIDECAR_MAX_RETRIES`
   > bound the client, and the shared `OZZGRAPH_HAL_PRIVILEGED` flag keeps
   > the supervisor-only boundary (`submit_flag` and `done` raise
   > `HalPrivilegeError` otherwise). Failures are typed
   > (`HalServiceError` with provider/status_code/retryable/message),
   > retried bounded on transient failures only (429/5xx/transport), and
   > recorded as `sidecar.failure` events; `/done` is BEST-EFFORT
   > (`sidecar.done` / `sidecar.done_failed` events, never raises — a run
   > must not fail because the sidecar was unreachable at teardown).
   > HAL-005 (2026-08-09): the "last two arrows" are wired into the
   > ACTIVE loop — after every executed runner turn persists its
   > observation/evidence, the supervisor-owned hook
   > (`Supervisor._submit_flag_candidates`, injected into the runner as
   > `flag_submitter`) runs `FlagCandidateExtractor.extract` and drives
   > the supervisor-only `submit_verified_candidate` through the
   > privileged sidecar transport: a newly observed flag is submitted
   > with ZERO LLM calls between seeing it and submitting it, one
   > submission attempt per turn (serialized, AGENTS.md rule 7), and an
   > accepted submission routes the graph DONE on the next iteration —
   > the objective completes and a COMPLETED run fires the best-effort
   > sidecar `/done` (`Supervisor._notify_platform_done`). No-candidate
   > (`MissingRequiredStateError`) is a silent no-op, a platform
   > rejection (`SubmissionRejectedError`) is never re-submitted (the
   > coordinator already marked the candidate rejected), and limit /
   > privilege / config / transient platform failures are recorded as
   > `supervisor.flag_submission_failed` events — a transient failure
   > leaves the candidate verified so the next turn's hook retries it.
   > The runner stays kernel-clean (the hook is injected, never
   > imported) and local mode is byte-for-byte unchanged.
   > HAL-006 (2026-08-09): objective completion is acceptance-gated per
   > environment — the `EnvironmentAdapter` protocol gains
   > `verdict_satisfies_objectives(graph)`, and the runner consults it
   > before completing objectives on an evaluator COMPLETE verdict
   > (`LocalEnvironment` always accepts the verdict, keeping local mode
   > byte-for-byte unchanged; `HalCTFEnvironment` accepts it ONLY when
   > the graph holds an accepted submission entity — the router's
   > terminal signal). A validated hypothesis (COMPLETE verdict) still
   > produces its evidence-backed Finding, but on its own never
   > completes `objective-halctf-flag`: a HalCTF run can no longer
   > terminate COMPLETED unscored. The accepted-submission DONE path is
   > unchanged.
   > HAL-008 (2026-08-09): process-boundary exit policy
   > (docs/adr/0012) — in HalCTF mode (any `HAL_CTF_ID` /
   > `HAL_CHALLENGE_ID` / `HAL_ENDPOINT` / `HAL_MCP_ENDPOINT` /
   > `MCP_ENDPOINT` / legacy `OZZGRAPH_CHALLENGE_ID` non-blank) the
   > process entry point (`ozzgraph.__main__`) maps EVERY structured
   > `TerminationReason` to container exit 0: scored, unsolved,
   > budget-exhausted, gave-up (platform-rejected submission), and
   > graceful FAILED terminations are all ordinary completed attempts,
   > because a nonzero container exit is interpreted by the real event
   > platform as a crash and reruns the detonation (wasting the run
   > budget and marking the run FAILED even when it scored). The
   > internal model is never collapsed: the `termination` event still
   > records the structured reason (`budget_exhausted` / `failed` /
   > `interrupted` / `completed`), and `INTERRUPTED` exits 0 too (a
   > signal stop is how the platform tears a run down). Only
   > startup-impossible stays exit 1: load-time `ConfigError` (missing
   > `HAL_USER_ID`, a set-but-invalid `HAL_TARGET_PORT`), CLI usage
   > errors, and uncaught exceptions. Local mode — `ozzgraph run
   > <target>`, the benchmark CLI, and the read-only image smoke — is
   > byte-for-byte unchanged (0/130/1/3).
   > HAL-011 (2026-08-09): halctf-real-contract regression fixture —
   > the benchmark suite ran the kernel against synthetic lab targets
   > + scripted models, never an actual HalCTF runtime contract;
   > `tests/halctf_contract_fixture.py` + `tests/test_halctf_contract.py`
   > reproduce Tottori's committed live-run contract cross-repo
   > (kazuki005276ssh/halctf-team-tottori, the same source HAL-001 /
   > HAL-004 verified) as REUSABLE data: the exact platform env shape
   > (named `HAL_TARGET_FERRY_IP`/`_PORT` + `HAL_TARGET_UNDERWORLD_IP`/
   > `_PORT` pairs, `HAL_CHALLENGE_ID=18`, `HAL_CHALLENGE_NAME` /
   > `HAL_CHALLENGE_CATEGORY`, `HAL_AGENT_MODEL` / `HAL_RUN_ID` /
   > `HAL_TEAM_UUID`, flag-like env values, `OPENAI_BASE_URL` /
   > `MCP_ENDPOINT`) plus real plain-HTTP listeners (stdlib only) for
   > the observed wire contract: the target's `GET /fetch` statuses
   > 403/404/502/200 (the 200 path serves the challenge flag) and the
   > sidecar's `POST /submit` -> `{"status":"correct","points_awarded":1}`
   > with `POST /done` -> 200. A full-harness child process
   > (`python -m ozzgraph`, the HAL-001..010 production composition)
   > against the fixture scores and terminates COMPLETED: the model
   > (routed from `HAL_AGENT_MODEL` + `OPENAI_BASE_URL`, HAL-003) probes
   > the real ferry listener (no allowlist refusal — the scope carries
   > the merged service allowlist), the 200 body delivers the flag, the
   > supervisor hook submits it through the REAL plain-HTTP sidecar
   > (env-first `OZZGRAPH_SIDECAR_BASE_URL`, HAL-004), the accepted
   > submission completes `objective-halctf-flag` (HAL-006
   > acceptance-gated — not an unexhausted complete), and the run exits
   > 0 with `TERMINATION: completed` and `findings.json` rendered. The
   > deterministic negative control keeps the V09 fallback honest: an
   > env WITHOUT `HAL_TARGET_*` services yields the bare challenge id as
   > the target address, and a non-allowlisted policy refuses the same
   > probes the fixture's allowlist admits.
   > PROFILE-FREE-TIER (2026-08-11): data-driven model profiles for the
   > OpenRouter free tier — `profile_data/openrouter.toml` (the
   > `openrouter/free` / `openrouter/auto*` aliases),
   > `profile_data/nemotron.toml` (the Nvidia Nemotron endpoints those
   > aliases often route to), and `profile_data/gemma.toml` (the Google
   > Gemma endpoints), all declaring the JSON protocol ONLY with
   > `repair_retry`. Before this, an unknown model id resolved to the
   > terminal-only fallback profile, so the harness compiled
   > terminal-format prompts the free models ignored; their structured
   > JSON replies degraded to `think` and the harness never executed a
   > tool (observed against OWASP Juice Shop: 34 turns, 0 tool calls).
   > With a JSON-only profile the harness compiles JSON-format prompts,
   > and e.g. `google/gemma-4-26b-a4b-it:free` returns exactly
   > `{"kind": "run", "payload": "curl -v ..."}` on repeat attempts —
   > `ozzgraph run <target>` with `OZZGRAPH_MODEL_ID=<free model>`
   > drives real actions. Two further fixes made the loop actually
   > execute: (1) `OUTPUT_CONTRACT` no longer prescribes a conflicting
   > JSON schema (`{"action", "skill_id"}`) — it now describes the
   > semantics of one bounded action while each adapter's OUTPUT FORMAT
   > block owns the wire shape, and the harness binds the skill
   > deterministically; (2) the runner advertises the routed skill
   > CARDS (id + card text with concrete commands) instead of bare
   > skill ids, so weak models see the command vocabulary they cannot
   > infer from an id alone (previously every model turn emitted a
   > skill-call like `recon_http_fingerprint --url ...` that the policy
   > plane rejected with exit 127). Two further fixes closed the
   > model-feedback loop: (3) `_fallback_protocol` now prefers the
   > profile's `json` protocol for prompt compilation whenever declared
   > (compiling a terminal-format prompt for a JSON-capable model made
   > it reply JSON with the command inside `kind`, which the JSON
   > parser rejected as a non-`run` kind); (4) `_transcript_tail` is no
   > longer a V01 stub — it renders the last ~6 action outcomes
   > (`RECENT ACTIONS`: OK/FAILED/REJECTED with exit code and command),
   > so a model actually learns that its duplicate/out-of-scope action
   > was rejected instead of proposing it forever (previously every
   > model looped on one command until the budget exhausted).
   > PROVE-ALL-FINDINGS (2026-08-11): every validated finding now
   > renders. Two compounding causes fixed: (1) `_produce_findings`
   > renders EVERY confirmed hypothesis per COMPLETE verdict, not just
   > the first (`next()` became a loop); (2) `FindingStore.save`
   > reloads the on-disk document first, so separate `for_run()`
   > instances (the runner's per-verdict finding and the specialist
   > fleet's per-confirmed-hypothesis finding) append to the same
   > findings.json instead of overwriting each other (observed on
   > Juice Shop: the fleet validated 2 findings in one batch, the run
   > rendered 1). The local completion contract is unchanged: a
   > COMPLETE verdict still satisfies the objective — the fix is that
   > every finding validated before that verdict is rendered.
   > EXHAUSTIVE-MODE (2026-08-11): new opt-in `OZZGRAPH_EXHAUSTIVE=true`
   > makes the local environment NEVER auto-complete the objective on a
   > verdict — the run keeps probing, new observations form new
   > hypotheses, the specialist fleet validates them, and findings
   > accumulate until the budget is spent (the whole box gets assessed,
   > not just the first finding). The planner skips resolved hypotheses
   > in exhaustive mode and declares NoPlan when none remain open, so
   > the model keeps proposing fresh probes instead of looping on an
   > exhausted plan. Default (off) is byte-for-byte unchanged. Verified
   > on Juice Shop: a 25-min exhaustive run rendered 4 findings across
   > 7 executed commands vs 1-2 in default mode.
   > NO-DUPLICATE-FEEDBACK (2026-08-11): the model's rejection feedback
   > now names the rejected COMMAND, not just a fingerprint hash.
   > Previously `_record_turn_failure` rendered only the exception
   > message (`duplicate action rejected: fingerprint 85f680...`), so
   > the model could not tell WHICH proposal was rejected and
   > re-proposed the same command forever (observed: 94 duplicate
   > rejections in one exhaustive run). The rejected action text now
   > appears in the `RECENT ACTIONS` transcript tail, and OUTPUT_CONTRACT
   > carries an explicit NEVER-repeat rule — anything already attempted
   > (OK or REJECTED) must not be proposed again; propose a different
   > path, parameter, or technique instead.
10. `v2/full-regression` — real benchmark suite across the model matrix.
    > V10 (2026-08-08): implemented — `src/ozzgraph/benchmarks/` +
    > `ozzgraph benchmark` CLI + docs/BENCHMARKS.md. The full-regression
    > suite runs EVERY lab target (the 9 suite categories plus the new
    > `dead-end` target) through the REAL `AutonomousRunner` composition
    > (graph, security brain, evaluator, scope policy, tool plane) under a
    > deterministic scripted model (`ScriptedModel`/`ScriptedModelService`,
    > the `tests/test_matrix.py` client pattern) — hermetic, zero network,
    > byte-deterministic report (modulo the lab's ephemeral port). The
    > `dead-end` lab target is a genuine rabbit hole (decoy
    > `/backup/flag.txt` 404 + spoofed flag text, decoy
    > `/backup/creds.txt`, 401 `/admin`; real flag only at `/flag`); its
    > benchmark run PROVES the agent pivots away: the failing decoy probes
    > refute the hypotheses formed on the promising paths
    > (`brain.hypothesis_abandoned`), the `ProgressEvaluator` records a
    > PIVOT verdict (`brain.progress_evaluated`, every hypothesis resolved,
    > objectives incomplete), and the run still completes with the REAL
    > flag in bounded turns. The plain-ReAct baseline
    > (`benchmarks/react.py` — bare propose→execute loop, no graph/brain/
    > evaluator) runs the SAME scripted model on the SAME targets: the
    > report proves OzzGraph beats ReAct on every target (fewer turns and
    > model calls — the harness completes the objective the moment the flag
    > is evidenced on a plan-bound turn; the baseline must wait for the
    > model to submit) and solves the dead-end where a non-submitting
    > baseline loops to its turn cap unsolved. The tool-contract test
    > (`tests/test_tool_contract.py`) proves EVERY
    > `Skill.required_capabilities` entry resolves to a working installed
    > provider via `ToolProvider` (capabilities-not-binaries), and that an
    > unavailable provider is detectable and fails loudly; fixes so the
    > shipped skills resolve in any base environment: `curl` gained
    > `web.content_discovery` (bounded path probing is its own primitive),
    > and `enum_service_version` / `exploit_parameter_injection` dropped
    > the `exploit.search` / `web.sql_injection` requirements (the cards
    > keep searchsploit/sqlmap as deep-dive guidance; the capabilities stay
    > in the catalog for Kali runtimes). `model_client.ModelClient` (a
    > runtime-checkable `complete`+`aclose` protocol) lets the runner
    > accept the hermetic scripted service alongside `ModelService`;
    > `ozzgraph benchmark [--target NAME|--all] [--react] [--max-turns N]
    > [--out FILE]` renders the deterministic markdown report, and
    > `OZZGRAPH_BENCHMARK_MODEL_ID` / `_BASE_URL` / `_API_KEY` select a
    > real model endpoint (no script) for real benchmarks. ~40 new tests.

## What to keep vs rewrite

| Component | Decision |
|---|---|
| state_graph.py, events/replay, artifacts, shell, budgets | **KEEP** |
| Context compiler | KEEP + revise (task-centric, token-aware) |
| Phase concept | KEEP, replace CTF phases with generic lifecycle |
| router.py, policy.py | MAJOR REVISE (capability/effect/scope based) |
| skills.py | MAJOR EXPANSION (full security-domain library) |
| planner.py, executor.py, workers.py | REWRITE |
| observations.py | MAJOR REWRITE (tool-specific semantic parsers) |
| bootstrap.py, supervisor.py | REWRITE |
| hal_client.py | MOVE + REWRITE (optional HalCTF integration) |
| profiles.py | REPLACE (empirical measured profiles) |
| model_client.py | REVISE (generic providers/local endpoints) |
| Dockerfile | REBUILD (Kali + effectively every tool) |
| synthetic lab | EXPAND MASSIVELY |
| existing unit tests | KEEP + add true process-level E2E |

## Key technical changes called out

- **ToolInventory** — startup deterministically inventories every installed
  tool (path, version, capabilities); the model NEVER hears about a
  nonexistent tool. Capabilities are first-class: `http.request`,
  `network.port_scan`, `ad.kerberos_enum`, `source.sast`, etc. Model selects
  intent; OzzGraph picks the executable.
- **NormalizedDecision** — the Executor stops consuming raw JSON; model →
  ModelAdapter → NormalizedDecision (`kind: execute|inspect|validate|replan|
  finish`, `intent`, `arguments`) → IntentResolver → ToolProvider → Executor.
- **ObservationProjector** — tool-native parsers (nmap XML, ffuf JSON, nuclei
  JSONL, semgrep JSON, SARIF, trivy JSON...) convert output to typed
  observations → evidence → graph. Always persist raw output to ArtifactStore
  FIRST.
- **Access & Capability as first-class state** — "valid credential" is doing
  too much work; model Access/Capability directly (RCE, filesystem read, SSRF,
  command_execution, etc.).
- **Effects-based safety** — replace command-family "shell = read-only" with
  explicit `Effect` enum (TARGET_READ, TARGET_MUTATE, ACCESS_CREATE, etc.).
  Parallelism based on effects/targets/conflict keys/scope.
- **RetryPolicy vs LoopPolicy** — "never retry" is too strict; a command can be
  retried when world state changes, just not loop infinitely.
- **Findings model** — richer than flags (CWE classification, affected assets,
  preconditions, evidence_ids, reproduction, impact CIA, confidence).
- **Enriched graph ontology** — Scope, Asset, Host, Service, Endpoint,
  Application, Component, Technology, File, Artifact, Identity, Credential,
  Session, Access, Capability, Permission, Hypothesis, Vulnerability, Finding,
  Evidence, Observation, CodeLocation, DataFlow, Exploit, Impact, Remediation +
  relationship edges (HOST EXPOSES SERVICE, ENDPOINT ACCEPTS PARAMETER, ...).
- **Hints/submissions fully out of the kernel** — move HintPolicy,
  SubmissionCoordinator, Scoreboard, FlagCandidateExtractor under
  `ozzgraph.environments.halctf`.
- **Model profiles empirical** — per-model TOML with measured capabilities,
  not hardcoded families; detect via GET /v1/models, run tiny probe if unknown.
- **Harsher tests** — true process-level E2E per category (web/api/source/
  network/ad/pwn/forensics/cloud/halctf) including deliberate dead ends and a
  tool-contract test (every skill's required capability has a working
  installed provider).

## The "most important fix"

The Supervisor must actually drive the agent. v1's `Supervisor.run()` roughly
does: bootstrap → heartbeat → `while budget: sleep(.25)`. v2 introduces an
`AutonomousRunner` with the full investigate loop: route → check objectives →
next task → plan (only when needed) → compile context → one model action →
execute → persist artifact + observation → update hypotheses → environment
process → evaluate. The vertical slice (`ozzgraph run <target>`) must work
before anything else.

## End state

> **OzzGraph is an autonomous vulnerability-research operating system.**
> HalCTF is one environment. A Docker Compose stack is one environment. A
> local web app is one environment. A Git repo is one environment. A
> vulnerable VM is one environment. The central engine stays the same:
> scope → assets → observations → evidence → hypotheses → experiments →
> validated findings → impact → report.
