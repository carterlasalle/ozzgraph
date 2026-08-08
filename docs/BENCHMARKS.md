# Benchmarks (V10 full-regression)

The V10 milestone (docs/CHANGES_v2.md, milestone 10) ships a real,
deterministic benchmark suite that runs the **full OzzGraph harness**
(scope → graph → security brain → evaluator → tool plane → findings)
against the synthetic lab's entire challenge matrix, proves the harness
handles **deliberate dead ends** by pivoting away through the
`ProgressEvaluator`, enforces the **tool contract** (every skill's
required capability resolves to a working installed provider), and
proves **OzzGraph beats a plain-ReAct baseline** on the same targets
and the same model.

The suite lives in `src/ozzgraph/benchmarks/` and is driven by the
`ozzgraph benchmark` CLI. Everything is **hermetic by default**: a
deterministic scripted model stands in for the LLM, so the whole matrix
runs in CI with zero network cost and zero nondeterminism. The same
harness also runs against a **real model endpoint** when configured.

## What it proves

1. **Full-regression across the model matrix.** Every lab category
   (HTTP reconnaissance, hidden routes, authentication logic, source
   vulnerability localization, file forensics, binary string
   extraction, credential reuse, network pivot, multi-stage flag
   discovery) plus the new `dead-end` target is run through the full
   harness and must terminate `COMPLETED` with the target's **real
   flag** evidenced — bounded turns, no loops.

2. **Deliberate dead ends.** The `dead-end` lab target is a genuine
   rabbit hole: `/backup/flag.txt` (404, spoofed flag text),
   `/backup/creds.txt` (credentials `/admin` rejects), and `/admin`
   (401 unconditionally) all look promising and lead nowhere; the real
   flag lives only at `/flag`. The benchmark **proves the agent pivots
   away**: the decoy probes fail deterministically, the hypotheses
   formed on the promising paths are refuted and abandoned
   (`brain.hypothesis_abandoned`), the `ProgressEvaluator` records a
   `PIVOT` verdict (every hypothesis resolved, objectives incomplete —
   `brain.progress_evaluated`), and the run still completes with the
   real flag — bounded iterations, never an infinite loop.

3. **Tool contract.** Every `Skill.required_capabilities` entry
   resolves to a working installed provider via `ToolProvider`
   (capabilities, not binaries), and a capability whose provider is
   unavailable is detectable (`is_resolvable() == False`) and fails
   loudly (`ToolProviderError`) — never a silent guess. The registry
   and skill declarations are fixed so the guarantee holds in any base
   environment: `curl` is a registered `web.content_discovery`
   provider (bounded path probing), and the sqlmap/searchsploit
   deep-dive capabilities were removed from the skill *requirements*
   (the cards keep them as guidance) so the shipped skills are usable
   everywhere, not only in the Kali image.

4. **OzzGraph beats plain ReAct.** The same scripted model runs both
   the full harness and a bare propose→execute baseline (no graph, no
   brain, no evaluator). The full harness completes the run the moment
   the flag is evidenced on a plan-bound turn (objectives complete via
   the evaluator's COMPLETE verdict), while the baseline must keep
   calling the model until it happens to submit — so OzzGraph uses
   **fewer turns and fewer model calls on every target**, and on the
   dead-end target it **solves where the baseline loops** (a naive
   model that never submits burns its whole turn cap unsolved).

## Running

```bash
# Hermetic full-regression matrix (scripted model, zero network cost)
ozzgraph benchmark

# One target, including the plain-ReAct comparison
ozzgraph benchmark --target hidden-routes --react

# The dead-end pivot proof, with the comparison
ozzgraph benchmark --target dead-end --react

# Write the report to a file instead of stdout
ozzgraph benchmark --all --react --out report.md

# Larger per-run turn cap (default 12)
ozzgraph benchmark --max-turns 20
```

The CLI exits 0 on success; the deterministic markdown report prints to
stdout (or `--out FILE`).

### Real-model runs

To benchmark a live OpenAI-compatible endpoint instead of the scripted
model, set:

```bash
export OZZGRAPH_BENCHMARK_MODEL_ID=deepseek-v4-flash
export OZZGRAPH_BENCHMARK_MODEL_BASE_URL=https://api.example.com/v1
export OZZGRAPH_BENCHMARK_MODEL_API_KEY=sk-...        # optional

ozzgraph benchmark --target hidden-routes --react
```

The real model sees the mission, the live target URL, and the
transcript/context each turn and proposes its own actions — no solve
script, no scripting. The same report and comparison render; runs are
of course not deterministic (the model is the source of
nondeterminism, exactly as documented for the matrix suite).

## The report

The markdown report contains a per-(target, harness) table and the
comparison summary:

```
| target | harness | status | solved | turns | model_calls | tool_calls | evidence | pivots | abandoned | decoy | score |
|---|---|---|---|---|---|---|---|---|---|---|---|
| hidden-routes | ozzgraph | completed | yes | 3 | 3 | 3 | 3 | 0 | 0 | no | 1000993 |
| hidden-routes | react | solved | yes | 4 | 4 | 3 | 0 | 0 | 0 | no | 1000880 |
| dead-end | ozzgraph | completed | yes | 5 | 5 | 5 | 5 | 1 | 2 | yes | 1000776 |
| dead-end | react | solved | yes | 6 | 6 | 5 | 0 | 0 | 0 | yes | 1000660 |
```

The table is a pure function of the recorded runs: no timestamps, no
wall-clock durations, and the lab's ephemeral loopback ports never
enter the document — the same runs always render the same report.

## Scoring

`Score = 1,000,000 (solved) + 100 per turn under the cap + 10 per
model call under the cap + min(evidence, 50) + min(pivots, 10)`.

Solving dominates; then fewer turns; then fewer model calls; then a
richer evidence chain; then dead-end pivots (a positive signal — the
harness recognized the rabbit hole). The comparison isolates **harness
behavior**: both harnesses run the identical scripted model on the
identical live target, so a score difference can only come from the
graph/brain/evaluator machinery.

## Hermetic determinism

The scripted model (`ozzgraph.benchmarks.scripted.ScriptedModel`) emits
one bounded terminal-protocol action per turn from a per-target solve
script (`ozzgraph.benchmarks.registry.build_solve_script`), which is
derived from the LIVE target instance — chained targets (network pivot,
multi-stage) discover their next hop through the same bounded shell the
harness uses, so the model never receives information the challenge
does not expose. The flag value is injected as constructor data, the
same convention as the matrix suite's scripted client
(`tests/test_matrix.py`). Runs are a pure function of (script, target);
`tests/test_benchmarks.py` asserts the report is byte-deterministic
modulo the ephemeral port.

## Tool contract

`tests/test_tool_contract.py` enforces the three-layer contract:

1. **Vocabulary** — every `Skill.required_capabilities` entry is a
   registered `ToolCatalog` capability.
2. **Resolution** — a real PATH inventory is probed and every required
   capability resolves to an installed provider (the shipped skills
   must be usable in the shipped runtime).
3. **Loud failure** — an unavailable capability is detectable
   (`is_resolvable() == False`) and `ToolProvider.resolve` raises
   `ToolProviderError` naming it.

## Where the code lives

| Concern | Module |
|---|---|
| Records, scoring, report model | `src/ozzgraph/benchmarks/models.py` |
| Scripted deterministic model (+ service form) | `src/ozzgraph/benchmarks/scripted.py` |
| Full-harness driver (AutonomousRunner wiring) | `src/ozzgraph/benchmarks/ozzgraph_harness.py` |
| Plain-ReAct baseline | `src/ozzgraph/benchmarks/react.py` |
| Target matrix, decoys, solve scripts | `src/ozzgraph/benchmarks/registry.py` |
| Markdown rendering | `src/ozzgraph/benchmarks/report.py` |
| Orchestration (`run_benchmark` / `run_all_benchmarks`) | `src/ozzgraph/benchmarks/__init__.py` |
| The rabbit-hole lab target | `src/ozzgraph/lab/targets.py` (`DeadEndTarget`) |
| CLI | `src/ozzgraph/__main__.py` (`ozzgraph benchmark`) |
| Tests | `tests/test_benchmarks.py`, `tests/test_tool_contract.py` |
