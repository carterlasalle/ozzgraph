# OzzGraph v1.0 Release Candidate (PR32)

Status: **release candidate** — final PR of the 32-PR sequence
(`docs/IMPLEMENTATION_PLAN.md` item 32).
Version: **1.0.0** (bumped from 0.1.0 — see [Version bump note](#version-bump-note)).

This document is the v1.0 release checklist. It maps every Definition of Done
item from `docs/IMPLEMENTATION_PLAN.md` (the "v1.0 requires" list) to concrete
verification evidence, records the rehearsal results, and gives the build/run
instructions, the SBOM procedure, and the version-bump note.

Rehearsal date: 2026-08-07, on `main` at PR32 (all 31 prior PRs merged).

---

## 1. Definition of Done — v1.0 checklist

Legend for evidence columns:

- **Kernel** — owning module in `src/ozzgraph/`.
- **Tests** — `tests/` test names (run by `uv run pytest`).
- **CI gate** — job in `.github/workflows/ci.yml`.
- **Docs** — section under `docs/`.
- **Rehearsal** — PASS, or FAIL + the fix applied in PR32.

| # | DoD item | Kernel | Tests | CI gate | Docs | Rehearsal |
|---|----------|--------|-------|---------|------|-----------|
| 1 | Correct container startup | `Dockerfile` (ENTRYPOINT `python -m ozzgraph`), `__main__.py` | `tests/test_image_hardening.py::test_runtime_stage_entrypoint_runs_kernel_and_halctl_on_path`, `test_main_version_flag` | `docker` job smokes: `--version`, `halctl --help`, read-only 2 s run | `IMAGE_HARDENING.md` §Build Recipe, §CI Gate | PASS |
| 2 | Identity and heartbeat | `supervisor.py` (`USER ID:` first line), `heartbeat.py` | `test_supervisor.py::test_start_prints_identity_immediately`, `test_signals.py::test_user_id_is_first_output_before_any_heartbeat`, `test_heartbeat.py::test_run_emits_one_heartbeat_then_stops` / `test_run_emits_multiple_heartbeats`, `test_entrypoint.py::test_main_prints_identity_and_exits_budget_exhausted` | `docker` smoke now asserts `USER ID: ci-smoke` inside the read-only run (PR32) | `ARCHITECTURE.md`, `TESTING_AND_QA.md` | PASS — in-container evidence strengthened in PR32 |
| 3 | Authorized-only communication | `hal_client.py` (privileged guard), `halctl.py` | `test_hal_client.py::test_privileged_guard_denies_non_supervisor`, `test_halctl.py::test_submit_denied_without_privilege` / `test_exit_denied_without_privilege`, `test_submissions.py::test_non_privileged_client_raises_before_wire`, `test_policy.py::test_executor_can_never_reach_a_submit_command_family` | `test` job (suite), `docker` smoke (halctl on PATH) | `AGENTS.md` invariant 5, `API_AND_INTEGRATIONS.md` | PASS |
| 4 | Model discovery or safe fallback | `profiles.py` (`probe_protocol`, `discover_profile`, `profile_for_model_id` + conservative fallback) | `test_profiles.py::test_probe_protocol_terminal` / `test_probe_protocol_three_line` / `test_probe_protocol_json` / `test_probe_protocol_never_raises_on_hostile_input`, `test_discover_profile_*` (5), `test_profile_for_model_id_unknown_returns_low_confidence_fallback`, `test_fallback_profile_is_conservative` | `test` job | `API_AND_INTEGRATIONS.md` (model profiles) | PASS |
| 5 | At least three protocols | `adapters.py` + terminal / three-line / JSON adapters | `test_adapters_terminal.py`, `test_adapters_three_line.py`, `test_adapters_json.py` (each: parse, prompt, repair, rejects), `test_matrix.py::test_default_evaluation_produces_three_protocol_rows` | `test` job | `API_AND_INTEGRATIONS.md` (adapters) | PASS |
| 6 | Replayable graph | `replay.py`, `state_graph.py` (append-only JSONL → identical `graph_hash`) | `test_replay.py::test_replay_reconstructs_identical_graph`, `test_replay_matches_live_graph_invariant`, `test_replay_update_and_delete_identical_hash`, `test_replay_after_flag_candidate_and_submission`, `test_state_graph.py::test_graph_hash_deterministic_across_fresh_dbs` | `test` job | `DATA_STRATEGY.md`, `TESTING_AND_QA.md` | PASS |
| 7 | Provenance for all facts | `reducer.py`, `planner.py`, `submissions.py`, `router.py` | `test_reducer.py::test_finding_without_evidence_is_rejected_through_worker_run` / `test_resolve_evidence_raises_unresolved_evidence_error`, `test_planner.py::test_hypothesis_without_evidence_refs_raises`, `test_submissions.py::test_verified_candidate_without_provenance_raises`, `test_flags.py::test_verified_candidate_without_provenance_edge_raises`, `test_router.py::test_verified_flag_without_provenance_raises` | `test` job | `AGENTS.md` Data Invariants, `DATA_STRATEGY.md` | PASS |
| 8 | Normalized tool output | `observations.py` (shell + halctl parsers → labeled, bounded Observations) | `test_observations.py::test_shell_parser_success_output` / `test_shell_parser_timeout_noted_in_summary` / `test_halctl_parser_normalizes_real_documents` / `test_parsers_are_deterministic`, `test_adversarial.py::test_shell_parser_never_trusts_adversarial_output` | `test` job | `TESTING_AND_QA.md` (Adversarial Tests) | PASS |
| 9 | Bounded planning and execution | `planner.py`, `executor.py`, `budgets.py` | `test_planner.py::test_plan_step_cap_is_respected`, `test_executor.py::test_happy_path_returns_one_bounded_action` / `test_overlong_action_text_raises` / `test_model_call_budget_exhaustion_raises`, `test_budgets.py` (token/model/tool/runtime budgets) | `test` job | `API_AND_INTEGRATIONS.md`, `AGENTS.md` rule 4 | PASS |
| 10 | Hypothesis abandonment | `evaluator.py` | `test_evaluator.py::test_hypothesis_abandoned_when_contradicted`, `test_plan_replanned_when_all_hypotheses_refuted`, `test_plan_abandoned_when_graph_leaves_plan_phase`, `test_step_abandoned_after_attempt_threshold_no_loop` | `test` job | `IMPLEMENTATION_PLAN.md` Phase 7 exit | PASS |
| 11 | Lazy skills | `skills.py` (summaries first, cards on demand) | `test_skills.py::test_lazy_loading_summaries_first_cards_on_demand`, `test_list_summaries_are_compact_advertisements`, `test_list_summaries_filters_by_phase` | `test` job | `AGENTS.md` rule 6, `API_AND_INTEGRATIONS.md` | PASS |
| 12 | Bounded parallel workers | `scheduler.py`, `workers.py` | `test_scheduler.py::test_never_more_than_max_workers_concurrent`, `test_conflicting_tasks_never_run_concurrently`, `test_serialized_task_never_runs_concurrently_with_anything`, `test_workers.py::test_read_only_worker_rejects_mutating_family_command` | `test` job | `ADR-0004`, `ADR-0005`, `TESTING_AND_QA.md` | PASS |
| 13 | Loop prevention | `policy.py` (fingerprints), `executor.py`, `evaluator.py`, `matrix.py` | `test_loop_detection.py::test_repeated_proposal_rejected_across_turns` / `test_plan_abandoned_when_every_step_failed` / `test_plan_model_call_budget_abandons_looping_plan` / `test_matrix_repetition_rate_detects_looping_model`, `test_executor.py::test_duplicate_fingerprint_is_rejected` / `test_failed_fingerprint_is_never_retried` | `test` job | `TESTING_AND_QA.md` (Loop and Timeout Detection) | PASS |
| 14 | Supervisor-only flags and hints | `flags.py`, `submissions.py`, `hints.py`, `supervisor.py` | `test_flags.py::test_unprivileged_client_is_refused_before_any_wire_call`, `test_hints.py::test_paid_hint_requires_privileged_client`, `test_supervisor.py::test_request_paid_hint_blocked_end_to_end`, `test_halctl.py::test_hint_zero_free_but_paid_hint_guarded` | `test` job | `ADR-0003`, `AGENTS.md` invariant 5 | PASS |
| 15 | Synthetic multi-stage solves | `lab/` (synthetic targets) | `test_lab.py::test_multi_stage_flag_after_two_chained_steps` / `test_network_pivot_flag_on_second_hop`, `test_lab_solve.py::test_full_solve_hidden_routes_through_harness` / `test_full_solve_auth_logic_flag_after_credentials` | `test` job | `SYNTHETIC_LAB.md`, `TESTING_AND_QA.md` | PASS |
| 16 | Deterministic golden replay | `traces.py` (capture/verify) | `test_traces.py::test_capture_is_byte_deterministic`, `test_capture_verify_round_trip_is_identical`, `test_replay_of_trace_reproduces_expected_final_graph`, `test_verify_flags_*` (entity/edge/hash/metric/schema regressions) | `test` job | `GOLDEN_TRACES.md`, `TESTING_AND_QA.md` | PASS |
| 17 | Full CI quality gates | — | non-Docker shape tests in `tests/test_image_hardening.py` | `lint`, `format`, `type`, `test`, `docker` jobs in `.github/workflows/ci.yml` | `TESTING_AND_QA.md` §CI Gates | PASS |
| 18 | Image size target | `Dockerfile` (multi-stage, slim base) | `test_image_hardening.py::test_size_budget_constant_is_1_5_gib` / `test_image_size_validator_boundaries` / `test_budget_is_consistent_across_ci_and_docs` | `docker` job size assertion (`1500 * 1024 * 1024` bytes) | `IMAGE_HARDENING.md` §Size Measurements, §CI Gate | PASS |
| 19 | Clear termination summary | `supervisor.py` (structured `termination` event), `__main__.py` (exit-code map + `TERMINATION: <reason>` line) | `test_supervisor.py::test_stop_writes_termination_event_with_reason` / `test_run_writes_bootstrap_and_termination_events`, `test_chaos.py::test_sigterm_style_stop_records_structured_termination_event`, `test_entrypoint.py` (PR32: asserts the summary is the final stdout line) | `docker` read-only smoke asserts the final line is `TERMINATION: budget_exhausted` (PR32) | `AGENTS.md` rule 9 | **PASS — gap fixed in PR32** |

Result: **19/19 PASS**. Three items were strengthened during the rehearsal
(gaps 2, 18, 19 below); none remain unaddressed.

---

## 2. Rehearsal results

Method: every DoD item was traced from `docs/IMPLEMENTATION_PLAN.md` to its
owning module, its tests (run via `uv run pytest -x -q`), its CI gate, and its
documentation, then exercised where practical (entry-point runs, replay
invariants, adapter fixtures, image fallback verification). The four quality
gates below were run on the final tree.

### Found and fixed in PR32

1. **DoD 19 — human-readable termination summary was missing.** The kernel
   wrote the structured `termination` event and mapped the reason to an exit
   code, but stdout ended with the last heartbeat; the operator got no
   human-readable summary, violating AGENTS.md rule 9 ("structured termination
   event *and* a human-readable summary").
   - Fix: `src/ozzgraph/__main__.py` now prints
     `TERMINATION: <reason>` (e.g. `TERMINATION: budget_exhausted`) as the
     final stdout line on every terminal path.
   - Tests: `tests/test_entrypoint.py` asserts the line in both the
     in-process run and the `python -m ozzgraph` subprocess run.

2. **CI never proved the image runs as a non-root user.** The Dockerfile has
   `USER ozzgraph` (uid 10001) and the shape tests check the directive, but no
   CI step ran the image and checked the runtime user.
   - Fix: new `docker` job step
   `Smoke — non-root runtime user` runs
   `docker run --rm --entrypoint id ozzgraph:ci` and asserts
   `uid=10001(ozzgraph)` / `gid=10001(ozzgraph)` and the absence of
   `uid=0(root)`.
   - Tests: `tests/test_image_hardening.py::test_ci_docker_gate_asserts_non_root_and_startup_evidence`.

3. **The read-only smoke only asserted the exit code.** It proved the run
   terminated with `BUDGET_EXHAUSTED` but not that the container actually
   started correctly.
   - Fix: the smoke now also asserts the identity line (`USER ID: ci-smoke`)
     appears and that the final log line is the termination summary —
     in-container evidence for DoD items 1, 2, and 19.
   - Tests: same CI-wiring test as above.

### Verified clean (no change needed)

All other DoD items passed against existing evidence — notably replay
determinism (items 6/16), provenance rejection paths (item 7), the three
protocols with repair (item 5), bounded scheduling (items 9/12), loop
prevention (item 13), and the image-size budget shared across CI, docs, and
tests (item 18).

### Baseline gates (final tree)

```bash
uv run ruff check .                                  # PASS
uv run ruff format --check .                         # PASS
uv run mypy src                                      # PASS
uv run pytest -x -q                                  # PASS (full suite)
```

---

## 3. Build and run

The competition image is built and gated exactly as documented in
`docs/IMAGE_HARDENING.md`. Requires a Docker daemon with BuildKit (Docker >= 23
or buildx).

```bash
# Build (from the repository root)
docker build -t ozzgraph:latest .

# Version smoke (ENTRYPOINT runs `python -m ozzgraph`)
docker run --rm ozzgraph:latest --version
# -> ozzgraph 1.0.0

# halctl adapter on PATH
docker run --rm --entrypoint halctl ozzgraph:latest --help

# Immutable runtime: read-only rootfs, state on the declared volume,
# 2-second budget run must exit 3 = BUDGET_EXHAUSTED
docker run --rm --read-only --tmpfs /tmp \
  -e HAL_USER_ID=my-user \
  -e OZZGRAPH_MAX_RUNTIME_S=2 \
  -e OZZGRAPH_HEARTBEAT_INTERVAL_S=1 \
  ozzgraph:latest
# exits 3; stdout ends with "TERMINATION: budget_exhausted"

# Non-root runtime user (uid 10001, never root)
docker run --rm --entrypoint id ozzgraph:latest
# -> uid=10001(ozzgraph) gid=10001(ozzgraph) groups=10001(ozzgraph)

# Persist run state across container exit
docker run --rm -v /host/path:/var/lib/ozzgraph/state \
  -e HAL_USER_ID=my-user -e OZZGRAPH_MAX_RUNTIME_S=30 ozzgraph:latest
```

CI runs the same checks on every PR (`docker` job in
`.github/workflows/ci.yml`): build, size < 1.5 GiB, version smoke, halctl
smoke, non-root check, and the read-only budget run.

No Docker daemon available? Follow the fallback verification in
`docs/IMAGE_HARDENING.md` §Fallback Verification (SBOM script syntax check,
builder-stage `uv sync --frozen --no-dev --no-editable` into a scratch venv,
`uv run pytest tests/test_image_hardening.py`).

---

## 4. SBOM generation

`scripts/gen-sbom.sh` produces SPDX 2.3 and CycloneDX 1.5 documents for a
built image using [syft](https://github.com/anchore/syft):

```bash
# Install syft once
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin

# Generate (image + output dir are optional; defaults shown)
scripts/gen-sbom.sh ozzgraph:latest sbom/
# -> sbom/ozzgraph.spdx.json   sbom/ozzgraph.cdx.json

# Optional: attach the SBOM to the image as an OCI artifact (requires cosign)
cosign attach sbom --sbom sbom/ozzgraph.spdx.json --type spdx ozzgraph:latest
```

The script prints a package-count sanity summary from the SPDX document and
needs no network access to the image. Without syft, a Python-dependency audit
fallback is:

```bash
uv export --frozen --no-dev | uv run pip-audit -r /dev/stdin
```

The image carries no OS package installer and no dev tooling
(`IMAGE_HARDENING.md` §Minimization Choices), so the SBOM is small and stable.

---

## 5. Version bump note

0.1.0 → **1.0.0** for the release candidate:

- `pyproject.toml` — `[project] version = "1.0.0"`
- `src/ozzgraph/__init__.py` — `__version__ = "1.0.0"`
- `uv.lock` — root package `version = "1.0.0"` (verified with `uv lock --check`)

The two source locations are kept in sync by convention (the task constraint:
"keep both in sync"); `python -m ozzgraph --version` and
`docker run --rm IMAGE --version` report the package `__version__`.

Rationale: all 31 PRs of the implementation sequence are merged, every v1.0
Definition of Done item is implemented and verified (section 1), and the
public surface (kernel entry point, `halctl`, event/replay formats, image) is
stable. Per semver, 1.0.0 signals the first stable, feature-complete release.

---

## 6. Related documents

- `docs/IMPLEMENTATION_PLAN.md` — PR sequence and Definition of Done
- `docs/IMAGE_HARDENING.md` + `docs/adr/0007-immutable-competition-image.md` — container story
- `docs/TESTING_AND_QA.md` — test suite, CI gates, container hardening section
- `docs/GOLDEN_TRACES.md`, `docs/SYNTHETIC_LAB.md` — golden replay and lab evidence
- `AGENTS.md` — invariants, forbidden shortcuts, Definition of Done for a PR
