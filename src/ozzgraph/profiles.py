"""Model profiles and protocol probing for OzzGraph (PR13).

Implements the PROFILE layer and the model-discovery half of the
Model Adapter Requirements (docs/TECHNICAL_REQUIREMENTS.md, "Model
Discovery" and "Model Adapter Requirements"; docs/ARCHITECTURE.md,
"Model Adapters"; PRD goal 2): a :class:`ModelProfile` captures
everything an adapter needs to know about a model family — protocol
families, context/output limits, temperature, supported roles,
advertised-skill budget, failure behavior, and a confidence score —
plus deterministic mapping from a model id to a profile and a pure,
side-effect-free protocol probe that classifies ONE untrusted
completion sample.

Design rules (AGENTS.md):

- Deterministic and pure: profile lookup, probing, and discovery do no
  I/O, keep no state, and always return the same result for the same
  input.
- Sample output is untrusted: probing never raises on hostile input
  (broken JSON, pathological nesting, control bytes, megabyte blobs);
  it returns the protocol the sample appears to conform to, or None
  when there is no signal.
- Conservative by default: unknown model ids map to
  :data:`FALLBACK_PROFILE` (plain text only, low confidence — which
  keeps protocol probing in the discovery loop). No built-in profile
  declares ``function_call``: that protocol requires explicit,
  evidenced registration and is never assumed from a text sample
  (AGENTS.md rule: "never assume function-call support").
  :func:`discover_profile` can therefore never add ``function_call``.
- The registry (:data:`BUILTIN_PROFILES`) is a plain deterministic
  dict keyed by family, populated at import. It is explicitly not a
  plugin system (AGENTS.md rule #10): adding a profile means adding
  one dict entry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

#: Protocol family names (docs/ARCHITECTURE.md, "Model Adapters").
PROTOCOL_TERMINAL = "terminal"
PROTOCOL_THREE_LINE = "three_line"
PROTOCOL_JSON = "json"
PROTOCOL_FUNCTION_CALL = "function_call"

#: Family name for unknown model ids: maps to :data:`FALLBACK_PROFILE`.
FALLBACK_FAMILY = "fallback"

#: When a base profile's confidence is below this, discovery applies
#: evidence (the advertised model list, a completion sample) to refine
#: the profile.
PROBE_CONFIDENCE_THRESHOLD = 0.5

#: Confidence added when the model id appears in the provider's
#: advertised model list (existence evidence).
MODELS_MATCH_CONFIDENCE = 0.1

#: Confidence added when a completion sample confirms a protocol the
#: profile did not yet declare (format evidence).
PROBE_MATCH_CONFIDENCE = 0.2

#: Probing examines at most this many characters of a sample: a
#: completion's action section is at the start, and hostile megabyte
#: output must stay bounded (never parsed in full).
PROBE_SAMPLE_LIMIT = 4_096

#: Conservative failure policies an adapter may declare. Repair
#: strategies themselves are PR15; this PR only declares the field.
FailureBehavior = Literal["repair_retry", "abort_turn"]

#: Deterministic (prefix, family) pairs, longest prefix first. Model
#: ids are lowercased before matching, and a prefix matches only when
#: followed by a non-letter (``"gpt-4o"``, ``"llama3.1:8b"``,
#: ``"claude-3"``) so lookalike ids like ``"gptool"`` never silently
#: map to the wrong family.
FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("claude", "claude"),
    ("llama", "llama"),
    ("gpt", "gpt"),
)


class ModelProfile(BaseModel):
    """Everything an adapter needs to know about one model family.

    Captures the Model Adapter Requirements
    (docs/TECHNICAL_REQUIREMENTS.md, "Model Adapter Requirements") plus
    the fields model discovery needs: ``protocols`` is the set of
    protocol families this model family is known/assumed to support
    (``"terminal"``, ``"three_line"``, ``"json"``, ``"function_call"``),
    and ``confidence`` is how sure we are the profile matches the
    model — low confidence triggers protocol probing.

    ``failure_behavior`` is declared conservatively: repair strategies
    themselves land in PR15, this field only names the policy
    (``"repair_retry"`` for known families, ``"abort_turn"`` for the
    unknown-model fallback).
    """

    model_config = ConfigDict(extra="forbid")

    family: str = Field(min_length=1)
    protocols: frozenset[str] = Field(min_length=1)
    context_soft_limit: int = Field(ge=1)
    output_token_limit: int = Field(ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    supported_roles: list[str] = Field(min_length=1)
    max_advertised_skills: int = Field(ge=0)
    failure_behavior: FailureBehavior
    confidence: float = Field(ge=0.0, le=1.0)

    @field_serializer("protocols")
    def _serialize_protocols(self, protocols: frozenset[str]) -> list[str]:
        """Serialize protocols in sorted order (deterministic dumps).

        Sets iterate in hash order, which varies across processes for
        ``str`` elements; serializing sorted keeps profile dumps
        byte-for-byte deterministic (AGENTS.md: prefer deterministic
        code).
        """
        return sorted(protocols)


#: One line of the strict three-line action format: ``LABEL: value``.
_THREE_LINE_RE = re.compile(r"^(THOUGHT|ACTION|PAYLOAD):\s*(.+)$")


def _probe_json(text: str) -> bool:
    """True when ``text`` parses as the normalized action JSON shape.

    The probe's "expected keys" are the normalized action shape's
    required key: a JSON object carrying a non-empty ``kind`` (the
    :class:`~ozzgraph.adapters.ParsedAction` contract). Anything else —
    arrays, strings, objects without ``kind`` — is not JSON-protocol
    evidence. Broken or pathologically nested JSON is caught and
    reported as no-match (never raised).
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    kind = payload.get("kind")
    return isinstance(kind, str) and kind != ""


def _probe_three_line(text: str) -> bool:
    """True when ``text`` is exactly the strict three-line format.

    Exactly three non-empty lines, in order ``THOUGHT: ...``,
    ``ACTION: ...``, ``PAYLOAD: ...``. This is the probe's
    conservative shape check only; the concrete three-line parser
    (PR14) is authoritative for the format.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 3:
        return False
    for line, label in zip(lines, ("THOUGHT", "ACTION", "PAYLOAD"), strict=True):
        match = _THREE_LINE_RE.match(line.strip())
        if match is None or match.group(1) != label:
            return False
    return True


def probe_protocol(sample: str) -> str | None:
    """Classify ONE untrusted completion sample by its apparent protocol.

    Deterministic, pure, and never raising: JSON-looking samples (a
    parseable object carrying the action shape's ``kind`` key), strict
    three-line samples, and plain text map to ``"json"`` /
    ``"three_line"`` / ``"terminal"`` respectively. Empty,
    whitespace-only, and binary (NUL-bearing) samples carry no signal
    and yield None. Only the first ``PROBE_SAMPLE_LIMIT`` characters
    are examined, so hostile output stays bounded.

    Args:
        sample: One raw model completion. Untrusted.

    Returns:
        The protocol family name the sample appears to conform to, or
        None when the sample is empty or ambiguous. Never raises.
    """
    text = sample.strip()[:PROBE_SAMPLE_LIMIT]
    if not text:
        return None
    if "\x00" in text:
        return None
    if _probe_json(text):
        return PROTOCOL_JSON
    if _probe_three_line(text):
        return PROTOCOL_THREE_LINE
    return PROTOCOL_TERMINAL


def _family_for_model_id(model_id: str) -> str:
    """The built-in family for ``model_id``, or the fallback family.

    Case-insensitive; a prefix matches only when followed by a
    non-letter (``"llama3.1"`` matches, ``"gptool"`` does not).
    Unknown or empty ids map to the fallback family — never raised.
    """
    lowered = model_id.lower()
    for prefix, family in FAMILY_PREFIXES:
        if lowered.startswith(prefix) and not lowered[len(prefix) : len(prefix) + 1].isalpha():
            return family
    return FALLBACK_FAMILY


def profile_for_model_id(
    model_id: str, *, profiles: Mapping[str, ModelProfile] | None = None
) -> ModelProfile:
    """Map a model id to a :class:`ModelProfile` (deterministic).

    Lowercases ``model_id`` and matches the built-in family prefixes
    (``"gpt-4o"`` -> gpt, ``"deepseek-v4"`` -> deepseek,
    ``"claude-3"`` -> claude, ``"llama-3"`` -> llama). Unknown ids map
    to :data:`FALLBACK_PROFILE` — whose confidence is below the probe
    threshold, keeping protocol probing in the loop
    (docs/TECHNICAL_REQUIREMENTS.md, "Model Discovery"). Never raises.

    Args:
        model_id: Any model identifier, e.g. ``"gpt-4o"``,
            ``"deepseek-v4-flash-0731"``, ``"llama3.1:8b"``.
        profiles: Optional override registry keyed by family (defaults
            to :data:`BUILTIN_PROFILES`). An unmatched id resolves to
            the supplied registry's ``"fallback"`` entry when present,
            else the module-level :data:`FALLBACK_PROFILE`.
    """
    registry = BUILTIN_PROFILES if profiles is None else profiles
    return registry.get(_family_for_model_id(model_id), FALLBACK_PROFILE)


def _model_id_listed(model_id: str, models: Sequence[str]) -> bool:
    """True when ``model_id`` appears in the advertised model list.

    Case-insensitive existence cross-check: an exact id, or a prefix
    in either direction (a server may advertise the family line
    ``gpt-4o`` while the run selected snapshot ``gpt-4o-2024-05-13``,
    or vice versa). A heuristic for the confidence bump only — never a
    security decision.
    """
    lowered = model_id.lower()
    return any(
        lowered == advertised.lower()
        or advertised.lower().startswith(lowered)
        or lowered.startswith(advertised.lower())
        for advertised in models
    )


def discover_profile(
    model_id: str,
    *,
    sample: str | None = None,
    models: Sequence[str] | None = None,
) -> ModelProfile:
    """Map ``model_id`` to a profile, refining it with any evidence.

    Starts from :func:`profile_for_model_id`. When the base mapping is
    low-confidence (below ``PROBE_CONFIDENCE_THRESHOLD``) and evidence
    is available, the profile is refined:

    - ``models`` (the provider's advertised list): a match for
      ``model_id`` adds ``MODELS_MATCH_CONFIDENCE`` (the model
      demonstrably exists).
    - ``sample``: :func:`probe_protocol` classifies ONE completion; a
      protocol not yet declared by the profile is added and
      ``PROBE_MATCH_CONFIDENCE`` is added (format evidence).
    - ``function_call`` is never added: it is not detectable from a
      text sample and is never assumed (AGENTS.md).

    Returns a copy of the base profile — the built-ins are never
    mutated — whose confidence reflects the evidence: no evidence
    means the base confidence stands, which for unknown families is
    low and keeps protocol probing in the loop.
    """
    base = profile_for_model_id(model_id)
    protocols = base.protocols
    confidence = base.confidence
    if confidence < PROBE_CONFIDENCE_THRESHOLD:
        if models is not None and _model_id_listed(model_id, models):
            confidence = min(1.0, confidence + MODELS_MATCH_CONFIDENCE)
        if sample is not None:
            probed = probe_protocol(sample)
            # Guard documents the invariant: probing can never confirm
            # function-call support from a text sample.
            if probed is not None and probed != PROTOCOL_FUNCTION_CALL and probed not in protocols:
                protocols = frozenset(protocols | {probed})
                confidence = min(1.0, confidence + PROBE_MATCH_CONFIDENCE)
    return base.model_copy(update={"protocols": protocols, "confidence": confidence})


def _builtin(
    *,
    family: str,
    protocols: frozenset[str],
    context_soft_limit: int,
    output_token_limit: int,
    max_advertised_skills: int,
    confidence: float,
    temperature: float | None = 0.2,
    supported_roles: list[str] | None = None,
    failure_behavior: FailureBehavior = "repair_retry",
) -> ModelProfile:
    """Convenience constructor for built-in family profiles.

    Conservative defaults: temperature ``0.2`` (a mild determinism
    bias; adapters may override), the full ``system`` / ``user`` /
    ``assistant`` role set, and ``repair_retry`` failure behavior. No
    built-in profile ever declares ``function_call`` (never assume
    function-call support, AGENTS.md).
    """
    return ModelProfile(
        family=family,
        protocols=protocols,
        context_soft_limit=context_soft_limit,
        output_token_limit=output_token_limit,
        temperature=temperature,
        supported_roles=supported_roles or ["system", "user", "assistant"],
        max_advertised_skills=max_advertised_skills,
        failure_behavior=failure_behavior,
        confidence=confidence,
    )


#: GPT-family profile (e.g. ``gpt-4o``): text + three-line + JSON
#: protocols, 128k context, 4096 output tokens.
GPT_PROFILE = _builtin(
    family="gpt",
    protocols=frozenset({PROTOCOL_TERMINAL, PROTOCOL_THREE_LINE, PROTOCOL_JSON}),
    context_soft_limit=128_000,
    output_token_limit=4_096,
    max_advertised_skills=8,
    confidence=0.9,
)

#: Claude-family profile (e.g. ``claude-3``): 100k context, 4096
#: output tokens.
CLAUDE_PROFILE = _builtin(
    family="claude",
    protocols=frozenset({PROTOCOL_TERMINAL, PROTOCOL_THREE_LINE, PROTOCOL_JSON}),
    context_soft_limit=100_000,
    output_token_limit=4_096,
    max_advertised_skills=8,
    confidence=0.9,
)

#: DeepSeek-family profile (e.g. ``deepseek-v4``): 64k context, 4096
#: output tokens.
DEEPSEEK_PROFILE = _builtin(
    family="deepseek",
    protocols=frozenset({PROTOCOL_TERMINAL, PROTOCOL_THREE_LINE, PROTOCOL_JSON}),
    context_soft_limit=64_000,
    output_token_limit=4_096,
    max_advertised_skills=8,
    confidence=0.9,
)

#: Llama-family profile (e.g. ``llama-3``): a wide family spanning
#: local deployments, so limits are conservative and confidence lower.
LLAMA_PROFILE = _builtin(
    family="llama",
    protocols=frozenset({PROTOCOL_TERMINAL, PROTOCOL_THREE_LINE, PROTOCOL_JSON}),
    context_soft_limit=32_000,
    output_token_limit=2_048,
    max_advertised_skills=4,
    confidence=0.8,
)

#: Conservative plain-text fallback for unknown models: terminal text
#: only, no function-call assumption, no system-role assumption, no
#: advertised skills, no temperature override, and ``abort_turn``
#: failure behavior (an unknown model's repair semantics are unknown).
FALLBACK_PROFILE = ModelProfile(
    family=FALLBACK_FAMILY,
    protocols=frozenset({PROTOCOL_TERMINAL}),
    context_soft_limit=8_000,
    output_token_limit=1_024,
    temperature=None,
    supported_roles=["user", "assistant"],
    max_advertised_skills=0,
    failure_behavior="abort_turn",
    confidence=0.3,
)

#: Deterministic registry: family -> profile. Populated at import with
#: the built-ins; explicitly not a plugin system (AGENTS.md rule #10).
#: The fallback family entry is :data:`FALLBACK_PROFILE` itself, so an
#: unknown id resolves to it through the same lookup path.
BUILTIN_PROFILES: dict[str, ModelProfile] = {
    "gpt": GPT_PROFILE,
    "claude": CLAUDE_PROFILE,
    "deepseek": DEEPSEEK_PROFILE,
    "llama": LLAMA_PROFILE,
    FALLBACK_FAMILY: FALLBACK_PROFILE,
}
