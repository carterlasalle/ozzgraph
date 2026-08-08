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
4. `v2/semantic-observations` — typed parsers/projectors for the highest-value
   tools (nmap, ffuf, nuclei, netexec, semgrep, CodeQL, trivy, gitleaks, ...);
   mandatory raw-artifact persistence before summarization.
5. `v2/model-harness-matrix` — benchmark models empirically (format
   compliance, tool selection, repetition, evidence grounding, solve rates);
   choose protocols from data.
6. `v2/security-brain` — OpportunityGenerator, StrategicPlanner, TaskBuilder,
   HypothesisManager, ProgressEvaluator.
7. `v2/specialists` — genuine narrow micro-agents; parallelize independent
   hypotheses, serialize global strategy, merge through reducer.
8. `v2/local-assessment` — URL/network/repository/Docker-Compose/hybrid modes,
   credentials, scope files, reporting, SARIF; default OzzGraph experience.
9. `v2/halctf-adapter` — HAL_* discovery, official tool set, smoke flag,
   scoring, hint costs, graceful completion; no kernel contamination.
10. `v2/full-regression` — real benchmark suite across the model matrix.

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
