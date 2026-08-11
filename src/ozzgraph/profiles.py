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
- The registry (:data:`BUILTIN_PROFILES`) is data, not code: profiles
  ship as per-model TOML files under ``profile_data/`` (declared as
  wheel package data) and are loaded through the deterministic
  :class:`~ozzgraph.profile_store.ProfileStore` (or the pure
  :func:`load_profiles_from_dir` loader at import time). Adding a
  profile means adding a TOML file — the kernel stays small (AGENTS.md
  rule #10), and the family assumptions that used to be hardcoded are
  now empirical, data-driven entries (docs/CHANGES_v2.md milestone 5).
- Benchmarks are measured data: a profile carries per-protocol
  :class:`~ozzgraph.traces.TraceMetrics` (``benchmarks``), persisted by
  :meth:`~ozzgraph.profile_store.ProfileStore.update_benchmarks` from
  model-harness matrix runs (format compliance, tool selection,
  repetition, evidence grounding, solve rates). Shipped placeholder
  entries are all-zero, meaning "not yet measured".
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from ozzgraph.traces import TraceMetrics

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
    ("nemotron", "nemotron"),
    ("nvidia", "nemotron"),
    ("openrouter", "openrouter"),
    ("gemma", "gemma"),
    ("google", "gemma"),
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

    ``model_ids`` lists concrete model ids the family profile covers
    (exact-id discovery evidence, V05); ``benchmarks`` holds per-protocol
    measured harness metrics (:class:`~ozzgraph.traces.TraceMetrics`),
    persisted by the profile store — ``None`` means nothing measured yet.
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
    model_ids: list[str] = Field(default_factory=list)
    benchmarks: dict[str, TraceMetrics] | None = None

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


def refine_profile(
    base: ModelProfile,
    model_id: str,
    *,
    sample: str | None = None,
    models: Sequence[str] | None = None,
) -> ModelProfile:
    """Refine ``base`` with any evidence (models list, one completion).

    The refinement half of discovery, factored out so callers with a
    pre-resolved base profile (e.g. the profile store's exact per-model
    lookup) apply exactly the same evidence rules as
    :func:`discover_profile`:

    - ``models`` (the provider's advertised list): a match for
      ``model_id`` adds ``MODELS_MATCH_CONFIDENCE`` (the model
      demonstrably exists).
    - ``sample``: :func:`probe_protocol` classifies ONE completion; a
      protocol not yet declared by the profile is added and
      ``PROBE_MATCH_CONFIDENCE`` is added (format evidence).
    - ``function_call`` is never added: it is not detectable from a
      text sample and is never assumed (AGENTS.md).

    Refinement only applies while the base confidence is below
    :data:`PROBE_CONFIDENCE_THRESHOLD` (unknown models stay in the
    probe loop; known families stand as shipped). Returns a copy — the
    base profile is never mutated.
    """
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


def discover_profile(
    model_id: str,
    *,
    sample: str | None = None,
    models: Sequence[str] | None = None,
) -> ModelProfile:
    """Map ``model_id`` to a profile, refining it with any evidence.

    Starts from :func:`profile_for_model_id`. When the base mapping is
    low-confidence (below ``PROBE_CONFIDENCE_THRESHOLD``) and evidence
    is available, the profile is refined — see
    :func:`refine_profile` for the evidence rules (advertised model
    list, one completion sample; ``function_call`` is never added).

    Returns a copy of the base profile — the built-ins are never
    mutated — whose confidence reflects the evidence: no evidence
    means the base confidence stands, which for unknown families is
    low and keeps protocol probing in the loop.
    """
    return refine_profile(profile_for_model_id(model_id), model_id, sample=sample, models=models)


def profile_from_toml_mapping(data: Mapping[str, object]) -> ModelProfile:
    """Parse one profile TOML file's contents into a :class:`ModelProfile`.

    Pure and deterministic (V05): the TOML document is a single table
    of profile fields (``family``, ``protocols``,
    ``output_token_limit``, ``model_ids``, ...) with optional nested
    ``[benchmarks.<protocol>]`` tables of measured
    :class:`~ozzgraph.traces.TraceMetrics`. ``model_ids`` lists the
    concrete model ids the family profile covers — exact-id discovery
    evidence. Invalid data fails loudly through pydantic validation.
    """
    return ModelProfile.model_validate(dict(data))


def load_profiles_from_dir(data_dir: Path) -> dict[str, ModelProfile]:
    """Deterministically load every ``*.toml`` in ``data_dir``.

    Files are read in sorted filename order; each file must declare a
    distinct family (a duplicate family fails loudly — a store whose
    data is ambiguous must never silently pick one). A missing
    directory raises :class:`FileNotFoundError`, so a vanished data
    source never degrades silently to the fallback profile.
    """
    if not data_dir.is_dir():
        raise FileNotFoundError(f"profile data dir does not exist: {data_dir}")
    profiles: dict[str, ModelProfile] = {}
    for path in sorted(data_dir.glob("*.toml")):
        profile = profile_from_toml_mapping(tomllib.loads(path.read_text()))
        if profile.family in profiles:
            raise ValueError(f"duplicate profile family {profile.family!r} in {path}")
        profiles[profile.family] = profile
    return profiles


def default_profile_dir() -> Path:
    """The package data directory shipping the initial TOML profiles.

    ``profile_data/`` sits next to this module, so it resolves the
    same in editable checkouts and installed wheels (the directory is
    declared as wheel package data in ``pyproject.toml``).
    """
    return Path(__file__).resolve().parent / "profile_data"


#: Deterministic registry: family -> profile, loaded from the shipped
#: TOML profiles under ``profile_data/``. The registry is data, not
#: code (V05): adding a profile means adding a TOML file, and the
#: family assumptions that used to be hardcoded here are now empirical,
#: data-driven entries. The fallback family entry is
#: :data:`FALLBACK_PROFILE` itself, so an unknown id resolves to it
#: through the same lookup path.
BUILTIN_PROFILES: dict[str, ModelProfile] = load_profiles_from_dir(default_profile_dir())

#: GPT-family profile (e.g. ``gpt-4o``) from ``profile_data/gpt.toml``:
#: text + three-line + JSON protocols, 128k context, 4096 output
#: tokens. Backward-compatible alias for the registry entry.
GPT_PROFILE = BUILTIN_PROFILES["gpt"]

#: Claude-family profile (e.g. ``claude-3``) from
#: ``profile_data/claude.toml``: 100k context, 4096 output tokens.
CLAUDE_PROFILE = BUILTIN_PROFILES["claude"]

#: DeepSeek-family profile (e.g. ``deepseek-v4``) from
#: ``profile_data/deepseek.toml``: 64k context, 4096 output tokens.
DEEPSEEK_PROFILE = BUILTIN_PROFILES["deepseek"]

#: Llama-family profile (e.g. ``llama-3``) from
#: ``profile_data/llama.toml``: a wide family spanning local
#: deployments, so limits are conservative and confidence lower.
LLAMA_PROFILE = BUILTIN_PROFILES["llama"]

#: Conservative plain-text fallback for unknown models (shipped as
#: ``profile_data/fallback.toml``): terminal text only, no
#: function-call assumption, no system-role assumption, no advertised
#: skills, no temperature override, and ``abort_turn`` failure behavior
#: (an unknown model's repair semantics are unknown). Confidence sits
#: below :data:`PROBE_CONFIDENCE_THRESHOLD`, keeping protocol probing
#: in the discovery loop.
FALLBACK_PROFILE = BUILTIN_PROFILES[FALLBACK_FAMILY]
