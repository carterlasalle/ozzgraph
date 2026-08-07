"""Tests for model profiles and protocol probing (PR13).

Covers family prefix mapping (including case-insensitivity, the
non-letter boundary rule, and garbage input), the deterministic
protocol probe on untrusted samples (JSON / three-line / terminal /
ambiguous / hostile), discovery refinement with evidence (sample,
advertised model list, and the never-add-function_call invariant),
and the built-in profile registry (AGENTS.md testing expectations for
profile changes).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ozzgraph.profiles import (
    BUILTIN_PROFILES,
    FALLBACK_PROFILE,
    MODELS_MATCH_CONFIDENCE,
    PROBE_CONFIDENCE_THRESHOLD,
    PROBE_MATCH_CONFIDENCE,
    PROTOCOL_FUNCTION_CALL,
    PROTOCOL_JSON,
    PROTOCOL_TERMINAL,
    PROTOCOL_THREE_LINE,
    ModelProfile,
    discover_profile,
    probe_protocol,
    profile_for_model_id,
)

_JSON_SAMPLE = '{"kind": "run", "payload": "ls -la", "rationale": "list the dir"}'
_THREE_LINE_SAMPLE = "THOUGHT: list the dir\nACTION: run\nPAYLOAD: ls -la"


# ---------------------------------------------------------------------------
# profile_for_model_id
# ---------------------------------------------------------------------------


def test_profile_for_model_id_known_prefixes() -> None:
    """Known prefixes map to the right built-in family."""
    assert profile_for_model_id("gpt-4o").family == "gpt"
    assert profile_for_model_id("deepseek-v4-flash-0731").family == "deepseek"
    assert profile_for_model_id("claude-3").family == "claude"
    assert profile_for_model_id("llama-3").family == "llama"
    # Exact family names and digit/no-separator suffixes also match.
    assert profile_for_model_id("gpt").family == "gpt"
    assert profile_for_model_id("llama3.1:8b").family == "llama"


def test_profile_for_model_id_returns_builtin_object() -> None:
    """Matched ids return the registered built-in profile itself."""
    assert profile_for_model_id("gpt-4o") is BUILTIN_PROFILES["gpt"]
    assert profile_for_model_id("claude-3") is BUILTIN_PROFILES["claude"]


def test_profile_for_model_id_case_insensitive() -> None:
    """Matching lowercases the model id before prefix matching."""
    assert profile_for_model_id("GPT-4o").family == "gpt"
    assert profile_for_model_id("Claude-3").family == "claude"
    assert profile_for_model_id("DeepSeek-V4").family == "deepseek"
    assert profile_for_model_id("LLAMA-3.1").family == "llama"


def test_profile_for_model_id_unknown_returns_low_confidence_fallback() -> None:
    """Unknown ids map to the fallback with confidence below the probe threshold."""
    profile = profile_for_model_id("totally-unknown-model")
    assert profile is FALLBACK_PROFILE
    assert profile.family == "fallback"
    assert profile.confidence < 1.0
    assert profile.confidence < PROBE_CONFIDENCE_THRESHOLD
    assert profile.protocols == {PROTOCOL_TERMINAL}


def test_profile_for_model_id_never_raises_on_garbage() -> None:
    """Garbage input is data, never an exception."""
    for garbage in ["", "   ", "!!!", "123", "\x00\x01", "gptool", "claudia"]:
        profile = profile_for_model_id(garbage)
        assert profile.family == "fallback"


def test_profile_for_model_id_prefix_boundary_rule() -> None:
    """A prefix only matches when followed by a non-letter."""
    assert profile_for_model_id("gptool").family == "fallback"
    assert profile_for_model_id("claudia").family == "fallback"
    assert profile_for_model_id("gpt-4o-mini").family == "gpt"


def test_profile_for_model_id_custom_registry() -> None:
    """A caller-supplied registry overrides the built-ins for lookups."""

    custom = ModelProfile(
        family="custom",
        protocols=frozenset({PROTOCOL_TERMINAL}),
        context_soft_limit=1000,
        output_token_limit=500,
        supported_roles=["user", "assistant"],
        max_advertised_skills=1,
        failure_behavior="abort_turn",
        confidence=0.5,
    )
    # The registry is keyed by family: "gpt-4o" maps to the "gpt"
    # family, so a registry containing that family key serves it.
    assert profile_for_model_id("gpt-4o", profiles={"gpt": custom}) is custom
    # Unmatched ids fall back to the supplied registry's "fallback"
    # entry when present, else the module-level fallback.
    assert profile_for_model_id("nope", profiles={"custom": custom}) is FALLBACK_PROFILE
    assert profile_for_model_id("nope", profiles={"custom": custom, "fallback": custom}) is custom


# ---------------------------------------------------------------------------
# probe_protocol
# ---------------------------------------------------------------------------


def test_probe_protocol_json() -> None:
    """A JSON object with the action shape's kind key is json."""
    assert probe_protocol(_JSON_SAMPLE) == PROTOCOL_JSON
    assert probe_protocol(_JSON_SAMPLE + "\n\n") == PROTOCOL_JSON
    assert probe_protocol('  {"kind": "think"}  ') == PROTOCOL_JSON


def test_probe_protocol_json_without_kind_is_not_json() -> None:
    """An object lacking the expected kind key is not JSON-protocol evidence."""
    assert probe_protocol('{"action": "run"}') == PROTOCOL_TERMINAL
    assert probe_protocol('{"thoughts": "hmm"}') == PROTOCOL_TERMINAL


def test_probe_protocol_three_line() -> None:
    """Exactly THOUGHT/ACTION/PAYLOAD lines is three_line."""
    assert probe_protocol(_THREE_LINE_SAMPLE) == PROTOCOL_THREE_LINE
    assert probe_protocol(_THREE_LINE_SAMPLE + "\n") == PROTOCOL_THREE_LINE


def test_probe_protocol_three_line_is_strict() -> None:
    """Near-misses (wrong labels, order, or line count) are not three_line."""
    assert probe_protocol("THOUGHT: a\nACTION: run") == PROTOCOL_TERMINAL
    assert probe_protocol("ACTION: run\nTHOUGHT: a\nPAYLOAD: ls") == PROTOCOL_TERMINAL
    assert probe_protocol("Thought: a\nAction: run\nPayload: ls") == PROTOCOL_TERMINAL
    assert probe_protocol("1. THOUGHT: a\n2. ACTION: run\n3. PAYLOAD: ls") == PROTOCOL_TERMINAL
    assert probe_protocol("THOUGHT: a\nACTION:\nPAYLOAD: ls") == PROTOCOL_TERMINAL


def test_probe_protocol_terminal() -> None:
    """Plain text — commands and prose — is terminal."""
    assert probe_protocol("ls -la") == PROTOCOL_TERMINAL
    assert probe_protocol("Let me list the directory to see what is there.") == PROTOCOL_TERMINAL
    assert probe_protocol("line one\nline two\nline three\nline four") == PROTOCOL_TERMINAL
    assert probe_protocol('{"no": "kind"}') == PROTOCOL_TERMINAL
    assert probe_protocol("[1, 2, 3]") == PROTOCOL_TERMINAL


def test_probe_protocol_ambiguous_returns_none() -> None:
    """Empty, whitespace-only, and binary samples carry no signal."""
    assert probe_protocol("") is None
    assert probe_protocol("   \n\t ") is None
    assert probe_protocol("\x00\x01\x02") is None


def test_probe_protocol_never_raises_on_hostile_input() -> None:
    """Broken JSON, pathological nesting, and megabyte blobs never raise."""
    assert probe_protocol("this is not json {") == PROTOCOL_TERMINAL
    assert probe_protocol("[" * 5000 + "]" * 5000) == PROTOCOL_TERMINAL
    blob = '{"kind": "run", "payload": "' + "x" * 100_000 + '"}'
    assert probe_protocol(blob) == PROTOCOL_TERMINAL  # capped, truncated JSON
    assert probe_protocol("x" * 1_000_000) == PROTOCOL_TERMINAL


def test_probe_protocol_deterministic() -> None:
    """The same sample always yields the same protocol."""
    for sample in [_JSON_SAMPLE, _THREE_LINE_SAMPLE, "ls -la", "", "garbage {"]:
        assert probe_protocol(sample) == probe_protocol(sample)


# ---------------------------------------------------------------------------
# discover_profile
# ---------------------------------------------------------------------------


def test_discover_profile_no_evidence_keeps_low_confidence() -> None:
    """No sample and no model list: base (low) confidence stands."""
    profile = discover_profile("totally-unknown-model")
    assert profile.family == "fallback"
    assert profile.confidence == FALLBACK_PROFILE.confidence
    assert profile.confidence < PROBE_CONFIDENCE_THRESHOLD
    assert profile.protocols == {PROTOCOL_TERMINAL}


def test_discover_profile_known_family_unaffected() -> None:
    """High-confidence family mappings stand without refinement."""
    profile = discover_profile("gpt-4o")
    assert profile.family == "gpt"
    assert profile.confidence == BUILTIN_PROFILES["gpt"].confidence
    assert profile.protocols == BUILTIN_PROFILES["gpt"].protocols


def test_discover_profile_sample_refines_fallback() -> None:
    """A JSON sample adds the json protocol and bumps confidence."""
    profile = discover_profile("totally-unknown-model", sample=_JSON_SAMPLE)
    assert profile.protocols == {PROTOCOL_TERMINAL, PROTOCOL_JSON}
    assert profile.confidence == pytest.approx(FALLBACK_PROFILE.confidence + PROBE_MATCH_CONFIDENCE)


def test_discover_profile_three_line_sample_refines_fallback() -> None:
    """A three-line sample adds the three_line protocol and bumps confidence."""
    profile = discover_profile("totally-unknown-model", sample=_THREE_LINE_SAMPLE)
    assert profile.protocols == {PROTOCOL_TERMINAL, PROTOCOL_THREE_LINE}
    assert profile.confidence == pytest.approx(FALLBACK_PROFILE.confidence + PROBE_MATCH_CONFIDENCE)


def test_discover_profile_plain_text_sample_no_refinement() -> None:
    """Plain text confirms a protocol the fallback already has: no change."""
    profile = discover_profile("totally-unknown-model", sample="ls -la")
    assert profile.protocols == {PROTOCOL_TERMINAL}
    assert profile.confidence == FALLBACK_PROFILE.confidence


def test_discover_profile_models_evidence() -> None:
    """A listed model id bumps confidence slightly, still below threshold."""
    profile = discover_profile("totally-unknown-model", models=["totally-unknown-model"])
    assert profile.confidence == pytest.approx(
        FALLBACK_PROFILE.confidence + MODELS_MATCH_CONFIDENCE
    )
    assert profile.confidence < PROBE_CONFIDENCE_THRESHOLD
    # Case-insensitive, and prefix matching works in either direction.
    assert discover_profile(
        "totally-unknown-model", models=["TOTALLY-UNKNOWN-MODEL"]
    ).confidence == pytest.approx(FALLBACK_PROFILE.confidence + MODELS_MATCH_CONFIDENCE)
    # Prefix matching also works when the advertised entry is a
    # snapshot of the run's model id (either direction).
    assert discover_profile(
        "totally-unknown-model", models=["totally-unknown-model-2024-05-13"]
    ).confidence == pytest.approx(FALLBACK_PROFILE.confidence + MODELS_MATCH_CONFIDENCE)


def test_discover_profile_models_no_match_no_change() -> None:
    """A model list that does not contain the id is no evidence."""
    profile = discover_profile("totally-unknown-model", models=["gpt-4o"])
    assert profile.confidence == FALLBACK_PROFILE.confidence


def test_discover_profile_never_adds_function_call() -> None:
    """function_call is never assumed, with or without evidence."""
    for profile in (
        discover_profile("totally-unknown-model", sample=_JSON_SAMPLE),
        discover_profile("totally-unknown-model", models=["totally-unknown-model"]),
        discover_profile("totally-unknown-model"),
    ):
        assert PROTOCOL_FUNCTION_CALL not in profile.protocols


def test_discover_profile_combined_evidence() -> None:
    """Model-list and sample evidence combine."""
    profile = discover_profile(
        "totally-unknown-model", sample=_JSON_SAMPLE, models=["totally-unknown-model"]
    )
    assert profile.protocols == {PROTOCOL_TERMINAL, PROTOCOL_JSON}
    assert profile.confidence == pytest.approx(
        FALLBACK_PROFILE.confidence + MODELS_MATCH_CONFIDENCE + PROBE_MATCH_CONFIDENCE
    )


def test_discover_profile_does_not_mutate_builtins() -> None:
    """Discovery returns a copy; the built-in registry never changes."""
    discover_profile("totally-unknown-model", sample=_JSON_SAMPLE)
    assert FALLBACK_PROFILE.protocols == {PROTOCOL_TERMINAL}
    assert FALLBACK_PROFILE.confidence == 0.3
    assert BUILTIN_PROFILES["fallback"].protocols == {PROTOCOL_TERMINAL}
    assert BUILTIN_PROFILES["gpt"].protocols == BUILTIN_PROFILES["gpt"].protocols


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------


def test_builtin_profiles_every_family_present() -> None:
    """The registry holds every declared family and the fallback."""
    assert set(BUILTIN_PROFILES) == {"gpt", "claude", "deepseek", "llama", "fallback"}
    assert BUILTIN_PROFILES["fallback"] is FALLBACK_PROFILE


def test_fallback_profile_is_conservative() -> None:
    """Fallback: terminal only, no function call, minimal assumptions."""
    assert FALLBACK_PROFILE.protocols == {PROTOCOL_TERMINAL}
    assert PROTOCOL_FUNCTION_CALL not in FALLBACK_PROFILE.protocols
    assert FALLBACK_PROFILE.temperature is None
    assert FALLBACK_PROFILE.supported_roles == ["user", "assistant"]
    assert FALLBACK_PROFILE.max_advertised_skills == 0
    assert FALLBACK_PROFILE.failure_behavior == "abort_turn"
    assert FALLBACK_PROFILE.confidence < 1.0


def test_no_builtin_profile_assumes_function_call() -> None:
    """No built-in profile declares function_call (AGENTS.md)."""
    for profile in BUILTIN_PROFILES.values():
        assert PROTOCOL_FUNCTION_CALL not in profile.protocols


def test_builtin_profiles_validate() -> None:
    """Every built-in profile round-trips through pydantic."""
    for profile in BUILTIN_PROFILES.values():
        assert profile.family
        assert profile.context_soft_limit > 0
        assert profile.output_token_limit > 0
        assert 0.0 <= profile.confidence <= 1.0
        assert profile.protocols
        assert profile.supported_roles


def test_profile_round_trip() -> None:
    """model_dump / model_dump_json round-trip to an equal profile."""
    for profile in BUILTIN_PROFILES.values():
        assert ModelProfile.model_validate(profile.model_dump()) == profile
        assert ModelProfile.model_validate_json(profile.model_dump_json()) == profile


def test_profile_serialization_is_deterministic() -> None:
    """Protocols serialize in sorted order (byte-stable dumps)."""
    dumped = BUILTIN_PROFILES["gpt"].model_dump()
    assert dumped["protocols"] == sorted(BUILTIN_PROFILES["gpt"].protocols)
    assert BUILTIN_PROFILES["gpt"].model_dump_json() == BUILTIN_PROFILES["gpt"].model_dump_json()


def test_profile_rejects_invalid_values() -> None:
    """Invalid profile values fail loudly (pydantic validation)."""
    base = BUILTIN_PROFILES["gpt"].model_dump()
    overrides: list[dict[str, object]] = [
        {"confidence": 1.5},
        {"context_soft_limit": 0},
        {"output_token_limit": -1},
        {"max_advertised_skills": -1},
        {"failure_behavior": "explode"},
        {"family": ""},
        {"supported_roles": []},
        {"bogus": 1},  # extra="forbid"
    ]
    for extra in overrides:
        with pytest.raises(ValidationError):
            ModelProfile.model_validate({**base, **extra})
