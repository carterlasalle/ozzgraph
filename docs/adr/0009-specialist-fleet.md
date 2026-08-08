# ADR-0009: Specialist Fleet — Genuine Narrow Micro-Agents (V07)

Status: accepted

Date: 2026-08-08

## Context

V07 of the v2 milestone plan (docs/CHANGES_v2.md, milestone 7
"v2/specialists") calls for genuine narrow micro-agents:
parallelize independent hypothesis tests, serialize global strategy, and
merge conclusions through the reducer. This adds a new **concurrency
model** to the kernel — the bounded-parallel specialist batch, layered on
the V01 task DAG scheduler (ADR-0004) and the V06 security brain — which
is an ADR trigger per AGENTS.md.

Three forces drive the decision:

1. **AGENTS.md rule #7**: parallelize evidence gathering, not mutable
   exploit chains. Independent hypothesis tests are read-only evidence
   gathering (a bounded reproduction probe) and MUST run concurrently;
   global strategy and state-mutating work MUST serialize. The V01
   scheduler already expresses this with explicit conflict keys
   (ADR-0004) — V07 must use that machinery, not a new scheduler.
2. **No model calls for independent deterministic tests.** The V06 brain
   routes more-than-one-viable-path to the StrategicPlanner (LLM). But a
   batch of independent, deterministic-testable hypotheses needs no LLM:
   each hypothesis's bounded reproduction probe is already known (its
   `exploitation_direction`), so a deterministic micro-agent loop can
   test them all in parallel with ZERO LLM calls.
3. **Every confirmed fact has provenance (rule #3).** The micro-agent
   conclusion must be a structured verdict with mandatory evidence
   references, merge through the reducer into graph facts unchanged, and
   replay-compatibly (additive payload fields only, AGENTS.md "Definition
   of Done").

## Decision

We will implement V07 specialists as a narrow micro-agent loop wired
through the existing scheduler and reducer:

- **`SpecialistMicroAgent` + `MicroAgentTask`** (`src/ozzgraph/workers.py`):
  one task is a bounded bundle of experiments (`experiments: tuple[
  WorkerTask, ...]`, the inherited `command` is locked to the empty string
  so a micro task can never smuggle an unbounded action). Each experiment
  is gated and executed through the existing single-action worker path
  (`SpecialistWorker._execute` — policy gate, fingerprint, bounded shell,
  content-addressed artifact), its raw output normalized with the tool
  parsers (`parser_for_command`), and a deterministic decide concludes a
  structured `Verdict` (`confirmed` / `refuted` / `inconclusive` plus an
  `impact` payload of CWE/assets/confidence). Evidence references are the
  stored artifacts, so every conclusion is evidence-backed (rule #3); a
  run that gathered no evidence FAILS loudly instead of concluding from
  prose. The loop runs at most `MAX_MICRO_ITERATIONS` experiments. ZERO
  model calls; the only context is the hypothesis objective and the prior
  observations — never the full graph.
- **Per-hypothesis conflict keys** (`src/ozzgraph/scheduler.py`):
  `hypothesis_task(id, hypothesis_id)` carries the hypothesis id AS its
  conflict key, so two tasks exploring the SAME hypothesis are mutually
  exclusive while tasks on DIFFERENT hypotheses carry disjoint keys and
  run concurrently under `max_workers` (ADR-0004 `ready_order`). Global
  strategy stays supervisor-serialized through `serialized_task`, and
  state-mutating work serializes through the new reserved
  `MUTATION_CONFLICT_KEY` (a `Task.mutating=True` task must carry exactly
  that key, mirroring the serialized-key contract).
- **Structured verdicts ride the Finding** (`src/ozzgraph/scheduler.py`,
  `src/ozzgraph/reducer.py`): a conclusion travels through the scheduler
  `Finding` as optional `verdict` + `impact` fields, and the reducer
  merges them into a structured `Fact`. The fact fingerprint extends with
  the verdict only when present, so a plain finding keeps its pre-V07 id
  (replay compatibility); verdict/impact are additive payload fields.
- **`SpecialistFleet`** (`src/ozzgraph/specialists.py`): owns the batch
  lifecycle — build one narrow task per hypothesis (skipping loudly a
  hypothesis whose reproduction direction is mutating or whose phase is
  out of scope, leaving it open), schedule through `Scheduler` with
  bounded concurrency, persist one `evidence` entity per succeeded run,
  merge verdicts into facts via `Reducer`, promote confirmed hypotheses
  (with an evidence-backed `finding` + `findings.json`) and abandon
  refuted ones.
- **Runner dispatch gate** (`src/ozzgraph/runner.py`): when a
  `SpecialistFleet` is wired in (`specialists=`), a `StrategicDecision`
  whose opportunities are ALL independent `test_hypothesis` paths
  (`_is_hypothesis_batch`) dispatches the parallel fleet instead of
  calling the StrategicPlanner. Mixed/service paths stay on the LLM
  strategic path; the deterministic single-obvious-action path is
  unchanged. With no fleet wired, V06 behavior is byte-for-byte intact.

## Consequences

**Easier:**

- Independent hypotheses are tested in bounded parallel batches with zero
  model calls — the LLM is reserved for genuinely ambiguous strategic
  choices.
- Conclusions are structured and evidence-backed end-to-end, satisfying
  rule #3 and merging into graph facts unchanged.
- Replay compatibility holds: verdict/impact are additive Finding/Fact
  payload fields, and a plain finding's fact id is identical to pre-V07.
- The mutation contract is explicit: a task that mutates state must
  declare it with the reserved key, so mutation/strategy serializes while
  evidence gathering parallelizes (rule #7).

**Harder:**

- The micro-agent decide is deterministic, not semantic: a clean probe
  with no structured output concludes `refuted` and an empty/malformed
  artifact concludes `inconclusive` — it cannot judge meaning the way a
  model could. Services characterization and mixed-path strategy still
  route through the LLM.
- The runner only batches when a fleet is wired in; building the fleet is
  a supervisor-level composition decision, so the default `AutonomousRunner`
  keeps the V06 model path.
- A hypothesis is only batch-testable when its `exploitation_direction`
  is a read-only (non-mutating) command in a specialist scope phase;
  mutating directions are skipped loudly and stay open for the serialized
  strategic path.
