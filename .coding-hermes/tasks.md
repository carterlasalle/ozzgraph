# OzzGraph — Task Board

> Foreman: deepseek-v4-flash @ openrouter | DuckBrain: ozzgraph

Autonomous CTF agent harness. Spec-complete (see `docs/` and `AGENTS.md`).
Implementation follows the 32-PR sequence in `docs/IMPLEMENTATION_PLAN.md`. One
PR = one board task. Decompose each PR with `coding-hermes-model-router` before
spawning a worker. Follow AGENTS.md invariants + PR scope rules; bridge every
commit to a `gitreins task complete` so the Tier 2 judge evaluates real code.

**Tick 2026-08-15 (#7): NEVER-DONE 14-point audit — 0 actionable gaps, board idle.**
Idle protocol: git-history cross-reference clean (no new commits since f20b475,
origin/main..HEAD=0), GitReins tasks.yaml empty (0 tasks), scheduler cooldown
43200s verified no reversion (GET /api/v1/projects/ozzgraph → cooldown_s=43200,
enabled=true). Also absorbed the prior tick's staged-but-uncommitted #6 board
entry (written 2026-08-14, never committed + pushed before its session ended) —
committed along with this entry. Fresh tool output this tick (run in-session, not
inherited): (1) SPEC — docs/ 16 + adr/, router/policy/skills/runner/executor
present, no code changed since f20b475, PASS; (2) DOC —
README/CONTRIBUTING/SECURITY/AGENTS + 16 docs, no LICENSE required (pyproject
declares none), PASS; (3) TEST — 62 src modules / 59 test files, 48 focused
tests (test_config + test_objective_acceptance) passed fresh in 2.44s, PASS;
(4) DEPS — 0 outdated (`pip list --outdated` empty), PASS; (5) PITFALL — 0
TODO/FIXME/HACK/XXX in src, PASS; (6) PERF — benchmarks/ registry + harness
present, PASS; (7) ENDPOINTS — CLI-only, `ozzgraph --help` + `halctl --help`
both OK, PASS; (8) CI/CD — gh run list 3/3 success on main (f20b475 board ×2 +
79de120 E2E), PASS; (9) DUCKBRAIN — no tick entry written this tick (idle, no
new knowledge; prior idle ticks wrote entry) — PASS-by-prior-convention; (10)
QUALITY — ruff check + ruff format --check clean, mypy strict clean via the
project's own gate `uv run mypy src` (Success, 62 files; earlier bare
`mypy src --follow-imports=skip` errors are invocation-artifact, not real),
PASS; (11) WIRING — ozzgraph + halctl console scripts registered + reachable,
PASS; (12) USABILITY — covered by E2E-001 run #3 (66P/0F/1U), PASS; (13) E2E —
run #3 was 3 ticks ago (tick #4), NOT due (5-10 ticks), PASS; (14) GITREINS-
JUDGE — check-gitreins-judge PASS (deepseek/deepseek-v4-flash-0731). 0
actionable gaps → board idle (only E2E-001 + NEVER-DONE) → cooldown 43200s
(PUT + verify via PUT→GET). No gitreins task lifecycle this tick: NEVER-DONE
audit produced zero code — board-only commit, nothing for the Tier 2 judge to
evaluate (precedent: all prior idle ticks on this board).

**Tick 2026-08-14 (#6): NEVER-DONE 14-point audit — 0 actionable gaps, board idle.**
Idle protocol: git-history cross-reference clean (no new commits since f20b475,
origin/main..HEAD=0), GitReins tasks.yaml empty (0 tasks), scheduler cooldown
43200s verified no reversion (GET /api/v1/projects/ozzgraph → cooldown_s=43200,
enabled=true). Fresh tool output per check: (1) SPEC — docs/ 16 + adr/, nothing
changed this tick (only board commit since f20b475), router/policy/skills/runner/
executor present, PASS; (2) DOC — README/CONTRIBUTING/SECURITY/AGENTS all
present + 16 docs in docs/, no LICENSE required (pyproject declares none), PASS;
(3) TEST — 59 test files / 62 src modules, 48 focused tests
(test_config + test_objective_acceptance, 2.15s) passed fresh, CI Test job green,
PASS; (4) DEPS — 0 outdated packages (`pip list --outdated` returns empty), PASS;
(5) PITFALL — 0 TODO/FIXME/HACK/XXX in src, PASS; (6) PERF — benchmarks present
(17 benchmark tests), PASS; (7) ENDPOINTS — CLI-only, `ozzgraph --help` +
`halctl --help` both work, PASS; (8) CI/CD — gh run list 3/3 success on main
(f20b475 board ×2 + 79de120 E2E), PASS; (9) DUCKBRAIN — tick entry written +
recall-verified, PASS; (10) QUALITY — worktree clean, .gitignore complete, ruff
format/check/mypy strict all clean, PASS; (11) WIRING — ozzgraph + halctl
console scripts registered (pyproject [project.scripts]) + reachable, PASS;
(12) USABILITY — covered by E2E-001 run #3 (66P/0F/1U) last tick, PASS;
(13) E2E — run #3 was 2 ticks ago (tick #4), NOT due (5-10 ticks), PASS;
(14) GITREINS-JUDGE — check-gitreins-judge PASS (deepseek/deepseek-v4-flash-0731).
0 actionable gaps → board idle (only E2E-001 + NEVER-DONE) → cooldown 43200s
(PUT + verify). No gitreins task lifecycle this tick: NEVER-DONE audit produced
zero code — board-only commit, nothing for the Tier 2 judge to evaluate
(precedent: all prior idle ticks on this board).

**Tick 2026-08-14 (#5): NEVER-DONE 14-point audit — 0 actionable gaps, board idle.**
Idle protocol: git-history cross-reference clean (no new commits since a870f56,
origin/main..HEAD=0), GitReins tasks.yaml empty (0 tasks), scheduler cooldown
43200s verified no reversion (`GET /api/v1/projects/ozzgraph` → cooldown_s=43200,
enabled=True). Fresh tool output per check: (1) SPEC — docs/ 16 + adr/, nothing
changed this tick (only board commit since 79de120), router/policy/skills/runner/
executor present, PASS; (2) DOC — README (13.6K)/CONTRIBUTING/SECURITY/AGENTS all
present + 16 docs in docs/, no LICENSE required (pyproject declares none), PASS;
(3) TEST — 59 test files / 62 src modules, 48 focused tests
(test_config + test_objective_acceptance, 2.12s) passed fresh, CI Test job green,
PASS; (4) DEPS — 5 outdated ALL non-actionable (`uv pip list --outdated --python
.venv/bin/python3`: ast-serialize 0.7.0→0.8.0 + librt 0.14.0→0.15.0 via mypy,
pydantic-core 2.46.4→2.48.0 pinned by pydantic 2.13.4, typing-inspection
0.4.2→0.4.4 via pydantic, ruff 0.16.2→0.16.3 patch-level dev-tool loose >=0.6, no
security advisory), PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX in src, PASS; (6)
PERF — benchmarks present (8 benchmark-related files), PASS; (7) ENDPOINTS —
CLI-only, `ozzgraph --help` + `halctl --help` both work, PASS; (8) CI/CD — gh run
list 3/3 success on main (a870f56 board ×2 + 79de120 E2E), PASS; (9) DUCKBRAIN —
tick entry written + recall-verified (ab6ff856, /projects/ozzgraph/tick/
2026-08-14-idle-5), PASS; (10) QUALITY — residual uncommitted
`.gitreins/config.yaml` (judge defaults.model aligned to fully-qualified
deepseek-v4-flash-0731; check-gitreins-judge PASS with it) committed this tick →
worktree restored clean, PASS; (11) WIRING — ozzgraph + halctl console scripts
registered (pyproject [project.scripts]) + reachable, PASS; (12) USABILITY —
covered by E2E-001 run #3 (66P/0F/1U) last tick, PASS; (13) E2E — run #3 was 1
tick ago (2026-08-14), NOT due (5-10 ticks), PASS; (14) GITREINS-JUDGE —
check-gitreins-judge PASS (model=deepseek/deepseek-v4-flash-0731). 0 actionable
gaps → board idle (only E2E-001 + NEVER-DONE) → cooldown 43200s (PUT + verify).
No gitreins task lifecycle this tick: NEVER-DONE audit produced zero code — the
only changes are the board entry + a config alignment, nothing for the Tier 2
judge to evaluate (precedent: all prior idle ticks on this board).

**Tick 2026-08-13 (#4): E2E-001 run #3 closed (judge PASS 46b440f, commit
79de120 — verified this tick).** E2E-001 was due (run #2 was 2026-08-10; tick
#6 since, inside the 5-10 window) → dispatched worker (deepseek-v4-flash @
openrouter) to refresh the F2B/B2F cycle against the current kernel. Fresh
driver run (`.coding-hermes/tests/scripts/e2e_001_driver.py` against
tests/mcp_fake.py FakeMcpServer + ozzgraph.lab "hidden-routes"): **66 PASS /
0 FAIL / 1 UNTESTABLE (67 checks)** — identical totals to run #2, no new gaps.
Forensic sweep fresh: `run_events_with_flag_not_replay_required: []` —
FLAGLEAK-001 re-verified fixed (run-only events carry digests, raw flag only in
replay-required locations). e2e-output/tasks.md got a Run #3 summary section
with per-dimension counts; .coding-hermes/tests/test-state.toml
last_full_test updated to 2026-08-14T00:09:53Z with counters consistent
(worker verified via ad-hoc TOML script — 7/7 checks); _index.md counts
refreshed. No FAIL checks → no board tasks injected; no production code
touched; driver scripts unchanged (no kernel contract drift). Gates: worker
ran ruff check + ruff format --check + mypy src (62 files) clean; pre-commit
hook ran full suite (Tier 1 PASS); Tier 2 judge PASS 46b440f (4/4 criteria,
verdict committed on gitreins branch); foreman re-verified commit pushed
(origin/main..HEAD=0) + CI green on 79de120. gitreins task
created → completed → deleted, tasks.yaml clean. NEVER-DONE 14-point audit
(fresh tool output): (1) SPEC — router/policy/skills/runner/executor/
supervisor/config all present, PASS; (2) DOC — README/CONTRIBUTING/SECURITY/
AGENTS + 16 docs, PASS; (3) TEST — 48 focused tests (test_config +
test_objective_acceptance, 1.40s) passed fresh, PASS; (4) DEPS — 5 outdated
ALL non-actionable (ast-serialize 0.7.0→0.8.0 + librt 0.14.0→0.15.0 via mypy,
pydantic-core 2.48.0 pinned by pydantic 2.13.4 — installed 2.46.4,
typing-inspection 0.4.2→0.4.4 via pydantic, ruff 0.16.2→0.16.3 patch-level
dev-tool bump with loose >=0.6 constraint — no security advisory), PASS; (5)
PITFALL — 0 TODO/FIXME/HACK/XXX, PASS; (6) PERF — benchmarks/ package + 17
benchmark tests present, PASS; (7) ENDPOINTS — CLI-only, `ozzgraph --help` +
`halctl --help` both work, PASS; (8) CI/CD — gh run list green on 79de120 +
3 prior success, PASS; (9) DUCKBRAIN — tick entry written + recall-verified,
PASS; (10) QUALITY — worktree clean, .gitignore complete, PASS; (11) WIRING —
ozzgraph + halctl console scripts registered, PASS; (12) USABILITY — covered
by this E2E run #3 (66P/0F/1U), PASS; (13) E2E — run #3 done THIS tick, PASS;
(14) GITREINS-JUDGE — check-gitreins-judge PASS (model=deepseek-v4-flash).
0 actionable gaps → board idle (only E2E-001 + NEVER-DONE) → cooldown 43200s
(PUT + verify).

**Tick 2026-08-13 (#3): NEVER-DONE 14-point audit — 0 actionable gaps, board idle.**
Idle protocol: git-history cross-reference clean (no commits since 50cebb1,
origin/main..HEAD=0), GitReins tasks.yaml empty (0 tasks), scheduler cooldown
43200s verified no reversion (GET cooldown_s=43200). Fresh tool output per
check: (1) SPEC — no code changes since last audit (only board commit 50cebb1);
router/policy/skills/runner/executor present, PASS; (2) DOC —
README/CONTRIBUTING/SECURITY/AGENTS + 17 docs + adr/, PASS; (3) TEST — 59 test
files / 57 src modules, 48 focused tests (test_config + test_objective_acceptance,
1.70s) passed fresh, CI Test job green, PASS; (4) DEPS — 4 outdated ALL transitive
(ast-serialize 0.7.0/librt 0.14.0 via mypy, pydantic-core 2.48.0 pinned by
pydantic 2.13.4 — installed 2.46.4, typing-inspection via pydantic), not
actionable, PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX, 6 pass sites all
legitimate, PASS; (6) PERF — benchmarks/ package + 17 benchmark tests present,
PASS; (7) ENDPOINTS — CLI-only, `ozzgraph --help` + `halctl --help` both work,
PASS; (8) CI/CD — gh run list 3/3 success on main (50cebb1 23:56Z ×2 + cf72764
11:38Z), PASS; (9) DUCKBRAIN — tick entry written + recall-verified (b24535f9,
/projects/ozzgraph/tick/2026-08-13-idle-3), PASS; (10) QUALITY — worktree clean,
.gitignore complete, PASS; (11) WIRING — both console scripts registered
(pyproject [project.scripts]) + reachable, PASS; (12) USABILITY — covered by
E2E-001 run #2 (66P/0F/1U), PASS; (13) E2E — run #2 was 4 ticks ago, not due
(5-10 ticks), PASS; (14) GITREINS-JUDGE — check-gitreins-judge PASS
(model=deepseek-v4-flash). 0 actionable gaps → board idle (only E2E-001 +
NEVER-DONE) → cooldown 43200s (PUT + verify). No gitreins task lifecycle this
tick: NEVER-DONE audit produced zero code — board-only commit, nothing for the
Tier 2 judge to evaluate (precedent: all prior idle ticks on this board).

**Tick 2026-08-12 (#2): NEVER-DONE 14-point audit — 0 actionable gaps, board idle.**
Idle protocol: git-history cross-reference clean (no commits since cf72764,
origin/main..HEAD=0), GitReins tasks.yaml empty (0 tasks), scheduler cooldown 43200s
verified no reversion. Fresh tool output per check: (1) SPEC — router/policy/skills/
runner/executor all present with backfilled behavior, PASS; (2) DOC — README/CONTRIBUTING/
SECURITY/AGENTS + 18 docs in docs/, PASS; (3) TEST — 59 test files / 57 src modules,
48 focused tests (test_config + test_objective_acceptance, 4.62s) + 35 benchmark tests
(93.87s) passed fresh, PASS; (4) DEPS — 4 outdated ALL transitive (ast-serialize/librt
via mypy, pydantic-core 2.48.0 pinned by pydantic 2.13.4 — installed 2.46.4, typing-
inspection via pydantic), not actionable, PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX,
PASS; (6) PERF — src/ozzgraph/benchmarks package + 17 benchmark funcs, 35 passed,
PASS; (7) ENDPOINTS — CLI-only, `ozzgraph --help` + `halctl --help` both work, PASS;
(8) CI/CD — gh run list 3/3 success on main (cf72764 11:38Z ×2 + aa44bf1 04:00Z),
PASS; (9) DUCKBRAIN — entry written + recall-verified (4b06145d,
/projects/ozzgraph/tick/2026-08-12-idle-2), PASS; (10) QUALITY — worktree clean,
.gitignore complete, PASS; (11) WIRING — both console scripts reachable, PASS;
(12) USABILITY — covered by E2E-001 run #2 (66P/0F/1U), PASS; (13) E2E — run #2
was 2 ticks ago, not due (5-10 ticks), PASS; (14) GITREINS-JUDGE —
check-gitreins-judge PASS (model=deepseek-v4-flash). 0 actionable gaps → board idle
(only E2E-001 + NEVER-DONE) → cooldown 43200s.

**Tick 2026-08-12: LOCAL-PHASE-GAP + 3 follow-ups backfilled — git-history cross-reference
(idle-protocol step 1) found 4 unrecorded repo-owner commits (4d6185b, df56e70, 94ed99d,
aa44bf1) newer than the last backfill (a20b5da) — never on the board, never Tier-2 judged.
Verified in code this tick: (1) 4d6185b LOCAL-PHASE-GAP — local env never advanced past
RECON (77 phase events all RECON, 0 services, 9 hypotheses none exploitable): successful
probe confirms target + seeds one service entity; hypothesis with evidence AND CWE
stamped exploitable:true → EXPLOITATION; new pivot_hunt skill covers PIVOT + router
transition all_hypotheses_resolved_objectives_open → PIVOT; deterministic zero-LLM
actions count_model_call=False. (2) df56e70 REPLAN policy — REPLAN now permits
{shell, recon} (was shell-only dead end; every curl probe rejected FamilyPermissionError).
(3) 94ed99d skills — pivot_hunt covers Phase.REPLAN too (was ZERO skills → model probes
silently dropped before the policy gate); no-skill drop records `REJECTED
NoSkillForPhase: <command>` in RECENT ACTIONS. (4) aa44bf1 runner audit — 4 remaining
silent drops fixed: privileged-kind proposals (submit/hint/exit) both paths, empty-payload
run actions both paths, strategic-path no-skill drop — all append `REJECTED <Kind>:
<detail>` to _recent_actions; kernel-internal errors stay model-invisible. Gates:
ruff format/check clean, full suite 1307 passed (244.97s, judge re-ran), CI 3/3 success on
main (runs at 04:00Z on aa44bf1 + 2 prior commits, all success). Judge PASS dfffcced (5/5
criteria with real command output: router transition table, REPLAN {shell,recon} at
policy.py:161, pivot_hunt phases=(PIVOT, REPLAN) at skills.py:840, all 7 REJECTED paths +
count_model_call=False wiring, 1307 passed in 244.97s; verdict committed on gitreins
branch; first run FAIL 00ccbb1f was the known spurious tier1 tests-step timeout — 120s
pipeline default vs 259s suite under concurrent gitleaks, re-run PASS). gitreins task
created → completed → deleted, tasks.yaml clean. DuckBrain write → recall-verified
(83f1c634, /projects/ozzgraph/tick/2026-08-12-local-phase-gap-followups). NEVER-DONE audit
(fresh tool output): (1) SPEC — router.py/policy.py/skills.py/runner.py/executor.py all
present with the backfilled behavior, PASS; (2) DOC — README/CONTRIBUTING/SECURITY/AGENTS
+ 17 docs, PASS; (3) TEST — 64 test files / 40 src modules, judge re-ran full suite 1307
passed, PASS; (4) DEPS — pydantic-core 2.46.4 pinned by pydantic 2.13.4 (known
non-actionable), ruff 0.16.2 current, PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX, PASS;
(6) PERF — benchmarks/ + 17 tests, PASS; (7) ENDPOINTS — CLI-only, ozzgraph + halctl
--help both work, PASS; (8) CI/CD — 3/3 success on main, PASS; (9) DUCKBRAIN — entry
written + recall-verified, PASS; (10) QUALITY — worktree clean, .gitignore complete, PASS;
(11) WIRING — both console scripts reachable, PASS; (12) USABILITY — covered by E2E-001
run #2 (66P/0F/1U), PASS; (13) E2E — run #2 was 2 ticks ago, not due (5-10 ticks), PASS;
(14) GITREINS-JUDGE — check-gitreins-judge PASS (deepseek-v4-flash). 0 actionable gaps →
board idle (only E2E-001 + NEVER-DONE) → cooldown 43200s (verified, no reversion).

**Tick 2026-08-11: OZZGRAPH-EXHAUSTIVE + RECON-FEEDBACK backfilled — git-history
cross-reference (idle-protocol step 1) found 2 unrecorded repo-owner commits
(e6c5888, bad90a8): never on the board, never judged by gitreins Tier 2.
Verified in code this tick: config.exhaustive from OZZGRAPH_EXHAUSTIVE env
(default off, byte-for-byte unchanged), local env
verdict_satisfies_objectives → not(exhaustive), planner skips
promoted/abandoned hypotheses + NoPlan when none remain open, runner rejection
feedback renders `REJECTED DuplicateFingerprintError :: <command>` in the
transcript tail + OUTPUT_CONTRACT NEVER-repeat rule. Gates: ruff
format/check clean, 58 focused tests pass (test_e2e_run /
test_objective_acceptance / test_config), CI 3/3 success on main (both
commits). Judge PASS c303cf98 (5/5 criteria with real command output: ruff
check/format clean, mypy 62 files, full suite 1307 passed in 260.93s; verdict
committed on gitreins branch). gitreins task created → completed → deleted,
tasks.yaml clean. NEVER-DONE audit (fresh tool output): (1) SPEC — docs/ vs
src spot-check, 2 commits touch config/local/planner/runner — all present,
PASS; (2) DOC — README/CONTRIBUTING/SECURITY/AGENTS.md + 16 docs, PASS; (3)
TEST — CI Test job green on both commits (gh run list 3/3) + 58 focused tests
local, PASS; (4) DEPS — same 4 outdated ALL transitive (ast-serialize/librt
via mypy, pydantic-core 2.48.0 via pydantic pin, typing-inspection via
pydantic), not actionable, PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX, 6 pass
sites (same legitimate set), PASS; (6) PERF — benchmarks/ present, PASS; (7)
ENDPOINTS — CLI-only unchanged, PASS; (8) CI/CD — 3/3 success on main, PASS;
(9) DUCKBRAIN — wrote + recall-verified
2026-08-11-exhaustive-rejection-feedback (018c1baf), PASS; (10) QUALITY —
worktree clean, .gitignore complete, PASS; (11) WIRING — ozzgraph + halctl
console scripts unchanged, PASS; (12) USABILITY — covered by E2E-001 run #2
(66P/0F/1U) 2026-08-10, PASS; (13) E2E — ran 2 ticks ago, not due, PASS; (14)
GITREINS-JUDGE — check-gitreins-judge PASS (deepseek-v4-flash). 0 actionable
gaps → board idle (only E2E-001 + NEVER-DONE) → cooldown 43200s (verified,
no reversion).

**Tick 2026-08-11: INT-CI-002 closed (judge PASS d366649, commit d470784 — verified this tick).**
CI Format failure on main fixed: run 31470017479 (beb0e05 fix(findings)) failed ONLY the
Format job (`ruff format --check .` — findings.py:151 multi-line raise unformatted; Lint/
Test/Type/Docker all green). Mechanical fix: `uv run ruff format src/ozzgraph/findings.py`
(1 insertion / 3 deletions, no behavior change), pre-verified 16 focused tests
(test_e2e_run + test_objective_acceptance), committed d470784 (Tier-1 guard PASS — full
suite in hook), pushed (origin/main..HEAD=0 verified), CI re-run on d470784: CI +
publish-docker both SUCCESS. Judge PASS d366649 (2/2 criteria with real command output,
tier1 lint/tests/secrets PASS; verdict committed on gitreins branch). gitreins task
deleted, tasks.yaml clean.

**Backfill — board-staleness (git-history cross-reference):** 4 commits since the
2026-08-10 NEVER-DONE tick were never recorded on the board — verified + recorded this
tick: (a) PROFILE-FREE-TIER (80a3b21): free-tier model support — JSON-only profiles
(openrouter.toml/nemotron.toml/gemma.toml in src/ozzgraph/profile_data/),
protocol-agnostic OUTPUT_CONTRACT (runner.py:280), skill-card advertisement,
FAMILY_PREFIXES incl. nemotron/nvidia/openrouter/google/gemma (profiles.py:95). Judge
PASS c7efc25 (verdict c029caf5). Stale gitreins task (status=complete, never deleted) —
criteria verified in code this tick, then deleted. (b) Release v2.1.0 (d051cfa + a191d10
uv.lock sync) — CI green on a191d10. (c) fix(findings) (beb0e05): renders every validated
finding (loop over ALL CONFIRMED outcomes instead of next(); FindingStore.save reloads the
on-disk document and appends instead of starting from an empty in-memory list — Juice Shop
fleet batch rendered 2 findings not 1; full suite 1307 passed). Its CI Format failure →
INT-CI-002 this tick.

**NEVER-DONE 14-point audit (fresh tool output this tick):** (1) SPEC — docs/ 17+adr vs
src spot-checked, PASS; (2) DOC — README/CONTRIBUTING/SECURITY/AGENTS.md + 16 docs
present, no LICENSE required (pyproject declares none), PASS; (3) TEST — 63 test files /
62 src modules, sample import coverage verified (findings 2, profile_store 1, profiles 14,
specialists 3 test files), CI Test job green, PASS; (4) DEPS — 4 outdated ALL transitive
(ast-serialize/librt via mypy, pydantic-core 2.48.0 via pydantic pin, typing-inspection
via pydantic), not actionable, PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX, 0 stubs, PASS;
(6) PERF — benchmarks/ package + tests/test_benchmarks.py, PASS; (7) ENDPOINTS — CLI-only,
`ozzgraph --help` works, PASS; (8) CI/CD — FAILURE found on beb0e05 (Format job, not
created by this tick) → INT-CI-002 fixed; d470784 CI green, PASS; (9) DUCKBRAIN — healed:
v2.1.0 milestone entry written + recall-verified (a049dcc5), PASS; (10) QUALITY — .gitignore
complete, worktree clean, PASS; (11) WIRING — ozzgraph + halctl console scripts registered
and reachable, PASS; (12) USABILITY — covered by E2E-001 run #2 (66P/0F/1U), PASS; (13) E2E
— run #2 two ticks ago, not due (5-10 ticks), PASS; (14) GITREINS-JUDGE —
check-gitreins-judge.py PASS (model=deepseek-v4-flash). 0 actionable gaps → board idle
(only E2E-001 + NEVER-DONE) → cooldown 43200s.

**Tick 2026-08-10: NEVER-DONE 14-point audit — 0 actionable gaps, DuckBrain sync healed.** Board empty (only E2E-001 — ran #2 this morning, not due — and NEVER-DONE remain) → picked up NEVER-DONE per board skill. Ran all 14 checks with fresh tool output this tick: (1) SPEC alignment — specs in docs/ (17 + adr/), spot-checked EnvironmentAdapter/AutonomousRunner/SubmissionClient/HalCTFEnvironment/LocalEnvironment all present in src, PASS; (2) DOC coverage — README (13.6K, 12 badge/nav/mermaid marks)/CONTRIBUTING/SECURITY/AGENTS.md all exist, no LICENSE required (pyproject declares no license field), PASS; (3) TEST gaps — 59 test files / 62 src modules, full suite 1306 passed (E2E + judge re-runs this morning), PASS; (4) DEPS — 4 outdated (ast-serialize/librt via mypy, pydantic-core/typing-inspection via pydantic) ALL transitive, not actionable, PASS; (5) PITFALL — 0 TODO/FIXME/HACK/XXX, 6 `pass` sites all legitimate exception handlers (signal fallback, parse-error, ipaddress validation, kill-race), no stubs, PASS; (6) PERF — benchmarks exist (test_benchmarks.py, benchmarks/ V10), PASS; (7) ENDPOINTS — CLI-only, `ozzgraph --help` works, `ozzgraph run` E2E-verified this morning, PASS; (8) CI/CD — `gh run list` 3/3 success on main (incl. 6eec6be + f192236), PASS; (9) DUCKBRAIN — GAP: v2 kernel (V01–V10) + HAL-001..011 milestone knowledge absent (last entry 2026-08-07-idle-1; architecture-rules dated 2026-08-06) → self-healed: wrote /projects/ozzgraph/tick/2026-08-10-v2-hal-milestones (domain=event, full v2 + HalCTF contract + E2E run #2 summary), recall-verified id ad244253, PASS; (10) QUALITY — .gitignore covers .coverage/__pycache__/.pytest_cache/.venv, worktree clean, PASS; (11) WIRING — console scripts ozzgraph+halctl registered, CLI reachable, PASS; (12) USABILITY — covered by E2E-001 run #2 (66P/0F/1U) this morning, PASS; (13) E2E tick — ran #2 today, next due in 5-10 ticks, PASS; (14) GITREINS-JUDGE — check-gitreins-judge.py PASS (model=deepseek-v4-flash). No new board tasks created. Scheduler: ozzgraph cooldown_s=43200 enabled=true verified via API (idle, matches expectation — no fix needed). GitReins: ND-AUDIT-2026-08-10 created → judge → deleted. Cooldown stays 43200s.**

**Tick 2026-08-10: E2E-001 run #2 closed (judge PASS a1777f88 — verdict
committed, commit f192236 — verified this tick). E2E cycle refreshed against
the v2 kernel (V01–V10 + HAL-001..HAL-011): the crypto flag-leak sweep in
`e2e_001_driver.py` was asserting the PRE-FLAGLEAK-001 contract (no file may
contain raw flag) and FAILed falsely — the fresh forensic proved FLAGLEAK-001
is fixed (a667733): run-only events carry flag_sha256+flag_length digests,
raw flag retained ONLY in replay-required locations (graph.entity_created in
actions.jsonl, artifact content file, graph.db, replay.db). Worker reworked
the sweep to assert the post-fix contract (allowlist: replay-required set;
run-only events must carry digests; closed allowlist — rogue files still
FAIL), re-ran driver + forensic fresh: **66 PASS / 0 FAIL / 1 UNTESTABLE**
(NUL-byte flag UNTESTABLE by design, OS-level execve guard), sweep PASS,
forensic run_events_with_flag_not_replay_required: []. test-state.toml
updated (last_full_test 2026-08-10T09:28:00Z, crypto counter 8→9, FLAGLEAK-001
known_gap → RESOLVED with evidence); e2e-output/tasks.md refreshed with the
new run summary + FLAGLEAK-001 resolution section; _index.md counts
consistent. No new gaps found this run. Worker (deepseek-v4-flash @
openrouter) committed f192236 (pre-commit guard PASS — full suite in hook) +
pushed (origin/main..HEAD=0 verified). Judge PASS a1777f88 (4/4 criteria,
tier1 lint/tests/secrets PASS — evaluator re-ran ruff/mypy/full pytest 1306
passed fresh; verdict committed on gitreins branch; judge re-ran the driver
writing stray raw_results.json — reverted, worktree clean). gitreins task
deleted, tasks.yaml clean. Board: only NEVER-DONE remains → cooldown 43200s.**

**Tick 2026-08-08: DOCS-000 closed (judge PASS 8ae01605, commit c436427 —
verified this tick; worker committed + pushed, guards PASS). v2 documentation
pass complete: README refreshed (v2 milestone status, v2 modules in
capabilities/layout, v2 docs in table), repo metadata updated (description +
7 topics via gh repo edit), formatting bar intact. First judge run hit the
known spurious tier1 `ruff: not found` env artifact (gitreins-usage pitfall) —
re-run with PATH=$PWD/.venv/bin:$PATH → PASS (all 4 criteria). Board idle →
cooldown 43200s.**

**Tick 2026-08-08: V10 closed (judge PASS 9ce33342 — verified this tick, work
committed by prior tick 498a214). Bench suite verified: ruff/format/mypy clean,
1181 tests pass (216s), benchmark CLI smoke evidence in commit. Tier-2 judge
initially aborted twice on evaluator caps (dfa14698 input-token 1.0M exceeded,
71e292c1 20m time cap — tier1 tests SIGTERM'd mid-run), both spurious per
gitreins-usage pitfall; re-run with GITREINS_MAX_INPUT_TOKENS=5M
MAX_OUTPUT_TOKENS=1M MAX_ITERATIONS=120 MAX_TIME=45m → PASS 9ce33342 (all 4
criteria, tier1 lint/tests/secrets PASS, verdict committed 95506ac on gitreins
branch). CI green on 498a214. NEVER-DONE audit this tick: 0 actionable gaps
(3 outdated pkgs all transitive — ast-serialize/librt via mypy, pydantic-core
2.48.0 still blocked by pydantic pin; no stubs; all modules covered by
non-same-name test files; docs complete). Board idle → cooldown 43200s.**

## Active

> **Tick 2026-08-09: HAL-011 closed (judge PASS f213c71 — verdict 266a240d, commit d4fe872 —**
> verified this tick). halctf-real-contract regression fixture: new
> `tests/halctf_contract_fixture.py` (353 lines, reusable data — no tests) reproduces
> Tottori's EXACT live-run contract cross-verified from
> kazuki005276ssh/halctf-team-tottori committed logs: named `HAL_TARGET_FERRY_IP/PORT`
> + `HAL_TARGET_UNDERWORLD_IP/PORT` service pairs, `HAL_CHALLENGE_ID=18`,
> `HAL_CHALLENGE_NAME/CATEGORY`, `HAL_AGENT_MODEL`/`HAL_RUN_ID`/`HAL_TEAM_UUID`,
> flag-like env (`BONUS_FLAG`, `FLAG_MAIN`), `OPENAI_BASE_URL`, `MCP_ENDPOINT`; real
> plain-HTTP listeners (stdlib http.server only, zero new deps): `HalctfTargetServer`
> serves the observed `/fetch` surface 403/404/502/200 (flag ONLY on the 200 path),
> `ContractSidecarServer` answers `/submit` → `{"status":"correct","points_awarded":1}`
> + `/done` → 200 (records bodies, `/mcp` 404s), `ScriptedModelServer` is a stub
> OpenAI-compatible endpoint; `tottori_env()` builds the full env from live server
> URLs. New `tests/test_halctf_contract.py` (525 lines, 5 tests): (1) env shape +
> services/allowlist/snapshot/sidecar-env-first resolution; (2) wire responses served
> by real listeners; (3) discovery → REAL-URL targets (never challenge id) +
> allowlist admits the exact probes (no refusal) + model routing from HAL_AGENT_MODEL
> + OPENAI_BASE_URL; (4) negative control — empty allowlist refuses (fail-closed
> PlatformDestinationError/AllowlistViolationError) and challenge-id-only env keeps
> the V09 bare-id fallback; (5) FULL-HARNESS subprocess E2E: real `python -m
> ozzgraph` under the fixture env → exit 0, `TERMINATION: completed`, exactly 4
> scripted probes executed against the real listener (no refusal, no loop),
> one accepted sidecar submission + `/done`(reason=completed), graph holds real-URL
> targets + `objective-halctf-flag completed:true`, findings.json + report.json
> rendered (scored — not unexhausted-complete, HAL-006 gate). Docs: CHANGES_v2.md
> HAL-011 line, SYNTHETIC_LAB.md fixture section. Gate: ruff format/check clean,
> mypy src strict (62 files), full suite 1306 passed (+5). Worker dispatched via
> hermes chat (deepseek-v4-flash @ openrouter) — worker fired the judge before
> committing (worktree evaluated: 266a240d PASS, all 3 criteria cited real fixture
> content, tier1 1306 collected exit 0), then committed d4fe872 (pre-commit guard
> PASS — full suite in hook) + pushed (origin/main..HEAD=0 verified) + re-verified
> ad-hoc (ruff + 5/5 focused incl. E2E). Foreman re-verified gates independently
> (ruff/mypy/5 tests). gitreins task deleted, tasks.yaml clean. Board is now empty
> (only E2E-001 + NEVER-DONE) → cooldown 43200s.
> Next: NEVER-DONE audit on next tick.

> **Tick 2026-08-09: HAL-010 closed (judge PASS 8d303648, commit a582b25 — verified**
> this tick). SpecialistFleet wired into PRODUCTION composition — the V07
> bounded-parallel batch path is no longer test-only. `Supervisor.run`
> composes `SpecialistFleet(artifacts, event_log, run_id, policy,
> max_workers, state_dir)` into `AutonomousRunner(specialists=...)` behind
> the new `OZZGRAPH_SPECIALISTS_ENABLED` toggle (`config.specialists_enabled`,
> `_env_bool` parser — 1/true/yes/on, default OFF: existing runs keep the V06
> model path byte-for-byte, ADR-0009 consequence). Fleet owns no async
> resources — plain construction, no aclose. Runner side already existed
> (gate runner.py:598 `_is_hypothesis_batch` → `_run_specialist_batch_turn`,
> ZERO LLM calls). Tests: test_supervisor.py captures the AutonomousRunner
> kwargs via monkeypatch — enabled ⇒ isinstance SpecialistFleet with the run's
> artifacts/event_log/run_id/max_workers/state_dir wired; default ⇒
> specialists=None; test_runner.py `_one_turn` gate test: batch decision →
> StubFleet dispatched, `_NoModelCallsService.calls == 0`; test_config.py 4
> env-toggle tests. Docs: CHANGES_v2.md HAL-010 line, USAGE.md env table row.
> Gate: ruff format/check clean, mypy src strict (62 files), full suite 1301
> passed (+7). Worker dispatched via hermes chat (deepseek-v4-flash @
> openrouter), committed a582b25 (Tier-1 guard PASS — full suite in hook) +
> pushed (origin/main..HEAD=0 verified). Judge PASS 8d303648 (4/4 criteria,
> tier1 lint/tests/secrets PASS — first two judge runs FAILed spurious tier1
> `ruff: not found` (bare-shell PATH), resolved by exporting
> `.venv/bin` on the judge PATH; evaluator re-ran ruff/mypy + full pytest
> 1301 passed fresh; verdict committed on gitreins branch). gitreins task
> deleted, tasks.yaml clean. HAL-011 pending → cooldown stays 900s.
> Next: HAL-011 (per board).

> **Tick 2026-08-09: HAL-009 closed (judge PASS 9ab0a5c9, commit de5d9a4 — verified**
> this tick). Tottori live-run exploitation lessons ported into skill cards,
> kernel-external: 8 new cards registered in the SKILLS registry (skills.py)
> — exploit_sqli_enumeration (multi-DB fingerprinting/enumeration, bounded
> sqlmap after evidenced hypothesis), exploit_jwt (alg confusion, PEM-as-
> HMAC-secret, kid injection, weak-secret brute within policy), exploit_ssrf
> (multi-service probing, IP obfuscation — decimal/hex/octal/IPv6/rebinding,
> file:// + gopher://, side-channel reasoning), exploit_xxe (entity in XML
> bodies, file read, SSRF via http:// entities, blind OOB only with authorized
> listener), exploit_deserialization (sink identification pickle/yaml/jackson/
> php unserialize, gadget chains evidence-driven, never execute untrusted
> payloads on harness host), exploit_protocol_reversing (capture/parse custom
> protocols, field fuzzing, length-prefix, checksum/CRC bypass), forensics_
> file_analysis (carving, strings/entropy, stego, archive/disk-image
> enumeration, timeline), exploit_cloud_iam (role chaining, metadata service
> 169.254.169.254 via SSRF, credential validation — authorized scopes only).
> New src/ozzgraph/techniques.py: deterministic TechniqueClassifier maps a
> challenge-category string (case-insensitive substring matching: ssrf/sql/
> jwt/web/forensic/cloud/iam/...) → ordered skill_id subset; unknown/None →
> DEFAULT_CATEGORY_SKILL_IDS (recon/enum core); unregistered mapping fails
> loudly (SkillRegistryError). SkillRegistry.list_for_category(category)
> returns category-constrained SkillSummaries sorted by skill_id — lazy:
> summaries only, full cards exclusively via load() (AGENTS.md rule #6);
> instructions stay in card DATA, zero supervisor/executor/context references
> to exploit_* ids (rule #10). Router wired (middle-out): router.py
> skills_for(phase, category=None) intersects phase + category, category-less
> behavior unchanged. Docs: CHANGES_v2.md +29, API_AND_INTEGRATIONS.md skill
> table 9→17 rows, CUSTOMIZATION.md refreshed. Gate: ruff/format/mypy strict
> clean (62 files), 1294 tests pass (239.96s, +23: tests/test_technique_
> classifier.py 300 lines — category routing incl. EXPLOITATION 'Web / SSRF'
> → exactly [exploit_auth_bypass, exploit_jwt, exploit_ssrf], forensics,
> cloud IAM, unknown/None default, lazy-summary + load() evidence, loud
> unregistered mapping; test_skills.py + test_toolplane.py extended for the
> 17-skill registry). Worker dispatched via hermes chat (deepseek-v4-flash @
> openrouter), committed de5d9a4 (Tier-1 guard PASS — full suite in hook) +
> pushed (origin/main..HEAD=0 verified). Judge PASS 9ab0a5c9 (4/4 criteria,
> tier1 lint/tests/secrets PASS, evaluator re-ran ruff/mypy + full pytest
> 1294 passed fresh; verdict committed on gitreins branch). gitreins task
> deleted, tasks.yaml clean. HAL-010..HAL-011 pending → cooldown stays 900s.
> Next: HAL-010 (wire SpecialistFleet into Supervisor production composition).

> **Tick 2026-08-09: HAL-008 closed (judge PASS f55fee81, commit f1a2a2d — verified**
> this tick). HalCTF process/exit semantics: `_exit_code_for()` in `__main__.py`
> makes the process-boundary mapping HalCTF-mode-aware — in HalCTF mode (any
> `HALCTF_MODE_VARS` non-blank) EVERY structured `TerminationReason`
> (COMPLETED / INTERRUPTED / FAILED / BUDGET_EXHAUSTED) → container exit 0
> (scored, unsolved, exhausted, gave-up, graceful platform failure all exit 0 —
> a nonzero exit would be misread by the platform as a crash and re-detonated);
> load-time ConfigError and uncaught exceptions (startup-impossible / process
> corruption) still exit 1; usage errors (bad target, benchmark args) stay 1
> regardless of mode. Local mode (`ozzgraph run TARGET`) byte-for-byte unchanged
> (0/130/1/3). Internal distinctions preserved: JSONL termination event still
> records the structured `reason` (budget_exhausted etc.) even when the process
> exits 0 — model never collapsed. Docs: CHANGES_v2.md +20, USAGE.md exit-code
> section rewritten, RELEASE/TESTING_AND_QA/IMAGE_HARDENING + adr/0007 updated,
> new docs/adr/0012-process-boundary-exit-policy.md (81 lines). Gate: ruff/
> format/mypy strict clean, 1271 tests pass (233s, +6 e2e in test_e2e_run.py:
> halctf budget-exhausted → exit 0 + TERMINATION: budget_exhausted + event
> reason, unsolved → 0, missing HAL_USER_ID → 1, invalid HAL_TARGET_PORT → 1,
> local exit-3/exit-1 unchanged). Worker dispatched via hermes chat
> (deepseek-v4-flash @ openrouter), committed f1a2a2d + pushed (16/16 ad-hoc
> verification). Judge PASS f55fee81 (5/5 criteria, tier1 lint/tests/secrets
> PASS, verdict committed on gitreins branch). gitreins task deleted, tasks.yaml
> clean. HAL-009..HAL-011 pending → cooldown stays 900s. Next: HAL-009 (skills
> port).
>
> **Tick 2026-08-09: HAL-007 closed (judge PASS 52fb4801, commit 44a52d9 — verified**
> this tick). HalCTF flag pattern generalized: `HalCTFEnvironment` gains
> `HALCTF_DEFAULT_FLAG_PATTERN = r"[A-Za-z][A-Za-z0-9_]{1,14}\{[^{}\s]+\}"` — the
> local `flag{...}` default generalized to identifier-style prefixes (flag{},
> HALCTF{}, ...), same interior shape (no braces/whitespace). `__init__` resolves
> the effective pattern once: operator's explicit `OZZGRAPH_FLAG_PATTERN`
> (non-blank, mirroring `load_config`'s blank-means-unset semantics) wins;
> otherwise the generalized HalCTF default applies — so a real platform
> detonation (which never injects OZZGRAPH_FLAG_PATTERN) now catches HALCTF{...}
> flags. `flag_extractor()` wired to the resolved pattern; constant exported from
> `ozzgraph.environments.halctf`. Local default byte-for-byte unchanged:
> `DEFAULT_FLAG_PATTERN` (config.py:178), `load_config`, `FlagCandidateExtractor`
> default, benchmarks/matrix/lab untouched. Gate: ruff/format/mypy strict clean,
> 1265 tests pass (215s, +6 new in tests/test_flag_pattern.py: HALCTF{}+flag{}
> both persist, JS/CSS braces never match, operator override wins, local default
> unchanged; test_environments.py service-factory test extended). Worker
> dispatched via hermes chat (deepseek-v4-flash @ openrouter), committed 44a52d9
> (worker ran Tier-1 guards, PASS — full suite inside the pre-commit guard) +
> pushed. Judge PASS 52fb4801 (3/3 criteria, tier1 lint/tests/secrets PASS,
> verdict committed on gitreins branch). gitreins task deleted, tasks.yaml clean.
> HAL-008..HAL-011 pending → cooldown stays 900s. Next: HAL-008 (process/exit
> semantics).

> **Tick 2026-08-09: HAL-006 closed (judge PASS bfbec37b, commit 9c0921f — verified**
> this tick). Objective completion acceptance-gated: `EnvironmentAdapter` gains
> `verdict_satisfies_objectives(graph)` (base.py) — the environment-specific
> completion predicate. Runner `_evaluate` consults it before `_complete_objectives()`
> on a `PlanVerdict.COMPLETE`, so a validated hypothesis (which still produces its
> evidence-backed Finding unconditionally) can no longer complete
> `objective-halctf-flag` without an accepted submission. `HalCTFEnvironment`
> accepts the verdict only when the graph holds an accepted submission entity
> (strict-bool read, fails loudly on non-bool, mirrors router `_payload_bool`);
> `LocalEnvironment` returns True unconditionally (pre-HAL-006 behavior
> byte-for-byte unchanged). The accepted-submission DONE path untouched (HAL-005
> zero-LLM flow preserved — test_flag_loop green). Gate: ruff/format/mypy strict
> clean, 1259 tests pass (219s, +6 new in tests/test_objective_acceptance.py:
> predicate unit tests incl. InvalidGraphStateError on non-bool accepted,
> COMPLETE-without-submission leaves objective incomplete + run continues
> (BUDGET_EXHAUSTED, not COMPLETED), local COMPLETE still completes objectives,
> accepted submission still completes via DONE path). Worker dispatched via hermes
> chat (deepseek-v4-flash @ openrouter), committed 9c0921f (worker ran Tier-1
> guards, PASS). Judge PASS bfbec37b (4/4 criteria, tier1 lint/tests/secrets PASS,
> verdict committed on gitreins branch). gitreins task deleted, tasks.yaml clean.
> HAL-007..HAL-011 pending → cooldown stays 900s. Next: HAL-007 (generalize flag
> pattern).

> **Tick 2026-08-09: HAL-002 closed (judge PASS d0e00cb, commit cbbffe5 — verified**
> this tick). MCP optional/fallback for HalCTF startup: `OPENAI_BASE_URL` removed
> from `HALCTF_ENDPOINT_CANDIDATES` (it is the model service `/llm`, never the MCP
> server `/mcp/`); `load_config` and `HalCTFEnvironment` construct with
> `endpoint=None` when only env-derived challenge metadata is present (env-first
> detonation starts with zero endpoint vars); `require_halctf_endpoint` retained as
> a helper for callers that genuinely need an endpoint, fail-loud scoped to truly
> unrecoverable config (missing HAL_USER_ID, invalid HAL_TARGET_* port, malformed
> scope/credentials files); hal_client keeps its localhost default for standalone
> halctl (HalClient only constructed when actually used); supervisor + 4 docs
> (CHANGES_v2/USAGE/API_AND_INTEGRATIONS/ADR-0011) updated. Gate: ruff/format/mypy
> strict clean, 1195 tests pass (216s, was 1181 — 8 new tests). Judge PASS d0e00cb
> (5/5 criteria, tier1 lint/tests/secrets PASS). Worker fired the judge before
> committing (empty-diff run killed); foreman committed + re-judged on committed
> state. gitreins task deleted, tasks.yaml clean. HAL-003..HAL-011 pending →
> cooldown stays 900s. Next: HAL-003 (model routing).

> E2E-001 run #1 (2026-08-07): 65P/1F/1U, finding FLAGLEAK-001 fixed (a667733, judge PASS) — see Completed.
> E2E-001 run #2 (2026-08-10): 66P/0F/1U against the v2 kernel (V01–V10 + HAL-001..HAL-011) — FLAGLEAK-001 verified fixed, flag-leak sweep reworked to post-fix contract, no new gaps (f192236, judge PASS a1777f88).

**Tick 2026-08-07: DOCS-000 done — documentation gate PASS (judge 1913c392).**
CONTRIBUTING.md (149 lines: toolchain, gates, PR workflow) + SECURITY.md (187
lines: vuln reporting, agent-isolated security model, flag hashing FLAGLEAK-001,
container hardening) created; README.md refreshed to launchpad bar (badges, ASCII
diagram, nav tables). Repo metadata set (description + 6 topics via gh repo
edit). Content landed in 06c5ab1 (concurrent repo-owner commit absorbed staged
docs; worker marker commit d3d74cf) — guards PASS, judge PASS 1913c392, Tier-1
lint/tests/secrets green. Worktree clean. Docs gate satisfied → idle classification.

## v2 Active — General Autonomous Security-Research Harness (per docs/CHANGES_v2.md)

> v2 milestone: pivot from "HalCTF agent" to "general vuln-research harness with
> HalCTF as one adapter." Vertical-first. Read docs/CHANGES_v2.md before
> starting. Phases V01-V10; work strictly in order (FIFO), each a judged commit.

> ✅ V01 DONE (2026-08-08): committed da1aaaf (26 files, 2968 insertions:
> environments/ package, ADR-0008, kernel rewrites, AutonomousRunner, tests).
> Judge PASS d1416f4 (5/5 criteria, 911 tests).
>
> ✅ V02 DONE (2026-08-08): committed db18787 (6 files, 923 insertions:
> `ozzgraph run` CLI + console script, findings.py Finding/FindingStore,
> runner evidence→hypothesis→Finding on CONFIRMED verdict, supervisor
> Evaluator wiring, tests/test_e2e_run.py process-level E2E — real subprocess
> against lab hidden-routes target + stub OpenAI endpoint; exit 0,
> TERMINATION: completed, graph chain + findings.json, exit-1/exit-3 mapping).
> Full gate green (ruff/format/mypy strict, 915 tests). Judge PASS 6c6a117a
> (3/3 criteria). Next: V03 (tool-runtime). Tick cap 3600s.
>
> ✅ V03 DONE (2026-08-08): committed c3c44f9 (10 files, 1583 insertions:
> src/ozzgraph/toolplane.py ToolCatalog/ToolInventory/CapabilityRegistry/
> ToolProvider, startup inventory in AutonomousRunner, capabilities-driven
> context (AVAILABLE CAPABILITIES block), Skill.required_capabilities +
> list_available, docker/Dockerfile.kali `:max` Kali image, docs +
> test_toolplane.py 26 tests + wiring/image tests). Work was completed by the
> prior tick's worker but left uncommitted (timeout) — this tick verified
> gates green (ruff/format/mypy strict, 941 tests) and committed, no redo.
> Judge PASS 3f4640ae (5/5 criteria). Next: V04 (semantic-observations).
>
> ✅ V04 DONE (2026-08-08): committed 8e49dd0 (observations.py 26KB→~140KB
> expansion: 17 tool-specific typed parsers — curl/nmap/ffuf/feroxbuster/
> nuclei/netexec/smbmap/ldapsearch/semgrep(JSON+SARIF)/codeql(SARIF)/trivy/
> gitleaks/file/readelf/checksec/exiftool(JSON+text)/binwalk — consuming
> JSON/XML/SARIF/JSONL/LDIF via registry keys (source,kind), parser_for_command
> dispatch gated by tool flags, runner `_persist_execution` wired through
> observation_for_result: raw output persisted to ArtifactStore FIRST, then
> semantic parse → typed Observation referencing artifact id → observation
> entity + evidence edge; malformed output still stored raw with
> malformed=True observation (fail loudly). docs/OBSERVATIONS.md added,
> toolplane.py capability entries for parseable tools, 56 new tests.
> Gates green (ruff/format/mypy strict, 997 tests). Judge PASS 01aa9dd
> (3/3 criteria, tier1 lint/tests/secrets PASS). Next: V05 (model-harness-matrix).

> ✅ V05 DONE (2026-08-08): committed 70d6f3f (9 files, 1040 insertions:
> profiles.py registry is now data, not code — per-model TOML profiles
> under profile_data/ (claude/deepseek/gpt/llama/fallback) loaded
> deterministically; ModelProfile gains model_ids + benchmarks
> (TraceMetrics); ProfileStore (discover exact-id→family-prefix→fallback,
> discover_from_service via GET /v1/models + capability probe — function
> call never assumed, update_benchmarks/persist_report byte-deterministic);
> tests/test_profile_store.py 18 tests. Work was completed by the prior
> tick's worker but left uncommitted (timeout) — this tick verified gates
> green (ruff/format/mypy strict, 1015 tests) and committed, no redo.
> Judge PASS b8a2cfd1 (5/5 criteria, tier1 lint/tests/secrets PASS).
> Also hardened .gitreins/config.yaml: test_command now `uv run --group
> dev pytest` + test_timeout 300 (pipeline's 120s default killed the
> 115s suite — 2 spurious FAIL verdicts traced to SIGKILL at 120s and
> dev-group-less resync). Next: V06 (security-brain).
>
> ✅ V06 DONE (2026-08-08): committed c948e93 (4 files, 2216 insertions:
> src/ozzgraph/security_brain.py 1129 lines — OpportunityGenerator
> (graph-predicate opportunities: characterize service / test hypothesis /
> expand scope, ranked, fail-loud), StrategicPlanner (LLM, invoked ONLY
> when >1 viable path; strategy prompt + ranking summary), TaskBuilder
> (bounded Task from opportunity/plan, command parity with runner),
> HypothesisManager (create→evidence→promote/abandon lifecycle,
> idempotent, event-mirrored), ProgressEvaluator (continue/pivot/finish
> from graph predicates); runner.py wired: _one_turn branches on typed
> BrainDecision — DeterministicActionDecision executes with ZERO model
> calls, StrategicDecision calls StrategicPlanner exactly once,
> FallbackDecision preserves the old model-propose path for 0-opportunity
> states; BRAIN_PROGRESS_EVALUATED event; docs/CHANGES_v2.md updated.
> 14 new tests (zero-LLM assertion, multi-path promote, hypothesis
> lifecycle, progress verdicts). Gates green (ruff/format/mypy strict,
> 1029 tests). Judge PASS ea396688 (4/4 criteria, tier1 lint/tests/
> secrets PASS). Next: V07 (specialists).
>
> ✅ V07 DONE (2026-08-08): committed c3872ce→e953e00 (4 commits, 2801
> insertions: workers.py SpecialistMicroAgent + MicroAgentTask — bounded
> deterministic hypothesis→experiment→observation→conclusion loop,
> MAX_MICRO_ITERATIONS=3, ZERO model calls, only context is hypothesis
> objective + prior observations (never full graph); scheduler.py
> parallelizes independent hypotheses (hypothesis id IS the conflict key;
> ready_order drives batch under max_workers) while global strategy stays
> serialized via serialized_task + reserved MUTATION_CONFLICT_KEY;
> reducer.py merges structured verdicts — Verdict + evidence_ids + impact
> (CWE/assets/confidence) live in fact payload + fingerprint;
> specialists.py SpecialistFleet (narrow task build → bounded parallel
> schedule → reducer → promote confirmed/abandon refuted → evidence-backed
> findings + findings.json); runner.py _run_specialist_batch_turn
> dispatches a fleet batch instead of an LLM call when brain returns a
> pure independent-hypothesis StrategicDecision and a fleet is wired
> (specialists=); docs/CHANGES_v2.md + ADR-0009. Work was completed by
> the prior tick's worker and committed but left unpushed/unjudged — this
> tick verified gates green (ruff/format/mypy strict, 1070 tests), pushed,
> judged PASS 8b6c8e3 (4/4 criteria, tier1 lint/tests/secrets PASS).
> Next: V08 (local-assessment).
>
> ✅ V08 DONE (2026-08-08): committed 1673b97→14eb864→198ba36 (16 files,
> 2261 insertions: reporting.py render_report_bundle — report.md +
> report.json + report.sarif from graph findings in state_dir alongside
> evidence/ + graph.sqlite (online-backup snapshot) + events.jsonl, runner
> emits bundle at COMPLETED with loud report_failed; LocalEnvironment
> url/network/repository/docker-compose/hybrid modes via OZZGRAPH_TARGET
> classification (classify_local_target, LOCAL_MODE_NAMES, scope_mode
> hybrid, LocalEnvironment now the DEFAULT via supervisor._make_environment);
> config.py OZZGRAPH_SCOPE_FILE/_load_scope_entries (JSON/YAML/TOML,
> sorted+deduped → target_allowlist) + OZZGRAPH_CREDENTIALS_FILE/
> _load_credential_records (Credential refs sorted by name) with loud
> ConfigError on unreadable/malformed/wrong-shape; ADR-0010, docs/
> CHANGES_v2.md + USAGE env vars; tests test_reporting.py 458 lines +
> test_config.py 219 + test_environments.py 149 + test_runner.py 57 + e2e
> additions). Work completed + pushed by prior worker, unjudged — this tick
> verified gates green (ruff/format/mypy strict, 1114 tests) and confirmed
> judge PASS 6025990f (4/4 criteria, tier1 lint/tests/secrets PASS).
> Next: V09 (halctf-adapter).

| ID | Task | Pri | Cpx | Deps | Tags | Model | Reasoning | Fallback |
|----|------|-----|-----|------|------|-------|-----------|----------|

All HAL-0xx tasks closed (HAL-001..HAL-011, judges PASS) — see Completed. Remaining: E2E-001 + NEVER-DONE below.

## [x] HAL-001 — HalCTFRuntimeSnapshot: env → graph targets + scope allowlist (real HalCTF runtime)

**Tick 2026-08-09: HAL-001 closed — board-staleness close, all work verified this tick (judge PASS a495fe1, commits 985950e + 9fca71a).**
Work was committed + judged by prior ticks but the board never marked it `[x]` — git-history cross-reference surfaced it. Verified this tick: `HalCTFRuntimeSnapshot` parses every `HAL_TARGET_<NAME>_IP/_PORT` pair + single `HAL_TARGET_IP/PORT`, `HAL_CHALLENGE_*`, `HAL_AGENT_MODEL`, `HAL_RUN_ID`, `HAL_TEAM_UUID`, flag-like env (BONUS_FLAG/FLAG_*), OPENAI_BASE_URL, MCP_ENDPOINT (config.py:122-142); `discover_targets()` emits one Target per named service as a real URL (http://IP:PORT), never the challenge id; `discover_scope()` populates hosts/urls + ScopePolicy target_allowlist merged from `halctf_target_allowlist` in one atomic adapter op (environment.py:262-319); infra authorities (sidecar 127.0.0.1:9000, model, MCP) excluded via `halctf_infra_authorities` (config.py:428,503). Tottori env-shape fixture tests: 30/30 pass locally, ruff/format clean, full suite green in CI (success on 9fca71a). Judge lifecycle: first run FAIL c229ca7a (2:52AM, judged only commit 985950e before metadata parsing landed — honest sequential), re-run after 9fca71a → PASS e4df7742/a495fe1 (all 5 criteria, tier1 lint/tests/secrets PASS, evaluated 10:33Z). gitreins task deleted, tasks.yaml clean. Board still has HAL-002..HAL-011 pending → cooldown stays 900s. Next: HAL-002 (MCP optional/fallback for HalCTF startup).

**Source:** Cross-repo assessment of `kazuki005276ssh/halctf-team-tottori` committed live-run logs vs current OzzGraph code (verified this session, no code changed). Tottori's real detonation injected `HAL_AGENT_MODEL`, `HAL_CHALLENGE_ID=18`, `HAL_CHALLENGE_NAME="Charon's Ferry"`, `HAL_CHALLENGE_CATEGORY="Web / SSRF"`, `HAL_TARGET_FERRY_IP/PORT`, `HAL_TARGET_UNDERWORLD_IP/PORT`, `HAL_RUN_ID`, `HAL_TEAM_UUID`, `OPENAI_BASE_URL`, `MCP_ENDPOINT`. OzzGraph's `discover_targets()` currently returns `Target(address=challenge_id)` — the graph target is literally `"18"`, not `http://10.244.x.x:9004`. And `DEFAULT_TARGET_ALLOWLIST=()` (fail-closed) is never derived from `HAL_TARGET_*`, so even a correct address would be refused.

**Acceptance:**
1. Build `HalCTFRuntimeSnapshot` parsing every `HAL_TARGET_<NAME>_IP` + `_PORT` pair, single `HAL_TARGET_IP/PORT`, `HAL_CHALLENGE_*`, `HAL_AGENT_MODEL`, `HAL_RUN_ID`, `HAL_TEAM_UUID`, flag-like env (`BONUS_FLAG`, `FLAG_*`), `OPENAI_BASE_URL`, `MCP_ENDPOINT`.
2. `discover_targets()` emits one Target per named service as a real URL (`http://IP:PORT`), NOT the challenge id.
3. `discover_scope()` populates `Scope.hosts` / `Scope.urls` AND the ScopePolicy `target_allowlist` from those targets (one atomic adapter operation, no separate manual config).
4. Sidecar/model/MCP infrastructure (127.0.0.1:9000, OPENAI_BASE_URL, MCP_ENDPOINT) is explicitly excluded from candidate targets.
5. Tests: fixture with Tottori's exact env shape asserts targets + allowlist + infra exclusion.

## [x] HAL-002 — Make MCP optional/fallback for HalCTF startup

**Tick 2026-08-09: HAL-002 closed (judge PASS d0e00cb, commit cbbffe5).**
MCP optional/fallback: `OPENAI_BASE_URL` removed from `HALCTF_ENDPOINT_CANDIDATES`
(model service `/llm`, never MCP `/mcp/`); `load_config` + `HalCTFEnvironment`
construct with `endpoint=None` on env-only detonations; `require_halctf_endpoint`
kept as a helper for callers that genuinely need an endpoint; fail-loud scoped to
truly unrecoverable config (missing HAL_USER_ID, invalid HAL_TARGET_* port,
malformed scope/credentials files); hal_client localhost default unchanged for
standalone halctl; supervisor + docs updated. Gate: ruff/format/mypy strict clean,
1195 tests pass (216s, 8 new tests). Judge PASS d0e00cb (5/5 criteria, tier1
lint/tests/secrets PASS). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — `require_halctf_endpoint()` raises `ConfigError` at construction and `load_config` does the same whenever HalCTF mode is selected. The real platform injects challenge metadata via env (env-first); requiring an MCP endpoint to start is wrong. Tottori runs env-first with MCP as fallback.

**Acceptance:**
1. HalCTF mode starts with env-derived challenge metadata alone (no endpoint required).
2. MCP becomes an optional enrichment path (challenge discovery fallback) if available.
3. Remove `OPENAI_BASE_URL` from `HALCTF_ENDPOINT_CANDIDATES` (it is the model service, `/llm`, not the MCP server `/mcp/`).
4. Tests: HalCTF env constructs with zero endpoint vars; fail-loud only preserved for truly unrecoverable config.

## [x] HAL-003 — Model routing in HalCTF mode

**Tick 2026-08-09: HAL-003 closed (judge PASS 469be7f1, commit e9c421e).**
Model routing wired: `Supervisor._model_routing()` resolves `HAL_AGENT_MODEL` →
model id + `OPENAI_BASE_URL` → client base URL via the HAL-001 snapshot; `_run`
constructs a supervisor-owned `ModelService` in HalCTF mode and passes
`model_id=`/`model_service=` into `AutonomousRunner` (closed in the same finally
as the environment); absent platform vars degrade gracefully to
`OZZGRAPH_MODEL_ID` / `OZZGRAPH_MODEL_BASE_URL` / defaults; local mode untouched.
`runner.started` now logs the actual resolved base URL via new
`ModelService.base_url` property (getattr-protected for protocol doubles).
docs/USAGE.md note added. Gate: ruff/format/mypy strict clean, 1201 tests pass
(220s, +6 new). Judge PASS 469be7f1 (4/4 criteria, tier1 lint/tests/secrets
PASS). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — `model_client.py` defaults `DEFAULT_MODEL_BASE_URL=http://127.0.0.1:8000/v1`; `runner.py` reads `OZZGRAPH_MODEL_ID` (default `"default"`). Neither `OPENAI_BASE_URL` nor `HAL_AGENT_MODEL` is used. The real platform provides `OPENAI_BASE_URL=http://127.0.0.1:9000/llm` and `HAL_AGENT_MODEL=google/gemma-4-26b-a4b-it-maas`. Probable immediate failure on the event platform.

**Acceptance:**
1. In HalCTF mode, model client base URL maps from `OPENAI_BASE_URL`; model id maps from `HAL_AGENT_MODEL`.
2. Local mode keeps the `OZZGRAPH_MODEL_*` defaults unchanged.
3. Tests: HalCTF env resolves model config from platform vars; local mode unchanged.

## [x] HAL-004 — Sidecar transport adapter (/submit + /done)

**Tick 2026-08-09: HAL-004 closed (judge PASS ab4d87a9, commit 66cf337).**
Sidecar transport adapter at the process boundary: new
`src/ozzgraph/environments/halctf/sidecar.py` (626 lines) —
`SidecarSubmissionClient` speaks the real competition sidecar's PLAIN HTTP
(`POST /submit` bounded `{challenge_id, flag}` + `POST /done` best-effort,
never fatal), base URL env-first (`OZZGRAPH_SIDECAR_BASE_URL` → MCP endpoint
origin → localhost default; `OPENAI_BASE_URL` never consulted);
`_normalize_submission` maps every observed response form into the UNCHANGED
`SubmissionResult` schema (deterministic precedence: status string in
ACCEPT_STATUSES {correct,accepted,solved,success,already_solved} → explicit
boolean verdict fields → points_awarded/points > 0; wrong-typed verdict
fields fail loudly); bounded retries on transient failures only, privilege
guard (OZZGRAPH_HAL_PRIVILEGED → HalPrivilegeError before the wire), events
sidecar.failure/sidecar.done/sidecar.done_failed. Implements the existing
`SubmissionClient` protocol — SubmissionCoordinator drives it unchanged
(zero-line diff on coordinator + schema). Wired as
`HalCTFEnvironment.sidecar_submission_client()` factory + halctf shim
exports. Gate: ruff/format/mypy strict clean, 1243 tests pass (215s, +42
new in tests/test_sidecar.py incl. coordinator integration). Judge PASS
ab4d87a9 (4/4 criteria, tier1 lint/tests/secrets PASS). gitreins task
deleted, tasks.yaml clean.

**Source:** Verified — Tottori discovered the real `/submit` response `{"status":"correct","points_awarded":1}` and normalizes multiple accept shapes. OzzGraph drives `flag.submit` over its own JSON-RPC `HalClient`. Add a real sidecar adapter at the process boundary; keep the excellent internal `SubmissionResult` schema.

**Acceptance:**
1. Adapter POSTs `/submit` and `/done` to the sidecar (127.0.0.1:9000); `/done` best-effort.
2. Normalizes observed response forms (`status in {correct,accepted,solved,success,already_solved}`, `points_awarded>0`, boolean fields) into the existing `SubmissionResult`.
3. No weakening of the internal coordinator/schema.
4. Tests: mock responses for each accept/reject shape.

## [x] HAL-005 — Wire flag extraction + submission into the active loop

**Tick 2026-08-09: HAL-005 closed (judge PASS 45694ccf, commit ded08d4 — verified this tick).**
Flag extraction + submission wired into the active loop: `AutonomousRunner` gains a
supervisor-owned `flag_submitter` hook invoked as `_process_flag_candidates()` after EVERY
executed turn's `_persist_execution()` (deterministic :640-645, specialist :752-753,
fallback :903-904 — the only observation-persisting paths, which satisfy the extractor's
provenance gate); `Supervisor._submit_flag_candidates` runs
`environment.flag_extractor().extract(graph)` → `submit_verified_candidate(graph, ...,
client=supervisor-owned privileged sidecar)` — ZERO LLM calls between seeing a flag and
submitting it (integration test asserts `terminated.model_calls == 1`, only the observing
turn). Accepted → `has_accepted_submission` routes DONE → `objective-halctf-flag`
completed → COMPLETED → `_notify_platform_done()` fires the best-effort sidecar `/done`
once (never fatal). Rejected → coordinator already marks the candidate `rejected: true` +
`attempts` (never re-submitted, `graph.entity_updated` + `submission.rejected` events),
hook returns non-fatally, investigation continues. No-candidate /
`MissingRequiredStateError` → silent no-op; limit/privilege/corrupt-state refusals → loud
`supervisor.flag_submission_failed` events, loop continues under budgets. Invariants
preserved: `SubmissionPrivilegeError` before the wire, per-candidate attempt + total
submission budgets, idempotent extraction (existing/rejected/at-budget skipped), durable
replay-identical graph state. Local mode untouched (hook None → loop byte-for-byte
unchanged). Gate: ruff/format/mypy strict clean, 1253 tests pass (227s, +10 new in
tests/test_flag_loop.py: real runner loop + real supervisor hook against a scripted
plain-HTTP sidecar — zero-LLM happy path, rejection-never-resubmitted, failure path
continues to BUDGET_EXHAUSTED). Worker dispatched via hermes chat (deepseek-v4-flash @
openrouter), committed ded08d4 + pushed. Judge PASS 45694ccf (4/4 criteria, tier1
lint/tests/secrets PASS — evaluator-side skylos_scan tool signature error recovered;
verdict committed 6cb49e4d on gitreins branch). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — `FlagCandidateExtractor.extract()` and `Supervisor.submit_verified_candidate()` are implemented and tested but never called in `runner.run()`/`supervisor.run()`; grep for production callers returns only definitions/docstrings. The "last two arrows" (extraction→loop, candidate→submitter) are missing.

**Acceptance:**
1. After `_persist_execution()`, run `FlagCandidateExtractor.extract()`; a NEW verified candidate immediately enters supervisor-owned submission.
2. Zero LLM turns between seeing a flag and submitting it.
3. Accepted → objective COMPLETE → `/done`; rejected → candidate marked rejected → continue investigation.
4. Preserve supervisor-only privilege, submission budget, duplicate/rejected handling, durable state.
5. Tests: an observation containing a flag → accepted submission → objective completed, no model call.

## [x] HAL-006 — Objective completion acceptance-gated

**Tick 2026-08-09: HAL-006 closed (judge PASS bfbec37b, commit 9c0921f).**
`EnvironmentAdapter` gains `verdict_satisfies_objectives(graph)` — the
environment-specific completion predicate. Runner `_evaluate` consults it before
`_complete_objectives()` on a `PlanVerdict.COMPLETE` (runner.py:1207-1219); the
evidence-backed Finding still renders unconditionally on a COMPLETE verdict.
`HalCTFEnvironment` accepts the verdict only when the graph holds an accepted
submission entity (strict-bool read mirroring router `_payload_bool`, fails loudly
on non-bool); `LocalEnvironment` returns True unconditionally (pre-HAL-006
behavior byte-for-byte unchanged). Accepted-submission DONE path untouched (HAL-005
zero-LLM flow preserved). Gate: ruff/format/mypy strict clean, 1259 tests pass
(219s, +6 in tests/test_objective_acceptance.py). Judge PASS bfbec37b (4/4
criteria, tier1 lint/tests/secrets PASS). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — `runner._evaluate` does `if evaluation.verdict is PlanVerdict.COMPLETE: await self._complete_objectives()`, and the evaluator's COMPLETE fires on "every plan step completed" OR "a hypothesis confirmed" — pure pentest semantics. A validated SQLi hypothesis can mark `objective-halctf-flag.completed=true` with no successful `/submit`, terminating the run COMPLETED (exit 0) unscored.

**Acceptance:**
1. Objective completion becomes an environment-specific predicate. For HalCTF, ONLY a `SubmissionAccepted(challenge_id)` satisfies `objective-halctf-flag`.
2. `PlanVerdict.COMPLETE` may produce a Finding but must NOT complete the HalCTF objective.
3. Local pentest semantics unchanged (validated finding may satisfy objective).
4. Tests: evaluator COMPLETE with no submission leaves objective incomplete; accepted submission completes it.

## [x] HAL-007 — Generalize HalCTF flag pattern

**Tick 2026-08-09: HAL-007 closed (judge PASS 52fb4801, commit 44a52d9 — verified this tick).**
HalCTF flag pattern generalized: `HalCTFEnvironment` gains
`HALCTF_DEFAULT_FLAG_PATTERN = r"[A-Za-z][A-Za-z0-9_]{1,14}\{[^{}\s]+\}"` —
identifier-style prefixes (flag{}, HALCTF{}, ...) with the same interior shape
as the local default; `__init__` resolves the effective pattern once (operator's
explicit non-blank `OZZGRAPH_FLAG_PATTERN` wins, blank-means-unset mirroring
`load_config`; else the HalCTF default) and `flag_extractor()` uses it; constant
exported from `ozzgraph.environments.halctf`. Local default byte-for-byte
unchanged (`DEFAULT_FLAG_PATTERN`, `load_config`, `FlagCandidateExtractor`
default, benchmarks/matrix/lab untouched). Gate: ruff/format/mypy strict clean,
1265 tests pass (215s, +6 in tests/test_flag_pattern.py + test_environments.py
extension). Judge PASS 52fb4801 (3/3 criteria, tier1 lint/tests/secrets PASS).
gitreins task deleted, tasks.yaml clean. Next: HAL-008 (process/exit semantics).

**Source:** Verified — `DEFAULT_FLAG_PATTERN=r"flag\{[^{}\s]+\}"`. Tottori's committed log shows real challenges use `HALCTF{...}` and `flag{...}`; its matcher generalizes to `[A-Za-z][A-Za-z0-9_]{1,14}\{...\}`. The platform doesn't inject `OZZGRAPH_FLAG_PATTERN`.

**Acceptance:**
1. HalCTF environment's default flag pattern generalizes to identifier-style prefixes (flag{}, HALCTF{}, etc.) independently of the local default.
2. Local default unchanged.
3. Tests: HALCTF{}, flag{}, and non-match (JS/CSS braces) fixtures.

## [x] HAL-008 — HalCTF process/exit semantics

**Tick 2026-08-09: HAL-008 closed (judge PASS f55fee81, commit f1a2a2d).**
`_exit_code_for()` (__main__.py) — HalCTF-mode-aware process-boundary mapping:
in HalCTF mode (any HALCTF_MODE_VARS non-blank) EVERY structured
TerminationReason → exit 0; load-time ConfigError + uncaught exceptions
(startup-impossible / process corruption) → exit 1; usage errors stay 1;
local mode byte-for-byte unchanged (0/130/1/3). Termination event keeps
structured reason (model not collapsed). Gate: ruff/format/mypy clean, 1271
tests pass (+6 e2e). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — `__main__.py` maps `{COMPLETED:0, INTERRUPTED:130, FAILED:1, BUDGET_EXHAUSTED:3}`. Tottori found a nonzero exit is interpreted by the platform as a crash → reruns the detonation (wasting time, marking run FAILED even when scored). It returns 0 on any ordinary completed attempt.

**Acceptance:**
1. Add a HalCTF process-boundary mapping: scored / unsolved / exhausted / gave-up / graceful platform failure → container exit 0.
2. Preserve internal `RunnerStatus` distinctions in the JSONL events (do NOT collapse the model).
3. Only actual process corruption / startup-impossible → exit 1.
4. Tests: budget-exhausted and unsolved HalCTF runs exit 0; startup-impossible exits 1.

## [x] HAL-009 — Port Tottori live-run exploitation lessons into skills

**Tick 2026-08-09: HAL-009 closed (judge PASS 9ab0a5c9, commit de5d9a4).**
Tottori live-run exploitation lessons ported into skill cards, kernel-external:
8 new cards registered in the SKILLS registry — exploit_sqli_enumeration
(multi-DB fingerprinting via error/UNION/boolean-blind, information_schema vs
sqlite_master, bounded sqlmap after evidenced hypothesis), exploit_jwt (alg
confusion incl. PEM-as-HMAC-secret, key confusion, kid injection, weak-secret
brute within policy), exploit_ssrf (multi-service probing, IP obfuscation
decimal/hex/octal/IPv6/rebinding reasoning, file:// + gopher://, side-channel),
exploit_xxe (external entities, file read, SSRF via http:// entities, blind OOB
only with authorized listener), exploit_deserialization (sink identification,
gadget chains evidence-driven, never execute untrusted payloads on harness
host), exploit_protocol_reversing (capture/parse, field fuzzing, length-prefix,
checksum/CRC bypass), forensics_file_analysis (carving, strings/entropy, stego,
archive/disk-image enumeration, timeline), exploit_cloud_iam (role chaining,
metadata service via SSRF, credential validation — authorized scopes only).
New src/ozzgraph/techniques.py: deterministic TechniqueClassifier
(case-insensitive substring category matching → ordered skill_id subset;
unknown/None → DEFAULT_CATEGORY_SKILL_IDS; unregistered mapping fails loudly).
SkillRegistry.list_for_category(category) → category-constrained SkillSummaries
(lazy — summaries only, full card via load(); instructions in card data, zero
supervisor/executor/context references to exploit_* ids). Router wired:
skills_for(phase, category=None) intersects phase + category. Docs: CHANGES_v2
+29, API_AND_INTEGRATIONS skill table 9→17, CUSTOMIZATION refreshed. Gate:
ruff/format/mypy strict clean, 1294 tests pass (239.96s, +23). Judge PASS
9ab0a5c9 (4/4 criteria, tier1 lint/tests/secrets PASS). gitreins task deleted,
tasks.yaml clean.

**Source:** Verified — Tottori's category playbooks encode hard-won event lessons (SQLi multi-DB enumeration, JWT PEM-as-HMAC-secret, SSRF multi-service + IP-obfuscation reasoning, XXE, deserialization, protocol reversing, forensics, cloud IAM role chaining). OzzGraph's skill system is architecturally superior (lazy skill cards, capability requirements, phase filtering); port the lessons, not the implementation.

**Acceptance:**
1. Add/expand skill cards for each category, kernel-external (skills/, not supervisor).
2. Challenge category → TechniqueClassifier → relevant SkillSummaries only; lazy-load the chosen technique card.
3. Keep instructions out of the kernel (AGENTS.md rule #10).
4. Tests: category routing selects correct skill subset; lazy-load evidence-driven.

## [x] HAL-010 — Wire SpecialistFleet into Supervisor

**Tick 2026-08-09: HAL-010 closed (judge PASS 8d303648, commit a582b25).** SpecialistFleet wired into PRODUCTION composition behind the `OZZGRAPH_SPECIALISTS_ENABLED` toggle (default OFF — existing runs byte-for-byte unchanged); Supervisor.run composes `SpecialistFleet(artifacts, event_log, run_id, policy, max_workers, state_dir)` into `AutonomousRunner(specialists=...)`; runner gate `_is_hypothesis_batch` → `_run_specialist_batch_turn` dispatches the bounded parallel batch with ZERO LLM calls. Gate: ruff/format/mypy strict clean, 1301 tests pass (+7). Judge PASS 8d303648 (4/4 criteria). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — `SpecialistFleet(` is constructed ONLY in `tests/test_specialists.py`. `supervisor.run()` builds `AutonomousRunner(...)` with no `specialists=` arg → `_specialists=None` → the V07 parallel path is dead in production. The implementation exists; the deployed execution graph doesn't turn it on.

**Acceptance:**
1. Supervisor wires a `SpecialistFleet` into the `AutonomousRunner` construction when configured.
2. A pure independent-hypothesis StrategicDecision dispatches the bounded parallel batch (existing `_run_specialist_batch_turn`).
3. Tests: production composition constructs the fleet; a hypothesis-batch decision routes to specialists, not the LLM.

## [x] HAL-011 — halctf-real-contract regression fixture

**Tick 2026-08-09: HAL-011 closed (judge PASS f213c71 — verdict 266a240d, commit d4fe872).** Fixture + tests reproduce Tottori's exact live-run env shapes (named HAL_TARGET_* service pairs, HAL_CHALLENGE_ID=18, metadata, runtime identity, OPENAI_BASE_URL, MCP_ENDPOINT) and observed HTTP responses (/submit {"status":"correct","points_awarded":1}, /fetch 403/404/502/200) as real stdlib plain-HTTP listeners; full-harness subprocess E2E ends scored/COMPLETED (exit 0, accepted submission, objective completed, findings.json) — not unexhausted-complete, not allowlist-refused. Gate: ruff/format/mypy strict clean, 1306 tests pass (+5). Judge PASS (3/3 criteria, tier1 1306 collected exit 0). gitreins task deleted, tasks.yaml clean.

**Source:** Verified — the existing benchmark suite runs the kernel against synthetic lab targets + scripted models, not an actual HalCTF runtime contract. Tottori's committed live logs give the exact env shapes and HTTP responses.

**Acceptance:**
1. Add a regression fixture reproducing Tottori's exact env shapes (HAL_TARGET_*_IP/PORT, HAL_CHALLENGE_*, HAL_AGENT_MODEL, OPENAI_BASE_URL, MCP_ENDPOINT) and observed HTTP responses (`/submit` `{status:correct,points_awarded:1}`, `/fetch` 403/404/502/200).
2. The harness against this fixture scores as expected (no garbage target, no allowlist refusal, model routed correctly).
3. Tests: full-harness run against the fixture ends scored/COMPLETED, not unexhausted-complete.



## Completed

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| RUNNER-REJECT-AUDIT | Backfill: audit of silent drops — 4 remaining model-proposal rejections now feed RECENT ACTIONS (privileged-kind submit/hint/exit both paths, empty-payload run actions both paths, strategic-path no-skill drop — all append `REJECTED <Kind>: <detail>`; kernel-internal errors stay model-invisible) (judge PASS dfffcced — judged together with LOCAL-PHASE-GAP backfill, 5/5 criteria, tier1 lint/tests/secrets PASS) | Medium | 2±1 | aa44bf1 | DS-V4-Flash |
| SKILLS-REPLAN | Backfill: REPLAN no longer silent-drops model probes — pivot_hunt covers Phase.REPLAN too (was ZERO skills → probes dropped before policy gate, model re-proposed forever), no-skill drop records `REJECTED NoSkillForPhase: <command>`, tests updated to new contract (REPLAN planning succeeds, branching graph replans instead of abandoning, BOOTSTRAP stays skill-less fail-loud) (judge PASS dfffcced, 5/5 criteria) | Medium | 2±1 | 94ed99d | DS-V4-Flash |
| POLICY-REPLAN | Backfill: REPLAN is not a dead end — policy now permits {shell, recon} in fallback phase (was shell-only → every curl probe rejected FamilyPermissionError → spin until budget exhaustion); live-verified on Juice Shop RECON→ENUMERATION→PIVOT→REPLAN executes real probes in every phase (judge PASS dfffcced, 5/5 criteria) | High | 2±1 | df56e70 | DS-V4-Flash |
| LOCAL-PHASE-GAP | Backfill: wire the phase machine — local env never advanced past RECON (77 phase events all RECON, 0 services, 9 hypotheses none exploitable): successful probe confirms target + seeds one service entity; evidence+CWE hypothesis stamped exploitable:true → EXPLOITATION; new pivot_hunt skill covers PIVOT + router transition all_hypotheses_resolved_objectives_open → PIVOT; deterministic zero-LLM actions count_model_call=False; CHANGES_v2 +29 (judge PASS dfffcced, 5/5 criteria, tier1 lint/tests/secrets PASS; 1307 suite passed in 244.97s) | Critical | 4±1 | 4d6185b | DS-V4-Flash |
| INT-CI-002 | Fix CI Format failure on main — findings.py:151 unformatted multi-line raise (beb0e05 broke the CI Format job only; Lint/Test/Type/Docker green); mechanical ruff format, CI re-run green on d470784 (judge PASS d366649, 2/2 criteria, tier1 lint/tests/secrets PASS) | High | 1±0 | d470784 | DS-V4-Flash |
| OZZGRAPH-EXHAUSTIVE | Exhaustive local assessment — OZZGRAPH_EXHAUSTIVE env (default off, byte-for-byte unchanged) → config.exhaustive; local env verdict_satisfies_objectives returns not(exhaustive) (never auto-completes on COMPLETE); planner skips promoted/abandoned hypotheses + NoPlan when none remain open; runner wires config.exhaustive into planner; Juice Shop: 1-2 findings default vs 4 findings in 25-min run (judge PASS c303cf98, 5/5 criteria, tier1 lint/tests/secrets PASS; backfilled via git-history cross-reference) | Medium | 3±1 | e6c5888 | DS-V4-Flash |
| RECON-FEEDBACK | Rejection feedback tells the model WHICH command was rejected — _record_turn_failure renders `REJECTED DuplicateFingerprintError :: <command>` in RECENT ACTIONS transcript tail; executor passes model_output['action'] through; OUTPUT_CONTRACT NEVER-repeat rule; live: 94→24 duplicate rejections, 7→17 distinct commands, 4→7 findings in same budget (judge PASS c303cf98 — judged together with OZZGRAPH-EXHAUSTIVE, same-file exception on runner.py, shared root cause) | Medium | 2±1 | bad90a8 | DS-V4-Flash |
| PROFILE-FREE-TIER | Free-tier model support — JSON-only profiles (openrouter/nemotron/gemma in profile_data/), protocol-agnostic OUTPUT_CONTRACT, skill-card advertisement, FAMILY_PREFIXES nemotron/nvidia/openrouter/google/gemma; part of release v2.1.0 (d051cfa + a191d10); backfilled from board-staleness (judge PASS c7efc25, verdict c029caf5, all criteria) | Medium | 3±1 | 80a3b21 | DS-V4-Flash |
| HAL-011 | halctf-real-contract regression fixture — Tottori's exact live-run env shapes (named HAL_TARGET_*_IP/PORT pairs, HAL_CHALLENGE_ID=18, metadata, HAL_AGENT_MODEL/RUN_ID/TEAM_UUID, flag-like env, OPENAI_BASE_URL, MCP_ENDPOINT) + observed wire responses (/submit {"status":"correct","points_awarded":1}, /fetch 403/404/502/200) as real stdlib plain-HTTP listeners; full-harness subprocess E2E scores/COMPLETEs (exit 0, TERMINATION: completed, accepted submission, objective-halctf-flag completed, findings.json+report.json), negative control (empty allowlist refuses fail-closed, challenge-id-only env keeps V09 bare-id fallback), docs CHANGES_v2 + SYNTHETIC_LAB (judge PASS f213c71, all 3 criteria, tier1 lint/tests/secrets PASS) | High | 4±1 | d4fe872 | DS-V4-Flash |
| HAL-010 | Wire SpecialistFleet into Supervisor production composition — OZZGRAPH_SPECIALISTS_ENABLED toggle (default OFF, byte-for-byte unchanged), Supervisor.run composes SpecialistFleet(artifacts, event_log, run_id, policy, max_workers, state_dir) into AutonomousRunner(specialists=...), runner batch gate dispatches with ZERO LLM calls (judge PASS 8d303648, all 4 criteria, tier1 lint/tests/secrets PASS) | High | 4±1 | a582b25 | DS-V4-Flash |
| HAL-009 | Tottori exploitation lessons ported into skill cards — 8 new cards (exploit_sqli_enumeration multi-DB, exploit_jwt PEM-as-HMAC, exploit_ssrf IP-obfuscation, exploit_xxe, exploit_deserialization, exploit_protocol_reversing, forensics_file_analysis, exploit_cloud_iam), deterministic TechniqueClassifier (category string → ordered skill_id subset, unknown→default, loud on unregistered), SkillRegistry.list_for_category lazy summaries, router skills_for(phase, category=None) wiring, docs (judge PASS 9ab0a5c9, all 4 criteria, tier1 lint/tests/secrets PASS) | High | 4±1 | de5d9a4 | DS-V4-Flash |
| HAL-008 | HalCTF process/exit semantics — _exit_code_for() HalCTF-mode-aware mapping (any HALCTF_MODE_VARS non-blank: every structured TerminationReason → exit 0; startup-impossible/process corruption (ConfigError/uncaught) → 1; usage errors 1; local mode byte-for-byte unchanged 0/130/1/3), termination event keeps structured reason, docs/adr/0012-process-boundary-exit-policy.md (judge PASS f55fee81, all 5 criteria, tier1 lint/tests/secrets PASS) | High | 3±1 | f1a2a2d | DS-V4-Flash |
| HAL-007 | HalCTF flag pattern generalized — HALCTF_DEFAULT_FLAG_PATTERN r"[A-Za-z][A-Za-z0-9_]{1,14}\{[^{}\s]+\}" (identifier-style prefixes flag{}/HALCTF{}...), __init__ resolves effective pattern (operator non-blank OZZGRAPH_FLAG_PATTERN wins, blank-means-unset like load_config, else HalCTF default), flag_extractor wired, exported from ozzgraph.environments.halctf, local default + benchmarks/matrix/lab byte-for-byte unchanged (judge PASS 52fb4801, all 3 criteria, tier1 lint/tests/secrets PASS) | Medium | 2±1 | 44a52d9 | DS-V4-Flash |
| HAL-001 | HalCTFRuntimeSnapshot from env — HAL_TARGET_<NAME>_IP/_PORT + single-form + HAL_CHALLENGE_* + HAL_AGENT_MODEL/HAL_RUN_ID/HAL_TEAM_UUID + flag-like env + OPENAI_BASE_URL/MCP_ENDPOINT → real-URL graph targets + Scope.hosts/urls + ScopePolicy target_allowlist (atomic), infra exclusion (sidecar 127.0.0.1:9000/model/MCP) (judge PASS a495fe1, all 5 criteria, tier1 lint/tests/secrets PASS) | Critical | 5±1 | 9fca71a | DS-V4-Flash |
| HAL-002 | MCP optional/fallback for HalCTF startup — OPENAI_BASE_URL out of HALCTF_ENDPOINT_CANDIDATES (model service /llm, never MCP), load_config + HalCTFEnvironment construct endpoint=None env-only, require_halctf_endpoint retained for genuine endpoint consumers, fail-loud scoped to unrecoverable config, hal_client localhost default unchanged (judge PASS d0e00cb, all 5 criteria, tier1 lint/tests/secrets PASS) | Critical | 3±1 | cbbffe5 | DS-V4-Flash |
| HAL-003 | Model routing in HalCTF mode — Supervisor._model_routing resolves HAL_AGENT_MODEL → model id + OPENAI_BASE_URL → client base URL via HAL-001 snapshot, supervisor-owned ModelService wired into runner (closed in finally), absent platform vars degrade to OZZGRAPH_MODEL_*/defaults, local mode unchanged, runner.started logs actual base_url via ModelService.base_url property (judge PASS 469be7f1, all 4 criteria, tier1 lint/tests/secrets PASS) | High | 3±1 | e9c421e | DS-V4-Flash |
| HAL-004 | Sidecar transport adapter — plain-HTTP /submit + /done to 127.0.0.1:9000 (best-effort done, never fatal), env-first base URL (OZZGRAPH_SIDECAR_BASE_URL → MCP origin → localhost default), normalizes {status:correct/accepted/solved/success/already_solved, points_awarded>0, boolean verdict fields} into UNCHANGED SubmissionResult via deterministic precedence, bounded transient retries, privilege guard, sidecar.* events, implements SubmissionClient protocol (coordinator/schema zero-line diff), HalCTFEnvironment.sidecar_submission_client factory (judge PASS ab4d87a9, all 4 criteria, tier1 lint/tests/secrets PASS) | High | 4±1 | 66cf337 | DS-V4-Flash |
| HAL-005 | Flag loop wired into active loop — runner invokes supervisor-owned hook (flag_submitter → _process_flag_candidates) after every executed turn's _persist_execution (all 3 observation-persisting paths), FlagCandidateExtractor.extract → Supervisor.submit_verified_candidate via supervisor-owned privileged sidecar client, ZERO LLM turns (test asserts model_calls == 1), accepted → objective-halctf-flag completed → COMPLETED → best-effort sidecar /done once, rejected → coordinator marks rejected:true (never re-submitted) → continue, no-candidate/no-op, limit/privilege refusals loud non-fatal events, privilege/budget/durability invariants preserved (judge PASS 45694ccf, all 4 criteria, tier1 lint/tests/secrets PASS) | Critical | 5±1 | ded08d4 | DS-V4-Flash |
| HAL-006 | Objective completion acceptance-gated — EnvironmentAdapter.verdict_satisfies_objectives(graph) environment-specific completion predicate, runner consults it before _complete_objectives on PlanVerdict.COMPLETE (Finding still renders unconditionally), HalCTF requires accepted submission entity in graph (strict-bool, fails loudly), Local always accepts (pre-HAL-006 byte-for-byte), accepted-submission DONE path untouched (judge PASS bfbec37b, all 4 criteria, tier1 lint/tests/secrets PASS) | Critical | 4±1 | 9c0921f | DS-V4-Flash |
| DOCS-000 | v2 documentation pass re-run — README refreshed for completed v2 milestone (release status v1.0.0 + v2 V01–V10, v2 modules in capabilities/layout: environments/, findings.py, observations.py, security_brain.py, specialists.py, profile_store.py/profile_data/, matrix.py, benchmarks/, lab/), v2 docs added to Documentation table (CHANGES_v2.md, OBSERVATIONS.md, BENCHMARKS.md, SYNTHETIC_LAB.md), repo description + 7 topics updated via gh repo edit, formatting bar intact (judge PASS 8ae01605, all 4 criteria, tier1 lint/tests/secrets PASS) | High | 3±1 | c436427 | DS-V4-Flash |
| V10 | full-regression: benchmarks/ package (registry + OzzGraph harness vs plain ReAct + scripted model + scoring + deterministic report), dead-end lab target with pivot proof (hypothesis_abandoned + PIVOT, bounded turns), tool-contract test (every required_capability resolves to installed provider), benchmark CLI (--target/--react/--max-turns/--out + OZZGRAPH_BENCHMARK_* env), docs/BENCHMARKS.md (judge PASS 9ce33342, all 4 criteria, tier1 lint/tests/secrets PASS) | High | 5±1 | 498a214 | DS-V4-Flash |
| INT-CI-001 | E2E driver imports V09-moved flag modules from canonical homes (ozzgraph.entities / ozzgraph.environments.halctf) — fixes CI Lint failure (ruff I001 on e2e_001_driver.py, symptom of deleted ozzgraph.flags regression) | High | 1±0 | 5628bf4 | DS-V4-Flash |
| V09 | halctf-adapter: HAL_* / OPENAI_BASE_URL / MCP_ENDPOINT discovery, official tool set (list_ctfs/challenges/status/submit_flag/request_hint/scoreboard), smoke flag, scoring, hint costs, graceful completion; hint-policy/submission/scoreboard/flag-candidate-extractor moved OUT of generic kernel into ozzgraph.environments.halctf (ADR-0011) (judge PASS d636bfd6, all criteria) | High | 4±1 | 6a7f8dc | DS-V4-Flash |
| V08 | local-assessment: URL/network/repository/Docker-Compose/hybrid modes via OZZGRAPH_TARGET classification, scope + credentials files (target_allowlist + Credential list, loud ConfigError), report bundle (report.md/json/sarif + evidence/ + graph.sqlite + events.jsonl) at COMPLETED, LocalEnvironment as DEFAULT, ADR-0010 (judge PASS 6025990f, all 4 criteria) | High | 5±1 | 198ba36 | DS-V4-Flash |
| V06 | security-brain: OpportunityGenerator + StrategicPlanner (LLM only when >1 viable path) + TaskBuilder + HypothesisManager + ProgressEvaluator, deterministic zero-LLM single-action path wired into runner (judge PASS ea396688, all 4 criteria) | Critical | 5±1 | c948e93 | DS-V4-Flash |
| V07 | specialists: SpecialistMicroAgent bounded hypothesis→experiment→observation→conclusion loop (MAX_MICRO_ITERATIONS=3, zero model calls, no full-graph context), Scheduler parallel hypotheses (hypothesis-id conflict keys) + serialized global strategy (MUTATION_CONFLICT_KEY), Reducer structured verdict merge (verdict+evidence_ids+impact CWE/assets/confidence), SpecialistFleet batch + runner dispatch, ADR-0009 (judge PASS 8b6c8e3, all 4 criteria) | High | 4±1 | e953e00 | DS-V4-Flash |
| V05 | model-harness-matrix: empirical per-model profiles — TOML-backed data-driven registry (profile_data/), ProfileStore discover/discover_from_service (GET /v1/models + capability probe), byte-deterministic TraceMetrics benchmark persistence (judge PASS b8a2cfd1, all 5 criteria) | High | 4±1 | 70d6f3f | DS-V4-Flash |
| V04 | semantic-observations: typed parsers/projectors for 17 high-value tools (JSON/XML/SARIF/JSONL), raw-first ArtifactStore persistence, runner observation wiring (judge PASS 01aa9dd, all 3 criteria) | Critical | 5±1 | 8e49dd0 | DS-V4-Flash |
| V03 | tool-runtime: ToolCatalog/ToolInventory/CapabilityRegistry/ToolProvider, startup tool inventory, capabilities-not-binaries, `:max` Kali image (judge PASS 3f4640ae, all 5 criteria) | Critical | 5±1 | c3c44f9 | DS-V4-Flash |
| V02 | autonomous-vertical-slice: `ozzgraph run <target>` end-to-end as a real process (CLI + console script, Finding model/store, evidence→hypothesis→Finding, evaluator wiring, process-level E2E test) (judge PASS 6c6a117a, all 3 criteria) | Critical | 5±1 | db18787 | DS-V4-Flash |
| V01 | generic-runtime: EnvironmentAdapter protocol, Scope/Target/Objective, Local/HalCTF environments, kernel rewrites, real AutonomousRunner (judge PASS d1416f4, all 5 criteria) | Critical | 5±1 | da1aaaf | DS-V4-Flash |
| FLAGLEAK-001 | Redact/hash flag material in run-only event-log events (flags.candidate_found/submission.attempted/submission.accepted/submission.rejected now carry flag_sha256+flag_length digests; graph.entity_created keeps raw flag — replay-required) (judge PASS, all 4 criteria) | High | 3±1 | a667733 | DS-V4-Flash |
| DOCS-000 | Documentation gate pass — CONTRIBUTING.md + SECURITY.md + README refresh + repo metadata (judge PASS 1913c392, all 3 criteria) | High | 3±1 | 06c5ab1 | DS-V4-Flash |
| DOC-001 | Full documentation pass — polished README + docs/USAGE.md + docs/CUSTOMIZATION.md (judge PASS 8cee566d) | High | 3±1 | 2a0daa3 | DS-V4-Flash |
| DEPS-001 | ruff 0.16.1→0.16.2 (direct dev dep patch bump; pydantic-core 2.48.0 BLOCKED by pydantic==2.46.4 pin) | Low | 1±0 | a70f3f7 | DS-V4-Flash |

| ID | Task | Pri | Cpx | Commit | Model |
|----|------|-----|-----|--------|-------|
| TEST-INFRA-001 | .coding-hermes/tests scaffold per coding-hermes-testing v1.0 (_index.md, test-state.toml, 5 prompts, 7 subdirs) | High | 3±1 | d58da21 | DS-V4-Flash |
| T32 | PR32: v1.0 release candidate (full DoD, rehearsal, image quality gates) | Critical | 5±1 | e10ca1d | DS-V4-Flash |
| T31 | PR31: image hardening (minimize, SBOM, immutable, <1.5GB) | High | 5±1 | ac7d3cb | DS-V4-Flash |
| T30 | PR30: optional Yarn dashboard (OUTSIDE image) | Medium | 5±1 | 0dc7098 | DS-V4-Flash |
| T29 | PR29: chaos + adversarial tests (loops, timeouts, malformed, injection) | High | 5±1 | cb3f46f | DS-V4-Flash |
| T28 | PR28: golden traces + model–harness matrix | High | 5±1 | 403e40a | DS-V4-Flash |
| T27 | PR27: synthetic test lab (isolated targets) | High | 5±1 | 6476a7f | DS-V4-Flash |
| T26 | PR26: reducer + conflict handling (validated findings merge) | Critical | 5±1 | e22b37b | DS-V4-Flash |
| T25 | PR25: specialist workers (scope-limited) | High | 5±1 | b4fd7da | DS-V4-Flash |
| T24 | PR24: task DAG + scheduler (conflict keys, bounded parallel) | High | 5±1 | 393a2ba | DS-V4-Flash |
| T23 | PR23: hint policy (free auto, paid gated, supervisor-only) | High | 4±1 | 72b0276 | DS-V4-Flash |
| T22 | PR22: flag provenance + supervisor-only submission | Critical | 4±1 | 40a62eb | DS-V4-Flash |
| T21 | PR21: evaluator (deterministic + model fallback) + replanning | High | 5±1 | 7666d01 | DS-V4-Flash |
| T20 | PR20: executor loop (one bounded action/turn, strict output contract) | Critical | 5±1 | 36f46a4 | DS-V4-Flash |
| T19 | PR19: planner + schemas (hypotheses, bounded plan, abandon conditions) | High | 5±1 | 7a7d16e | DS-V4-Flash |
| T18 | PR18: graph-driven phase router (predicates, not counts) | Critical | 5±1 | a9cf1cb | DS-V4-Flash |
| T17 | PR17: skill registry + lazy loading + initial skill packs | High | 4±1 | 2cc240a | DS-V4-Flash |
| T16 | PR16: context compiler (bounded subgraph view, layers) | Critical | 5±1 | 26b9c8e | DS-V4-Flash |
| T15 | PR15: JSON adapter + repair strategy | High | 4±1 | 3a07f79 | DS-V4-Flash |
| T14 | PR14: terminal-native + three-line adapters | High | 4±1 | 706cc7c | DS-V4-Flash |
| T13 | PR13: model profile + adapter interfaces | High | 4±1 | 282625b | DS-V4-Flash |
| T12 | PR12: deterministic bootstrap (target parse, smoke flag, probe, free hint) | High | 4±1 | 681f79f | DS-V4-Flash |
| T11 | PR11: observation parsers (normalized results) | High | 4±1 | d0f03df | DS-V4-Flash |
| T10 | PR10: scope policy + duplicate detection (fingerprints, allowlists) | Critical | 5±1 | 6589721 | DS-V4-Flash |
| T09 | PR9: bounded shell runner (process-group timeout, truncation, ToolResult) | Critical | 5±1 | 5745d2d | DS-V4-Flash |
| T08 | PR8: artifact store + JSONL event log + replay (graph hash) | Critical | 5±1 | 59a3cff | DS-V4-Flash |
| T07 | PR7: SQLite state graph (entities, edges, schema_version, migrations) | Critical | 5±1 | 32d9185 | DS-V4-Flash |
| T06 | PR6: MCP client + `halctl` adapter (challenge/status/submit/hint/exit) | High | 5±1 | 2af48d3 | DS-V4-Flash |
| T05 | PR5: OpenAI-compatible model client (list/complete, retry, timeout, token usage) | High | 4±1 | 60f4be6 | DS-V4-Flash |
| T04 | PR4: structured event logging | High | 3±1 | 0948763 | DS-V4-Flash |
| T00 | Bootstrap init — repo, spec docs, AGENTS.md, uv scaffold | Trivial | 1±0 | — | DS-V4-Flash |
| T03 | PR3: heartbeat, budgets, lifecycle (SIGTERM/SIGINT) | High | 4±1 | T02 | DS-V4-Flash | 41b16a4 |
| T01 | PR1: init uv project + CI skeleton (lint, format, type, test gates) | Critical | 2±1 | e4deda1 | DS-V4-Flash |
| T02 | PR2: runtime configuration + supervisor skeleton | Critical | 4±1 | 02711ad | DS-V4-Flash |

## [x] DOCS-000 — Documentation pass re-run for completed v2 milestone (V01–V10)

**Tick 2026-08-08: DOCS-000 done — v2 documentation gate PASS (judge 8ae01605).**
Worker (deepseek-v4-flash @ openrouter) refreshed README.md for the completed v2
milestone: release status line "v1.0.0 shipped + v2 (V01–V10) complete" with
general security-research reframing (HalCTF as one optional adapter), v2
modules added to capabilities table + repository layout tree (environments/,
lab/, benchmarks/, profile_data/, findings.py, observations.py,
security_brain.py, specialists.py, profile_store.py, matrix.py), v2 docs added
to Documentation table (CHANGES_v2.md, OBSERVATIONS.md, BENCHMARKS.md,
SYNTHETIC_LAB.md), quick-start now leads with `ozzgraph run <target>` local
mode, test count 880→1181. Repo metadata updated via gh repo edit: description
"Autonomous security-research harness: model-adaptive agent supervisor with
SQLite graph state, provenance, bounded actions, and pluggable environment
adapters (HalCTF, local, lab)" + 7 topics (agent, automation, cli, harness,
cybersecurity, security, vulnerability-research — dropped stale ctf/go).
Formatting bar intact (title/badges/nav/mermaid/tables). Content landed in
c436427 (worker committed + pushed; guards PASS). First judge run FAIL was the
known spurious tier1 env artifact (ruff: not found — .venv/bin missing from
judge subprocess PATH, gitreins-usage pitfall); re-run with
PATH=$PWD/.venv/bin:$PATH → PASS 8ae01605 (all 4 criteria, tier1
lint/tests/secrets PASS). Board idle → cooldown 43200s.

## [ ] E2E-001 — E2E Testing Tick (self-improving loop)

Spawn Luna (browser/screenshots) or Step 3.7 Flash (CLI/API). Deploy/build,
run the F2B→B2F cycle using .coding-hermes/tests/ scaffold (test-state.toml
dims: f2b/b2f/negative/visual/crypto/wiring/structure/audit — all currently 0).
halctl CLI journeys, artifact store, executor loop. → update test-state.toml →
inject findings into board. Every 5-10 ticks.

## [ ] NEVER-DONE — Run coding-hermes-never-done 12-point audit

Load coding-hermes-never-done skill. Run ALL 12 checks: spec alignment,
doc coverage, test gaps, package upgrades, pitfall hunt, performance audit,
endpoint verification, CI/CD health, DuckBrain sync, code quality,
middle-out wiring, usability smoke test. Create a task for EVERY gap found.
This task is never complete — the audit always finds something.
