"""Tests for the strict JSON adapter (PR15).

Covers :class:`JsonAdapter`: exact-shape parse success (full action,
minimal kind-only, payloads with spaces and special characters,
whitespace tolerance, pretty-printed objects), every deviation raising
:class:`AdapterParseError` with protocol ``"json"`` (empty input,
unparseable JSON, pathological nesting, non-object top levels,
missing/empty/non-string ``kind``, non-string ``payload``/``rationale``,
extra keys — with a raw pydantic error never escaping), prompt
compilation including the JSON format template and the output
contract, the deterministic never-raising repair strategy (code-fenced
JSON, prose-wrapped JSON, unfixable garbage), import-time registry
registration, and probe/parser consistency across model profiles.
"""

from __future__ import annotations

import pytest

from ozzgraph.adapters import (
    ADAPTERS,
    AdapterParseError,
    JsonAdapter,
    adapter_for,
)
from ozzgraph.profiles import (
    CLAUDE_PROFILE,
    DEEPSEEK_PROFILE,
    GPT_PROFILE,
    PROTOCOL_JSON,
    ModelProfile,
    probe_protocol,
)


def _adapter() -> JsonAdapter:
    """A JSON adapter over the GPT profile."""
    return JsonAdapter(GPT_PROFILE)


# ---------------------------------------------------------------------------
# protocol + profile
# ---------------------------------------------------------------------------


def test_json_protocol_name() -> None:
    """The adapter declares the JSON protocol family."""
    assert _adapter().protocol == PROTOCOL_JSON


def test_json_reads_limits_from_profile() -> None:
    """Protocol limits read through from the constructed profile."""
    adapter = _adapter()
    assert adapter.output_token_limit == GPT_PROFILE.output_token_limit
    assert adapter.context_soft_limit == GPT_PROFILE.context_soft_limit
    assert adapter.failure_behavior == GPT_PROFILE.failure_behavior


# ---------------------------------------------------------------------------
# parse success paths (format-compliance fixtures)
# ---------------------------------------------------------------------------

FULL_ACTION_JSON = (
    '{"kind": "run", "payload": "nmap -sV 127.0.0.1", "rationale": "port 22 is open"}'
)


def test_json_parses_full_action() -> None:
    """kind, payload, and rationale map onto the normalized action."""
    action = _adapter().parse(FULL_ACTION_JSON)
    assert action.kind == "run"
    assert action.payload == "nmap -sV 127.0.0.1"
    assert action.rationale == "port 22 is open"
    assert action.raw == FULL_ACTION_JSON


def test_json_parses_minimal_kind_only_action() -> None:
    """kind alone is a complete action; payload/rationale default None."""
    action = _adapter().parse('{"kind": "think"}')
    assert action.kind == "think"
    assert action.payload is None
    assert action.rationale is None


def test_json_payload_with_spaces_and_special_chars() -> None:
    """Payload values keep spaces, quotes, and shell metacharacters."""
    completion = (
        '{"kind": "run", "payload": "echo \\"hi there\\" && ls -la /tmp", '
        '"rationale": "quotes and spaces survive"}'
    )
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == 'echo "hi there" && ls -la /tmp'
    assert action.rationale == "quotes and spaces survive"


def test_json_tolerates_surrounding_whitespace() -> None:
    """Whitespace around the object is ignored, as in the probe."""
    completion = '  \n\t{"kind": "run", "payload": "ls -la"} \n '
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "ls -la"


def test_json_parses_pretty_printed_object() -> None:
    """Newlines and indentation inside the object parse normally."""
    completion = '{\n  "kind": "submit",\n  "rationale": "flag found"\n}'
    action = _adapter().parse(completion)
    assert action.kind == "submit"
    assert action.rationale == "flag found"


# ---------------------------------------------------------------------------
# parse error paths (malformed-output fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "completion",
    [
        "",  # empty
        "   ",  # whitespace-only
        "\n\t\n",
        "{not json",  # unparseable
        '{"kind": "run", "payload": }',
        "[1, 2, 3]",  # top-level array
        '"just a string"',  # top-level string
        "42",  # top-level number
        "{}",  # missing kind
        '{"payload": "no kind here"}',
        '{"kind": ""}',  # empty-string kind
        '{"kind": 42}',  # non-string kind
        '{"kind": "run", "payload": 42}',  # non-string payload
        '{"kind": "run", "rationale": ["not", "a", "string"]}',  # non-string rationale
        '{"kind": "run", "bogus": "extra key"}',  # extra key
        '{"kind": "run", "payload": "p", "unexpected": 1}',
    ],
)
def test_json_rejects_malformed_output(completion: str) -> None:
    """Every malformed completion raises AdapterParseError for json."""
    with pytest.raises(AdapterParseError) as excinfo:
        _adapter().parse(completion)
    assert excinfo.value.protocol == PROTOCOL_JSON


def test_json_rejects_pathologically_nested_input() -> None:
    """Pathological nesting never leaks a raw RecursionError."""
    completion = "[" * 100_000 + "]" * 100_000
    with pytest.raises(AdapterParseError) as excinfo:
        _adapter().parse(completion)
    assert excinfo.value.protocol == PROTOCOL_JSON


def test_json_extra_keys_fail_via_schema_validation() -> None:
    """Unknown keys hit ParsedAction's extra='forbid' and are converted."""
    with pytest.raises(AdapterParseError) as excinfo:
        _adapter().parse('{"kind": "run", "bogus": 1}')
    assert excinfo.value.protocol == PROTOCOL_JSON
    assert "validation" in excinfo.value.detail


# ---------------------------------------------------------------------------
# compile_prompt
# ---------------------------------------------------------------------------


def test_json_prompt_includes_template_and_contract() -> None:
    """The compiled prompt carries the JSON schema and the contract."""
    contract = "One JSON object with kind, payload, and rationale."
    prompt = _adapter().compile_prompt(
        mission="m",
        graph_summary="g",
        transcript_tail="t",
        skills=["skill-a", "skill-b"],
        output_contract=contract,
    )
    assert '"kind": "<action kind>"' in prompt
    assert '"payload": "<optional string>"' in prompt
    assert '"rationale": "<optional reasoning>"' in prompt
    assert "run, think" in prompt
    assert "- skill-a" in prompt
    assert "- skill-b" in prompt
    assert prompt.endswith(contract)


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def test_json_repair_strips_code_fence() -> None:
    """A fenced JSON block is unwrapped and re-parses to the same action."""
    error = AdapterParseError(protocol=PROTOCOL_JSON, detail="n/a")
    completion = '```json\n{"kind": "run", "payload": "ls -la"}\n```'
    repaired = _adapter().repair(completion, error)
    assert repaired == '{"kind": "run", "payload": "ls -la"}'
    action = _adapter().parse(repaired)
    assert action.kind == "run"
    assert action.payload == "ls -la"


def test_json_repair_extracts_prose_wrapped_object() -> None:
    """Prose around a JSON block is dropped; the object re-parses."""
    error = AdapterParseError(protocol=PROTOCOL_JSON, detail="n/a")
    completion = 'Sure, here is my action: {"kind": "submit", "rationale": "flag"} — done.'
    repaired = _adapter().repair(completion, error)
    assert repaired == '{"kind": "submit", "rationale": "flag"}'
    action = _adapter().parse(repaired)
    assert action.kind == "submit"
    assert action.rationale == "flag"


def test_json_repair_braces_in_strings_do_not_truncate() -> None:
    """Braces inside a string value stay inside the extracted object."""
    error = AdapterParseError(protocol=PROTOCOL_JSON, detail="n/a")
    completion = 'Result: {"kind": "run", "payload": "echo {braces}"}'
    repaired = _adapter().repair(completion, error)
    assert repaired == '{"kind": "run", "payload": "echo {braces}"}'


def test_json_repair_returns_none_on_unfixable_garbage() -> None:
    """Nothing salvageable yields None, never a raise."""
    error = AdapterParseError(protocol=PROTOCOL_JSON, detail="n/a")
    adapter = _adapter()
    for bad in (
        "",
        "   ",
        "no json here",
        "the answer is 42",
        '{"kind": }',  # balanced but unparseable
        "```json\n{broken\n```",  # fenced but unparseable
        'I think {the flag} is here: {"kind": "submit"}',  # first object is prose
    ):
        assert adapter.repair(bad, error) is None


def test_json_repair_returns_none_when_already_clean() -> None:
    """A bare action object is unchanged by repair: nothing to fix."""
    error = AdapterParseError(protocol=PROTOCOL_JSON, detail="n/a")
    assert _adapter().repair('{"kind": "run"}', error) is None


# ---------------------------------------------------------------------------
# registration + probe consistency
# ---------------------------------------------------------------------------


def test_json_adapter_registered_at_import() -> None:
    """adapter_for resolves the JSON adapter after plain import."""
    assert adapter_for(PROTOCOL_JSON) is JsonAdapter
    assert ADAPTERS[PROTOCOL_JSON] is JsonAdapter


def test_json_probe_consistency() -> None:
    """A valid JSON action sample probes as the json protocol."""
    assert probe_protocol('{"kind": "run", "payload": "ls -la"}') == PROTOCOL_JSON
    assert probe_protocol('{"kind": "think"}') == PROTOCOL_JSON
    # The probe and the parser agree on what is NOT json.
    assert probe_protocol("just prose") != PROTOCOL_JSON


# ---------------------------------------------------------------------------
# model-profile regression fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile",
    [GPT_PROFILE, CLAUDE_PROFILE, DEEPSEEK_PROFILE],
    ids=["gpt", "claude", "deepseek"],
)
def test_json_adapter_works_across_model_profiles(profile: ModelProfile) -> None:
    """The JSON adapter parses identically over multiple model profiles."""
    adapter = JsonAdapter(profile)
    assert adapter.protocol == PROTOCOL_JSON
    assert adapter.failure_behavior == profile.failure_behavior
    assert adapter.output_token_limit == profile.output_token_limit
    action = adapter.parse(FULL_ACTION_JSON)
    assert action.kind == "run"
    assert action.payload == "nmap -sV 127.0.0.1"
