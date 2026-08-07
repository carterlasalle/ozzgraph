"""Tests for the strict three-line adapter (PR14).

Covers :class:`ThreeLineAdapter`: exact-format parse success (payloads
with spaces pass through, blank lines are ignored probe-consistently),
every deviation raising :class:`AdapterParseError` (empty input, wrong
line count, wrong label order, missing/empty values, extra lines),
kind passthrough without vocabulary validation, prompt compilation
including the strict template and the output contract, import-time
registry registration, and the minimal never-raising whitespace-trim
repair.
"""

from __future__ import annotations

import pytest

from ozzgraph.adapters import (
    ADAPTERS,
    AdapterParseError,
    ThreeLineAdapter,
    adapter_for,
)
from ozzgraph.profiles import GPT_PROFILE, PROTOCOL_THREE_LINE


def _adapter() -> ThreeLineAdapter:
    """A three-line adapter over the GPT profile."""
    return ThreeLineAdapter(GPT_PROFILE)


# ---------------------------------------------------------------------------
# protocol + profile
# ---------------------------------------------------------------------------


def test_three_line_protocol_name() -> None:
    """The adapter declares the three-line protocol family."""
    assert _adapter().protocol == PROTOCOL_THREE_LINE


def test_three_line_reads_limits_from_profile() -> None:
    """Protocol limits read through from the constructed profile."""
    adapter = _adapter()
    assert adapter.output_token_limit == GPT_PROFILE.output_token_limit
    assert adapter.context_soft_limit == GPT_PROFILE.context_soft_limit
    assert adapter.failure_behavior == GPT_PROFILE.failure_behavior


# ---------------------------------------------------------------------------
# parse success paths
# ---------------------------------------------------------------------------


def test_three_line_parses_full_action() -> None:
    """The three labeled lines map to kind, payload, and rationale."""
    completion = "THOUGHT: port 22 is open\nACTION: run\nPAYLOAD: nmap -sV 127.0.0.1"
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "nmap -sV 127.0.0.1"
    assert action.rationale == "port 22 is open"
    assert action.raw == completion


def test_three_line_payload_may_contain_spaces() -> None:
    """A payload value containing spaces is preserved."""
    completion = "THOUGHT: t\nACTION: run\nPAYLOAD: ls -la /tmp && echo done"
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "ls -la /tmp && echo done"
    assert action.rationale == "t"


def test_three_line_strips_surrounding_whitespace() -> None:
    """Whitespace around the completion and after each colon is dropped."""
    completion = "  THOUGHT:  t  \n  ACTION:  run  \n  PAYLOAD:  p  "
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "p"
    assert action.rationale == "t"


def test_three_line_ignores_blank_lines() -> None:
    """Blank lines are not counted: three non-empty lines parse, exactly
    as in the conservative probe's shape check."""
    completion = "\nTHOUGHT: t\n\nACTION: run\n\nPAYLOAD: p\n"
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "p"
    assert action.rationale == "t"


def test_three_line_kinds_pass_through_unvalidated() -> None:
    """Any schema-valid kind verb passes through; executor policy (PR20)
    owns vocabulary validation."""
    for kind in ("run", "think", "submit", "hint", "exit"):
        action = _adapter().parse(f"THOUGHT: t\nACTION: {kind}\nPAYLOAD: p")
        assert action.kind == kind


# ---------------------------------------------------------------------------
# parse error paths
# ---------------------------------------------------------------------------


def test_three_line_rejects_empty_completion() -> None:
    """Empty and whitespace-only completions are unparseable."""
    for bad in ("", "   ", "\n\t\n"):
        with pytest.raises(AdapterParseError) as excinfo:
            _adapter().parse(bad)
        assert excinfo.value.protocol == PROTOCOL_THREE_LINE
        assert "empty" in excinfo.value.detail


def test_three_line_rejects_wrong_line_count() -> None:
    """Two or four non-empty lines are not the strict three-line format."""
    for bad in (
        "THOUGHT: t\nACTION: run",
        "THOUGHT: t\nACTION: run\nPAYLOAD: p\nTHOUGHT: again",
    ):
        with pytest.raises(AdapterParseError) as excinfo:
            _adapter().parse(bad)
        assert excinfo.value.protocol == PROTOCOL_THREE_LINE
        assert "3 non-empty lines" in excinfo.value.detail


def test_three_line_rejects_wrong_label_order() -> None:
    """Labels must appear in THOUGHT, ACTION, PAYLOAD order."""
    with pytest.raises(AdapterParseError) as excinfo:
        _adapter().parse("ACTION: run\nTHOUGHT: t\nPAYLOAD: p")
    assert excinfo.value.protocol == PROTOCOL_THREE_LINE
    assert "expected THOUGHT" in excinfo.value.detail


def test_three_line_rejects_missing_value() -> None:
    """A label with no value (or only whitespace) is a deviation."""
    for bad in (
        "THOUGHT:\nACTION: run\nPAYLOAD: p",
        "THOUGHT: t\nACTION:   \nPAYLOAD: p",
        "THOUGHT: t\nACTION: run\nPAYLOAD:",
    ):
        with pytest.raises(AdapterParseError) as excinfo:
            _adapter().parse(bad)
        assert excinfo.value.protocol == PROTOCOL_THREE_LINE
        assert ": <value>" in excinfo.value.detail


def test_three_line_rejects_unknown_or_repeated_label() -> None:
    """A label outside the trio, or a repeated label, fails loudly."""
    for bad in (
        "THOUGHT: t\nACTION: run\nEXTRA: p",
        "THOUGHT: a\nTHOUGHT: b\nACTION: run",
    ):
        with pytest.raises(AdapterParseError) as excinfo:
            _adapter().parse(bad)
        assert excinfo.value.protocol == PROTOCOL_THREE_LINE
        assert "expected" in excinfo.value.detail


def test_three_line_rejects_lowercase_labels() -> None:
    """Labels are case-sensitive: 'thought:' is not 'THOUGHT:'."""
    with pytest.raises(AdapterParseError):
        _adapter().parse("thought: t\nACTION: run\nPAYLOAD: p")


# ---------------------------------------------------------------------------
# compile_prompt
# ---------------------------------------------------------------------------


def test_three_line_prompt_includes_template_and_contract() -> None:
    """The compiled prompt carries the strict template and the contract."""
    contract = "Exactly three lines: THOUGHT, ACTION, PAYLOAD."
    prompt = _adapter().compile_prompt(
        mission="m",
        graph_summary="g",
        transcript_tail="t",
        skills=["skill-a", "skill-b"],
        output_contract=contract,
    )
    assert "THOUGHT: <your reasoning>" in prompt
    assert "ACTION: <kind>" in prompt
    assert "PAYLOAD: <value>" in prompt
    assert "- skill-a" in prompt
    assert "- skill-b" in prompt
    assert prompt.endswith(contract)


# ---------------------------------------------------------------------------
# registration + repair
# ---------------------------------------------------------------------------


def test_three_line_adapter_registered_at_import() -> None:
    """adapter_for resolves the three-line adapter after plain import."""
    assert adapter_for(PROTOCOL_THREE_LINE) is ThreeLineAdapter
    assert ADAPTERS[PROTOCOL_THREE_LINE] is ThreeLineAdapter


def test_three_line_repair_trims_whitespace_or_returns_none() -> None:
    """PR14 repair trims surrounding whitespace and never raises."""
    error = AdapterParseError(protocol=PROTOCOL_THREE_LINE, detail="n/a")
    adapter = _adapter()
    assert (
        adapter.repair("  THOUGHT: t\nACTION: run\nPAYLOAD: p  ", error)
        == "THOUGHT: t\nACTION: run\nPAYLOAD: p"
    )
    assert adapter.repair("THOUGHT: t\nACTION: run\nPAYLOAD: p", error) is None
    assert adapter.repair("", error) is None
    assert adapter.repair("   ", error) is None
