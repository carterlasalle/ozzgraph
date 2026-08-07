"""Tests for the terminal-native adapter (PR14).

Covers :class:`TerminalAdapter`: plain-text parse success paths
(including payloads with spaces), think-degradation for completions
without an ACTION directive, the single genuinely-unparseable case
(empty input), prompt compilation including the protocol format
instructions and the passed-through output contract, import-time
registry registration, and the never-raising minimal repair.
"""

from __future__ import annotations

import pytest

from ozzgraph.adapters import (
    ADAPTERS,
    AdapterParseError,
    TerminalAdapter,
    adapter_for,
)
from ozzgraph.profiles import GPT_PROFILE, PROTOCOL_TERMINAL


def _adapter() -> TerminalAdapter:
    """A terminal adapter over the GPT profile."""
    return TerminalAdapter(GPT_PROFILE)


# ---------------------------------------------------------------------------
# protocol + profile
# ---------------------------------------------------------------------------


def test_terminal_protocol_name() -> None:
    """The adapter declares the terminal protocol family."""
    assert _adapter().protocol == PROTOCOL_TERMINAL


def test_terminal_reads_limits_from_profile() -> None:
    """Protocol limits read through from the constructed profile."""
    adapter = _adapter()
    assert adapter.output_token_limit == GPT_PROFILE.output_token_limit
    assert adapter.context_soft_limit == GPT_PROFILE.context_soft_limit
    assert adapter.max_advertised_skills == GPT_PROFILE.max_advertised_skills


# ---------------------------------------------------------------------------
# parse success paths
# ---------------------------------------------------------------------------


def test_terminal_parses_action_directive_with_payload() -> None:
    """An ACTION line plus a PAYLOAD line parses into a run action."""
    completion = "The target exposes port 22.\nACTION: run\nPAYLOAD: nmap -sV 127.0.0.1"
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "nmap -sV 127.0.0.1"
    assert action.rationale == "The target exposes port 22."
    assert action.raw == completion


def test_terminal_payload_may_contain_spaces() -> None:
    """The payload value keeps spaces; whitespace after the colon is not
    part of the value."""
    completion = "ACTION: run\nPAYLOAD:   ls -la /tmp && echo done  "
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "ls -la /tmp && echo done"
    assert action.raw == completion


def test_terminal_action_without_payload() -> None:
    """An ACTION directive with no PAYLOAD line yields payload None."""
    completion = "I should think first.\nACTION: think"
    action = _adapter().parse(completion)
    assert action.kind == "think"
    assert action.payload is None
    assert action.rationale == "I should think first."
    assert action.raw == completion


def test_terminal_keeps_text_around_directive_as_rationale() -> None:
    """Text before and after the directive lines stays in the rationale."""
    completion = "prelude\nACTION: run\nPAYLOAD: ls -la\npostscript"
    action = _adapter().parse(completion)
    assert action.kind == "run"
    assert action.payload == "ls -la"
    assert action.rationale == "prelude\npostscript"


def test_terminal_bare_directive_has_empty_rationale() -> None:
    """A completion that is only a directive leaves an empty rationale."""
    action = _adapter().parse("ACTION: run")
    assert action.kind == "run"
    assert action.payload is None
    assert action.rationale == ""
    assert action.raw == "ACTION: run"


# ---------------------------------------------------------------------------
# think degradation (never raise on plain text)
# ---------------------------------------------------------------------------


def test_terminal_degrades_to_think_without_directive() -> None:
    """Plain text with no ACTION directive becomes a think action."""
    completion = "Nothing to act on yet; still reasoning."
    action = _adapter().parse(completion)
    assert action.kind == "think"
    assert action.rationale == completion
    assert action.payload is None
    assert action.raw == completion


def test_terminal_multiline_prose_degrades_to_think() -> None:
    """Multi-line prose, even with PAYLOAD-like lines, degrades to think."""
    completion = "First line.\nSecond line.\nPAYLOAD: no directive without ACTION"
    action = _adapter().parse(completion)
    assert action.kind == "think"
    assert action.rationale == completion


def test_terminal_bare_action_label_is_prose() -> None:
    """'ACTION:' with no value is prose, so the text degrades to think."""
    action = _adapter().parse("ACTION:")
    assert action.kind == "think"
    assert action.rationale == "ACTION:"


def test_terminal_lowercase_action_is_prose() -> None:
    """Directive labels are case-sensitive; 'action:' is not a directive."""
    action = _adapter().parse("action: run\nPAYLOAD: ls")
    assert action.kind == "think"
    assert action.rationale == "action: run\nPAYLOAD: ls"


# ---------------------------------------------------------------------------
# parse error path (the only terminal raise case)
# ---------------------------------------------------------------------------


def test_terminal_rejects_empty_completion() -> None:
    """Empty and whitespace-only completions are the sole raise case."""
    for bad in ("", "   ", "\n\t \n"):
        with pytest.raises(AdapterParseError) as excinfo:
            _adapter().parse(bad)
        assert excinfo.value.protocol == PROTOCOL_TERMINAL
        assert "empty" in excinfo.value.detail


# ---------------------------------------------------------------------------
# compile_prompt
# ---------------------------------------------------------------------------


def test_terminal_prompt_includes_format_instructions_and_contract() -> None:
    """The compiled prompt carries the directive rules and the contract."""
    contract = "Respond with ACTION: <kind> and optionally PAYLOAD: <value>."
    prompt = _adapter().compile_prompt(
        mission="Capture the flag on the isolated target.",
        graph_summary="2 services discovered.",
        transcript_tail="Last probe returned nothing.",
        skills=["recon", "exploit"],
        output_contract=contract,
    )
    assert "ACTION: <kind>" in prompt
    assert "PAYLOAD: <value>" in prompt
    assert "Capture the flag on the isolated target." in prompt
    assert "2 services discovered." in prompt
    assert "Last probe returned nothing." in prompt
    assert "- recon" in prompt
    assert "- exploit" in prompt
    assert prompt.endswith(contract)


def test_terminal_prompt_handles_empty_skills() -> None:
    """An empty skills list renders an explicit '(none)' block."""
    prompt = _adapter().compile_prompt(
        mission="m",
        graph_summary="g",
        transcript_tail="t",
        skills=[],
        output_contract="c",
    )
    assert "(none)" in prompt
    assert "OUTPUT CONTRACT\nc" in prompt


# ---------------------------------------------------------------------------
# registration + repair
# ---------------------------------------------------------------------------


def test_terminal_adapter_registered_at_import() -> None:
    """adapter_for resolves the terminal adapter after plain import."""
    assert adapter_for(PROTOCOL_TERMINAL) is TerminalAdapter
    assert ADAPTERS[PROTOCOL_TERMINAL] is TerminalAdapter


def test_terminal_repair_never_raises_and_returns_none() -> None:
    """PR14 repair for terminal is a no-op that never raises."""
    error = AdapterParseError(protocol=PROTOCOL_TERMINAL, detail="empty")
    adapter = _adapter()
    assert adapter.repair("", error) is None
    assert adapter.repair("ACTION: run", error) is None
    assert adapter.repair("plain text", error) is None
