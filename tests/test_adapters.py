"""Tests for the model adapter interfaces (PR13).

Covers the adapter registry contract (:func:`register_adapter` /
:func:`adapter_for` failure and success paths), the normalized
:class:`ParsedAction` shape (validation, extra-forbid, serialization),
:class:`AdapterParseError` carrying protocol + detail, and the
:class:`ModelAdapter` abstract base contract: profile-derived limits
and the ability of concrete subclasses to override them (AGENTS.md
testing expectations for adapter changes).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from ozzgraph.adapters import (
    ADAPTERS,
    AdapterError,
    AdapterParseError,
    AdapterRegistryError,
    ModelAdapter,
    ParsedAction,
    adapter_for,
    register_adapter,
)
from ozzgraph.profiles import GPT_PROFILE, PROTOCOL_TERMINAL


class _MinimalAdapter(ModelAdapter):
    """Minimal concrete adapter for registry and property tests."""

    @property
    def protocol(self) -> str:
        return PROTOCOL_TERMINAL

    def compile_prompt(
        self,
        *,
        mission: str,
        graph_summary: str,
        transcript_tail: str,
        skills: Sequence[str],
        output_contract: str,
    ) -> str:
        return f"{mission}\n{output_contract}"

    def parse(self, completion: str) -> ParsedAction:
        return ParsedAction(kind="run", raw=completion)

    def repair(self, completion: str, error: AdapterParseError) -> str | None:
        return None


class _StrictOutputAdapter(_MinimalAdapter):
    """A terminal adapter capping output below the profile's limit."""

    @property
    def output_token_limit(self) -> int:
        return 512


# ---------------------------------------------------------------------------
# register_adapter / adapter_for registry
# ---------------------------------------------------------------------------


def test_model_adapter_is_abstract() -> None:
    """The ABC cannot be instantiated without the protocol methods."""
    with pytest.raises(TypeError):
        ModelAdapter(GPT_PROFILE)  # type: ignore[abstract]


def test_register_adapter_rejects_empty_protocol() -> None:
    """An empty protocol is a registry error, never a silent accept."""
    with pytest.raises(AdapterRegistryError, match="non-empty"):
        register_adapter("", _MinimalAdapter)


def test_register_adapter_rejects_non_subclass() -> None:
    """A class that is not a ModelAdapter subclass is rejected."""
    with pytest.raises(AdapterRegistryError, match="ModelAdapter subclass"):
        register_adapter("terminal", object)  # type: ignore[arg-type]


def test_register_adapter_rejects_duplicate() -> None:
    """Registering twice for the same protocol fails loudly."""
    register_adapter("terminal", _MinimalAdapter)
    try:
        with pytest.raises(AdapterRegistryError, match="already registered"):
            register_adapter("terminal", _MinimalAdapter)
    finally:
        ADAPTERS.pop("terminal", None)


def test_adapter_for_returns_registered_class() -> None:
    """adapter_for resolves the exact class registered for a protocol."""
    register_adapter("terminal", _MinimalAdapter)
    try:
        assert adapter_for("terminal") is _MinimalAdapter
    finally:
        ADAPTERS.pop("terminal", None)


def test_adapter_for_raises_on_missing_protocol() -> None:
    """An unregistered protocol is a registry error, never None."""
    with pytest.raises(AdapterRegistryError, match="no adapter registered"):
        adapter_for("no-such-protocol")


# ---------------------------------------------------------------------------
# ParsedAction
# ---------------------------------------------------------------------------


def test_parsed_action_accepts_minimal_shape() -> None:
    """Only kind and raw are required; payload/rationale are optional."""
    action = ParsedAction(kind="run", raw="ls -la")
    assert action.kind == "run"
    assert action.raw == "ls -la"
    assert action.payload is None
    assert action.rationale is None


def test_parsed_action_rejects_extra_fields() -> None:
    """extra='forbid': unknown keys fail loudly (AGENTS.md)."""
    with pytest.raises(ValidationError):
        ParsedAction(kind="run", raw="ls", bogus=1)  # type: ignore[call-arg]


def test_parsed_action_requires_non_empty_kind() -> None:
    """An empty kind is schema-invalid (min_length=1)."""
    with pytest.raises(ValidationError):
        ParsedAction(kind="", raw="ls")


def test_parsed_action_requires_raw() -> None:
    """raw is required: the original completion is never lost."""
    with pytest.raises(ValidationError):
        ParsedAction(kind="run")  # type: ignore[call-arg]


def test_parsed_action_serializes() -> None:
    """model_dump / model_dump_json round-trip to an equal action."""
    action = ParsedAction(
        kind="run",
        payload="ls -la",
        rationale="list the dir",
        raw='{"kind": "run"}',
    )
    assert action.model_dump() == {
        "kind": "run",
        "payload": "ls -la",
        "rationale": "list the dir",
        "raw": '{"kind": "run"}',
    }
    assert ParsedAction.model_validate_json(action.model_dump_json()) == action


# ---------------------------------------------------------------------------
# AdapterParseError
# ---------------------------------------------------------------------------


def test_adapter_parse_error_carries_protocol_and_detail() -> None:
    """Parse failures name the failing protocol and a human-readable detail."""
    error = AdapterParseError(protocol="json", detail="missing kind")
    assert error.protocol == "json"
    assert error.detail == "missing kind"
    assert str(error) == "missing kind"
    assert isinstance(error, AdapterError)
    assert isinstance(error, RuntimeError)


# ---------------------------------------------------------------------------
# ModelAdapter profile-derived properties
# ---------------------------------------------------------------------------


def test_adapter_exposes_profile_derived_properties() -> None:
    """Adapter limits/roles/behavior read through from the profile."""
    adapter = _MinimalAdapter(GPT_PROFILE)
    assert adapter.protocol == PROTOCOL_TERMINAL
    assert adapter.context_soft_limit == GPT_PROFILE.context_soft_limit
    assert adapter.output_token_limit == GPT_PROFILE.output_token_limit
    assert adapter.temperature == GPT_PROFILE.temperature
    assert adapter.supported_roles == GPT_PROFILE.supported_roles
    assert adapter.max_advertised_skills == GPT_PROFILE.max_advertised_skills
    assert adapter.failure_behavior == GPT_PROFILE.failure_behavior


def test_adapter_subclass_can_override_profile_properties() -> None:
    """A protocol with stricter limits overrides the profile-derived value."""
    adapter = _StrictOutputAdapter(GPT_PROFILE)
    assert adapter.output_token_limit == 512
    # Non-overridden properties still read through from the profile.
    assert adapter.context_soft_limit == GPT_PROFILE.context_soft_limit
    assert adapter.temperature == GPT_PROFILE.temperature
