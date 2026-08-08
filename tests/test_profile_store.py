"""Tests for the TOML-backed ProfileStore (V05 model-harness-matrix).

Covers the empirical per-model profile contract from
docs/CHANGES_v2.md milestone 5: TOML profile files load through a
ProfileStore (replacing hardcoded family assumptions); discovery
selects profiles from data (GET /v1/models + capability probe) and
probes unknown models instead of assuming; benchmark persistence
round-trips measured harness metrics into the TOML files; and the
fail-loud paths (missing dir, duplicate family, corrupt file,
unwritable dir) never degrade silently.
"""

from __future__ import annotations

import asyncio
import shutil
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from ozzgraph.matrix import MatrixProbe, MatrixReport, MatrixRow
from ozzgraph.model_client import ModelService
from ozzgraph.profile_store import ProfileStore, ProfileStoreError
from ozzgraph.profiles import (
    MODELS_MATCH_CONFIDENCE,
    PROBE_CONFIDENCE_THRESHOLD,
    PROBE_MATCH_CONFIDENCE,
    PROTOCOL_FUNCTION_CALL,
    PROTOCOL_JSON,
    PROTOCOL_TERMINAL,
    default_profile_dir,
    load_profiles_from_dir,
)
from ozzgraph.traces import TraceMetrics

_JSON_SAMPLE = '{"kind": "run", "payload": "ls -la", "rationale": "list the dir"}'

_METRICS = TraceMetrics(
    valid_output_rate=0.9,
    correct_tool_selection=1.0,
    repetition_rate=0.1,
    recovery_rate=0.8,
    output_tokens_per_decision=120.0,
    steps_per_objective=6.0,
    solve_rate=0.5,
    unsupported_fact_rate=0.0,
    unsupported_flag_rate=0.0,
)


class _NoopSleeper:
    """Backoff sleeper that returns immediately (deterministic tests)."""

    async def __call__(self, _: float) -> None:
        return None


def _service(handler: Any, **kwargs: Any) -> ModelService:
    """Build a ModelService on a MockTransport with a no-op backoff sleeper."""
    return ModelService(transport=httpx.MockTransport(handler), sleeper=_NoopSleeper(), **kwargs)


def _chat_response(content: str) -> dict[str, object]:
    """A well-formed OpenAI-compatible chat completion response."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
    }


@pytest.fixture
def seeded_dir(tmp_path: Path) -> Path:
    """A writable store dir seeded with the shipped profile TOML files."""
    data_dir = tmp_path / "profiles"
    data_dir.mkdir()
    for path in sorted(default_profile_dir().glob("*.toml")):
        shutil.copy(path, data_dir / path.name)
    return data_dir


# ---------------------------------------------------------------------------
# Loading: TOML files -> ProfileStore
# ---------------------------------------------------------------------------


def test_store_loads_shipped_toml_profiles() -> None:
    """The default store loads every shipped per-model TOML profile."""
    store = ProfileStore()
    assert set(store.families()) == {"claude", "deepseek", "fallback", "gpt", "llama"}
    gpt = store.profile_for("gpt-4o")
    assert gpt.family == "gpt"
    assert gpt.output_token_limit == 4096
    assert gpt.context_soft_limit == 128_000
    assert gpt.protocols == {PROTOCOL_TERMINAL, PROTOCOL_JSON, "three_line"}
    # Per-model data ships in the files (exact-id discovery evidence).
    assert "gpt-4o" in gpt.model_ids
    assert "deepseek-v4-flash-0731" in store.profile_for("deepseek-v4").model_ids
    # Benchmarks section exists per protocol (all-zero placeholders).
    assert set(gpt.benchmarks or {}) == {PROTOCOL_TERMINAL, PROTOCOL_JSON, "three_line"}
    assert store.profile_for("totally-unknown").family == "fallback"
    assert store.profile_for("totally-unknown").confidence < PROBE_CONFIDENCE_THRESHOLD


def test_store_load_is_deterministic() -> None:
    """Two stores over the same data yield identical registries."""
    first = ProfileStore().registry()
    second = ProfileStore().registry()
    assert first == second
    assert list(first) == list(second)  # same iteration order


def test_store_loads_from_custom_dir(seeded_dir: Path) -> None:
    """A store over a writable data dir loads the same profiles."""
    store = ProfileStore(seeded_dir)
    assert store.profile_for("claude-3").family == "claude"
    assert store.data_dir == seeded_dir


def test_load_profiles_from_dir_missing_dir_fails_loudly(tmp_path: Path) -> None:
    """A vanished data source raises, never silently degrades."""
    with pytest.raises(FileNotFoundError):
        load_profiles_from_dir(tmp_path / "does-not-exist")


def test_load_profiles_from_dir_duplicate_family_fails_loudly(tmp_path: Path) -> None:
    """Two files declaring the same family are ambiguous: loud error."""
    profile = (
        'family = "gpt"\n'
        'protocols = ["terminal"]\n'
        "context_soft_limit = 8000\n"
        "output_token_limit = 1024\n"
        'supported_roles = ["user", "assistant"]\n'
        "max_advertised_skills = 0\n"
        'failure_behavior = "abort_turn"\n'
        "confidence = 0.3\n"
    )
    (tmp_path / "a.toml").write_text(profile)
    (tmp_path / "b.toml").write_text(profile.replace("confidence = 0.3", "confidence = 0.4"))
    with pytest.raises(ValueError, match="duplicate profile family"):
        load_profiles_from_dir(tmp_path)


def test_store_corrupt_toml_fails_loudly(tmp_path: Path) -> None:
    """A corrupt profile file raises instead of silently skipping."""
    (tmp_path / "gpt.toml").write_text("family = [unterminated")
    with pytest.raises(tomllib.TOMLDecodeError):
        ProfileStore(tmp_path)


# ---------------------------------------------------------------------------
# Selection: profile_for / discover from data
# ---------------------------------------------------------------------------


def test_profile_for_exact_model_id(seeded_dir: Path) -> None:
    """Exact per-model ids (case-insensitive) select the family profile."""
    store = ProfileStore(seeded_dir)
    assert store.profile_for("gpt-4o").family == "gpt"
    assert store.profile_for("GPT-4O").family == "gpt"
    assert store.profile_for("deepseek-v4-flash-0731").family == "deepseek"
    # Family-prefix matching still covers the rest of the line.
    assert store.profile_for("gpt-anything-else").family == "gpt"
    assert store.profile_for("llama3.1:8b").family == "llama"
    # Unknown ids resolve to the low-confidence fallback, never assumed.
    assert store.profile_for("totally-unknown-model").family == "fallback"


def test_discover_known_family_stands_on_data(seeded_dir: Path) -> None:
    """High-confidence data profiles are not refined away."""
    store = ProfileStore(seeded_dir)
    profile = store.discover("gpt-4o", sample=_JSON_SAMPLE)
    assert profile.family == "gpt"
    assert profile.confidence == store.profile_for("gpt-4o").confidence
    assert PROTOCOL_FUNCTION_CALL not in profile.protocols


def test_discover_unknown_probed_not_assumed(seeded_dir: Path) -> None:
    """Unknown models are refined by the capability probe + model list."""
    store = ProfileStore(seeded_dir)
    probed = store.discover("totally-unknown-model", sample=_JSON_SAMPLE)
    assert PROTOCOL_JSON in probed.protocols
    assert probed.confidence == pytest.approx(
        store.profile_for("totally-unknown-model").confidence + PROBE_MATCH_CONFIDENCE
    )
    listed = store.discover("totally-unknown-model", models=["totally-unknown-model", "gpt-4o"])
    assert listed.confidence == pytest.approx(
        store.profile_for("totally-unknown-model").confidence + MODELS_MATCH_CONFIDENCE
    )
    combined = store.discover(
        "totally-unknown-model", sample=_JSON_SAMPLE, models=["totally-unknown-model"]
    )
    assert PROTOCOL_JSON in combined.protocols
    assert PROTOCOL_FUNCTION_CALL not in combined.protocols
    assert combined.confidence == pytest.approx(
        store.profile_for("totally-unknown-model").confidence
        + MODELS_MATCH_CONFIDENCE
        + PROBE_MATCH_CONFIDENCE
    )


def test_discover_never_mutates_store_data(seeded_dir: Path) -> None:
    """Discovery returns copies; the store's profiles never change."""
    store = ProfileStore(seeded_dir)
    before = store.profile_for("totally-unknown-model")
    store.discover("totally-unknown-model", sample=_JSON_SAMPLE)
    assert store.profile_for("totally-unknown-model").protocols == before.protocols
    assert store.profile_for("totally-unknown-model").confidence == before.confidence


# ---------------------------------------------------------------------------
# Discovery from data: GET /v1/models + capability probe
# ---------------------------------------------------------------------------


def test_discover_from_service_probes_unknown_model(seeded_dir: Path) -> None:
    """Unknown models: list_models + one probe completion, refined from data."""
    completions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-4o", "owned_by": "openai"},
                        {"id": "mystery-model", "owned_by": "acme"},
                    ]
                },
            )
        completions.append(request.url.path)
        return httpx.Response(200, json=_chat_response(_JSON_SAMPLE))

    store = ProfileStore(seeded_dir)
    service = _service(handler)
    try:
        profile = asyncio.run(store.discover_from_service(service, "mystery-model"))
    finally:
        asyncio.run(service.aclose())
    assert PROTOCOL_JSON in profile.protocols
    assert profile.family == "fallback"  # probed, not assumed into a family
    assert completions == ["/v1/chat/completions"]  # exactly one probe


def test_discover_from_service_known_model_not_probed(seeded_dir: Path) -> None:
    """Known families stand on their data: no probe completion is sent."""
    completions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-4o", "owned_by": "openai"},
                        {"id": "mystery-model", "owned_by": "acme"},
                    ]
                },
            )
        completions.append(request.url.path)
        return httpx.Response(200, json=_chat_response(_JSON_SAMPLE))

    store = ProfileStore(seeded_dir)
    service = _service(handler)
    try:
        profile = asyncio.run(store.discover_from_service(service, "gpt-4o"))
    finally:
        asyncio.run(service.aclose())
    assert profile.family == "gpt"
    assert profile.protocols == store.profile_for("gpt-4o").protocols
    assert completions == []  # listed in GET /v1/models, high confidence: no probe


# ---------------------------------------------------------------------------
# Benchmark persistence: matrix output -> TOML store
# ---------------------------------------------------------------------------


def test_update_benchmarks_round_trip(seeded_dir: Path) -> None:
    """Measured metrics persist into the profile TOML and reload equal."""
    store = ProfileStore(seeded_dir)
    store.update_benchmarks("gpt-4o", PROTOCOL_JSON, _METRICS)
    reloaded = ProfileStore(seeded_dir)
    profile = reloaded.profile_for("gpt-4o")
    benchmarks = profile.benchmarks
    assert benchmarks is not None
    assert benchmarks[PROTOCOL_JSON] == _METRICS
    text = (seeded_dir / "gpt.toml").read_text()
    assert "[benchmarks.json]" in text
    assert "solve_rate = 0.5" in text
    # The rest of the profile data is untouched.
    assert profile.output_token_limit == 4096


def test_update_benchmarks_in_memory_sync(seeded_dir: Path) -> None:
    """After persisting, the same store instance serves the new benchmarks."""
    store = ProfileStore(seeded_dir)
    store.update_benchmarks("claude-3", "three_line", _METRICS)
    benchmarks = store.profile_for("claude-3").benchmarks
    assert benchmarks is not None
    assert benchmarks["three_line"] == _METRICS


def test_update_benchmarks_unknown_model_persists_to_fallback(seeded_dir: Path) -> None:
    """Unknown models' measured metrics persist into the fallback profile."""
    store = ProfileStore(seeded_dir)
    store.update_benchmarks("totally-unknown-model", PROTOCOL_TERMINAL, _METRICS)
    reloaded = ProfileStore(seeded_dir)
    benchmarks = reloaded.profile_for("totally-unknown-model").benchmarks
    assert benchmarks is not None
    assert benchmarks[PROTOCOL_TERMINAL] == _METRICS


def test_update_benchmarks_is_byte_deterministic(seeded_dir: Path) -> None:
    """Writing the same metrics twice produces identical file bytes."""
    store = ProfileStore(seeded_dir)
    store.update_benchmarks("gpt-4o", PROTOCOL_JSON, _METRICS)
    first = (seeded_dir / "gpt.toml").read_bytes()
    store.update_benchmarks("gpt-4o", PROTOCOL_JSON, _METRICS)
    second = (seeded_dir / "gpt.toml").read_bytes()
    assert first == second


def test_persist_report_round_trips_into_profiles(seeded_dir: Path) -> None:
    """A matrix report's rows persist per protocol into the store."""
    store = ProfileStore(seeded_dir)
    report = MatrixReport(
        model_id="gpt-4o",
        probe=MatrixProbe(model_id="gpt-4o", sample=_JSON_SAMPLE, detected_protocol="json"),
        rows=[
            MatrixRow(
                model_id="gpt-4o",
                protocol=PROTOCOL_JSON,
                episodes=2,
                steps=6,
                completions=6,
                solved_episodes=1,
                total_completion_tokens=720,
                metrics=_METRICS,
                interactions=[],
            ),
            MatrixRow(
                model_id="gpt-4o",
                protocol="three_line",
                episodes=2,
                steps=6,
                completions=6,
                solved_episodes=1,
                total_completion_tokens=720,
                metrics=_METRICS,
                interactions=[],
            ),
        ],
    )
    store.persist_report(report)
    reloaded = ProfileStore(seeded_dir)
    benchmarks = reloaded.profile_for("gpt-4o").benchmarks
    assert benchmarks is not None
    assert benchmarks[PROTOCOL_JSON] == _METRICS
    assert benchmarks["three_line"] == _METRICS


def test_update_benchmarks_unwritable_dir_fails_loudly(tmp_path: Path) -> None:
    """A store whose data dir cannot be written raises ProfileStoreError."""
    for path in sorted(default_profile_dir().glob("*.toml")):
        shutil.copy(path, tmp_path / path.name)
    store = ProfileStore(tmp_path)
    (tmp_path / "gpt.toml").chmod(0o444)
    try:
        with pytest.raises(ProfileStoreError):
            store.update_benchmarks("gpt-4o", PROTOCOL_JSON, _METRICS)
    finally:
        (tmp_path / "gpt.toml").chmod(0o644)
