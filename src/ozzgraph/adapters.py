"""Model adapters for OzzGraph (PR13 interface, PR14/15 concrete adapters).

Defines the ADAPTER layer (docs/ARCHITECTURE.md, "Model Adapters";
docs/TECHNICAL_REQUIREMENTS.md, "Model Adapter Requirements"; PRD goal
2): the normalized, protocol-independent :class:`ParsedAction` parsed
out of a model completion, the typed :class:`AdapterParseError`, and
the :class:`ModelAdapter` abstract base class that every concrete
adapter implements — protocol, prompt compiler, parser, repair
strategy, and the protocol-specific limits carried from a
:class:`~ozzgraph.profiles.ModelProfile`.

PR14 and PR15 land the three concrete adapters in this module,
registered at import time so :func:`adapter_for` resolves them
immediately:

- :class:`TerminalAdapter` — the permissive plain-text fallback
  protocol (``PROTOCOL_TERMINAL``): free text with an optional
  ``ACTION: <kind>`` directive line and an optional ``PAYLOAD:
  <value>`` line, degrading to a ``think`` action when no directive is
  present. Never raises on plain text.
- :class:`ThreeLineAdapter` — the strict bounded-output protocol
  (``PROTOCOL_THREE_LINE``): exactly three non-empty lines in order
  (``THOUGHT:``, ``ACTION:``, ``PAYLOAD:``); every deviation raises
  :class:`AdapterParseError`.
- :class:`JsonAdapter` — the strict structured-output protocol
  (``PROTOCOL_JSON``): exactly one JSON object carrying the normalized
  action shape (a required non-empty string ``kind``, optional string
  ``payload`` / ``rationale``, no other keys); every deviation raises
  :class:`AdapterParseError`.

PR15 also owns the deterministic repair strategies: labeled-line
extraction for :class:`ThreeLineAdapter` (prose-wrapped completions
are rebuilt into the exact three-line format) and fence-strip /
balanced-object extraction for :class:`JsonAdapter`. The permissive
terminal protocol has nothing to repair.

The registry (:data:`ADAPTERS`) is a plain deterministic dict keyed by
protocol family, populated only by explicit :func:`register_adapter`
calls (AGENTS.md rule #10 — not a plugin system).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ozzgraph.profiles import (
    _THREE_LINE_RE,
    PROTOCOL_JSON,
    PROTOCOL_TERMINAL,
    PROTOCOL_THREE_LINE,
    FailureBehavior,
    ModelProfile,
    _probe_json,
)


class ParsedAction(BaseModel):
    """A normalized, protocol-independent action parsed from a completion.

    The executor (PR20) consumes this shape regardless of which adapter
    produced it. ``kind`` is one of the action kinds the executor
    understands (e.g. ``"run"``, ``"think"``, ``"submit"``, ``"hint"``,
    ``"exit"``); unknown kinds are schema-valid here and rejected by
    executor policy later (fail loudly at the owning layer). ``raw``
    always carries the original completion text so repair and evidence
    handling never lose the source.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    payload: str | None = None
    rationale: str | None = None
    raw: str


class AdapterError(RuntimeError):
    """Base error for the model adapter layer (AGENTS.md rule #9)."""


class AdapterParseError(AdapterError):
    """A completion that could not be parsed into an action.

    Attributes:
        protocol: The adapter protocol that failed to parse (e.g.
            ``"json"``).
        detail: Human-readable parse failure detail.
    """

    def __init__(self, *, protocol: str, detail: str) -> None:
        super().__init__(detail)
        self.protocol = protocol
        self.detail = detail


class AdapterRegistryError(AdapterError):
    """Raised when the adapter registry cannot resolve or accept an adapter."""


class ModelAdapter(ABC):
    """Contract every concrete model adapter (PR14/15) must implement.

    An adapter owns one protocol family: how a prompt is compiled for
    it, how a completion is parsed into a :class:`ParsedAction`, and
    how parse failures are repaired (docs/ARCHITECTURE.md, "Model
    Adapters": "the adapter owns prompt compilation, parsing, repair,
    and protocol-specific limits").

    Concrete subclasses are constructed with the :class:`ModelProfile`
    the model was discovered with. The profile-derived attributes
    (``context_soft_limit``, ``output_token_limit``, ``temperature``,
    ``supported_roles``, ``max_advertised_skills``,
    ``failure_behavior``) read through to the profile and may be
    overridden by a subclass when the protocol imposes stricter limits
    (e.g. a terminal adapter capping output below the profile's token
    limit).
    """

    def __init__(self, profile: ModelProfile) -> None:
        self.profile = profile

    @property
    @abstractmethod
    def protocol(self) -> str:
        """The protocol family name this adapter implements."""

    @abstractmethod
    def compile_prompt(
        self,
        *,
        mission: str,
        graph_summary: str,
        transcript_tail: str,
        skills: Sequence[str],
        output_contract: str,
    ) -> str:
        """Compile the model prompt for this protocol.

        Exact prompt composition is the context compiler's and each
        concrete adapter's job (PR14/16); this contract fixes the input
        shape: the immutable mission, the bounded graph summary, the
        recent transcript tail, the advertised skill summaries, and the
        output contract describing the expected completion format.
        """

    @abstractmethod
    def parse(self, completion: str) -> ParsedAction:
        """Parse one completion into a normalized action.

        Raises:
            AdapterParseError: When the completion does not conform to
                this protocol's format.
        """

    @abstractmethod
    def repair(self, completion: str, error: AdapterParseError) -> str | None:
        """Repair a failed completion, or say it cannot be repaired.

        Returns repaired completion text when the adapter's repair
        strategy (PR15) produced a fix, else None. Never raises.
        """

    @property
    def context_soft_limit(self) -> int:
        """Usable context budget (chars/tokens) for this adapter."""
        return self.profile.context_soft_limit

    @property
    def output_token_limit(self) -> int:
        """Output token cap for this adapter's completions."""
        return self.profile.output_token_limit

    @property
    def temperature(self) -> float | None:
        """Sampling temperature; None means the adapter/model default."""
        return self.profile.temperature

    @property
    def supported_roles(self) -> list[str]:
        """Message roles this protocol can express (e.g. system/user)."""
        return self.profile.supported_roles

    @property
    def max_advertised_skills(self) -> int:
        """Cap on skills advertised to the model in one prompt."""
        return self.profile.max_advertised_skills

    @property
    def failure_behavior(self) -> FailureBehavior:
        """Conservative failure policy (``repair_retry`` | ``abort_turn``)."""
        return self.profile.failure_behavior


#: Terminal protocol output-format instructions: free text with an
#: optional action directive line. Compiled into every terminal prompt.
_TERMINAL_FORMAT_INSTRUCTIONS = """\
OUTPUT FORMAT
Respond in plain text. If you want to act, end your response with a single
action directive line:

  ACTION: <kind>

Optionally followed by one payload line:

  PAYLOAD: <value>

The payload may contain spaces. Everything else you write is your rationale.
If you only want to think, write plain text and no directive.
"""

#: Three-line protocol output-format instructions: the strict bounded
#: template. Compiled into every three-line prompt.
_THREE_LINE_FORMAT_INSTRUCTIONS = """\
OUTPUT FORMAT
Respond with exactly three non-empty lines, in this order:

  THOUGHT: <your reasoning>
  ACTION: <kind>
  PAYLOAD: <value>

Each line is a label, a colon, and a non-empty value. No extra lines, no
missing lines, no reordered labels.
"""

#: JSON protocol output-format instructions: the strict single-object
#: action schema. Compiled into every JSON prompt.
_JSON_FORMAT_INSTRUCTIONS = """\
OUTPUT FORMAT
Respond with a single JSON object and nothing else — no prose, no code fence:

  {"kind": "<action kind>", "payload": "<optional string>", "rationale": "<optional reasoning>"}

"kind" is required: a non-empty string naming the action kind (run, think,
submit, hint, or exit). "payload" and "rationale" are optional strings. The
object must contain no other keys.
"""


def _compile_prompt(
    *,
    mission: str,
    graph_summary: str,
    transcript_tail: str,
    skills: Sequence[str],
    format_instructions: str,
    output_contract: str,
) -> str:
    """Compose the shared prompt skeleton for a concrete adapter.

    Both PR14 adapters render the same context sections — mission,
    graph summary, transcript tail, advertised skills, the protocol's
    output-format instructions, and the passed-through output contract
    — so the skeleton lives here and each adapter supplies its own
    format block. Higher-level composition is the context compiler's
    job (PR16); this guarantees the protocol instructions and the
    contract are always present (docs/ARCHITECTURE.md, "Model
    Adapters").
    """
    if skills:
        skills_block = "\n".join(f"- {skill}" for skill in skills)
    else:
        skills_block = "(none)"
    return "\n\n".join(
        (
            f"MISSION\n{mission}",
            f"GRAPH SUMMARY\n{graph_summary}",
            f"TRANSCRIPT TAIL\n{transcript_tail}",
            f"AVAILABLE SKILLS\n{skills_block}",
            format_instructions,
            f"OUTPUT CONTRACT\n{output_contract}",
        )
    )


#: A terminal action directive line: ``ACTION: <kind>``. Case-sensitive
#: (matching the three-line labels); requires a non-empty value, so a
#: bare ``ACTION:`` line is prose, not a directive.
_ACTION_DIRECTIVE_RE = re.compile(r"^ACTION:\s*(.+)$")

#: A terminal payload line: ``PAYLOAD: <value>`` (may contain spaces).
_PAYLOAD_DIRECTIVE_RE = re.compile(r"^PAYLOAD:\s*(.+)$")


class TerminalAdapter(ModelAdapter):
    """Permissive plain-text adapter (protocol ``"terminal"``).

    The fallback protocol for unknown models
    (:data:`~ozzgraph.profiles.FALLBACK_PROFILE` declares terminal
    only): free text that may contain an action directive line
    ``ACTION: <kind>``, optionally followed by a ``PAYLOAD: <value>``
    line. Everything before/around the directive becomes the rationale.
    A completion with no directive degrades to a ``think`` action —
    parsing never raises on plain text, only on empty input.

    Consistent with the probe: :func:`probe_protocol` classifies any
    non-JSON, non-three-line text as terminal, and this parser accepts
    any text, so the two never disagree on what is terminal.
    """

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
        """Compile the terminal prompt (free text + optional directive)."""
        return _compile_prompt(
            mission=mission,
            graph_summary=graph_summary,
            transcript_tail=transcript_tail,
            skills=skills,
            format_instructions=_TERMINAL_FORMAT_INSTRUCTIONS,
            output_contract=output_contract,
        )

    def parse(self, completion: str) -> ParsedAction:
        """Parse free text into a think or directed action.

        The first ``ACTION: <kind>`` line is the directive; a
        ``PAYLOAD: <value>`` line immediately after it is the payload;
        all other lines (before and after) are the rationale. Without a
        directive the whole completion degrades to a ``think`` action.

        Raises:
            AdapterParseError: Only when the completion is empty or
                whitespace-only — plain text never raises.
        """
        if not completion.strip():
            raise AdapterParseError(
                protocol=self.protocol,
                detail="completion is empty or whitespace-only",
            )
        lines = completion.splitlines()
        action_index: int | None = None
        kind = ""
        payload: str | None = None
        for index, line in enumerate(lines):
            match = _ACTION_DIRECTIVE_RE.match(line)
            if match is None:
                continue
            action_index = index
            kind = match.group(1).strip()
            if index + 1 < len(lines):
                payload_match = _PAYLOAD_DIRECTIVE_RE.match(lines[index + 1])
                if payload_match is not None:
                    payload = payload_match.group(1).strip()
            break
        if action_index is None:
            return ParsedAction(kind="think", rationale=completion.strip(), raw=completion)
        kept = [
            line
            for index, line in enumerate(lines)
            if index != action_index and not (payload is not None and index == action_index + 1)
        ]
        return ParsedAction(
            kind=kind,
            payload=payload,
            rationale="\n".join(kept).strip(),
            raw=completion,
        )

    def repair(self, completion: str, error: AdapterParseError) -> str | None:
        """No terminal repair strategy: the protocol is permissive.

        The terminal protocol's only parse failure is empty input,
        which no repair can fix — there is nothing to salvage. Never
        raises.
        """
        return None


class ThreeLineAdapter(ModelAdapter):
    """Strict three-line adapter (protocol ``"three_line"``).

    The bounded-output contract: exactly three non-empty lines, in
    order, each matching ``LABEL: <non-empty value>`` — THOUGHT, then
    ACTION, then PAYLOAD. The ACTION value is the kind verb (e.g.
    ``"run"``, ``"think"``, ``"submit"``, ``"hint"``, ``"exit"``); the
    kind vocabulary is NOT validated here — executor policy (PR20)
    owns that, so schema-valid kinds pass through.

    Every deviation — wrong line count, wrong label order, missing or
    empty values, extra lines, empty completion — raises
    :class:`AdapterParseError` with a human-readable detail. The parser
    is authoritative but consistent with the conservative probe
    (:func:`~ozzgraph.profiles.probe_protocol`): non-empty lines are
    the unit of counting, exactly as in the probe's shape check.
    """

    @property
    def protocol(self) -> str:
        return PROTOCOL_THREE_LINE

    def compile_prompt(
        self,
        *,
        mission: str,
        graph_summary: str,
        transcript_tail: str,
        skills: Sequence[str],
        output_contract: str,
    ) -> str:
        """Compile the strict three-line prompt."""
        return _compile_prompt(
            mission=mission,
            graph_summary=graph_summary,
            transcript_tail=transcript_tail,
            skills=skills,
            format_instructions=_THREE_LINE_FORMAT_INSTRUCTIONS,
            output_contract=output_contract,
        )

    def parse(self, completion: str) -> ParsedAction:
        """Parse a strict three-line completion into an action.

        Raises:
            AdapterParseError: On any deviation from the strict format.
        """
        if not completion.strip():
            raise AdapterParseError(
                protocol=self.protocol,
                detail="completion is empty or whitespace-only",
            )
        lines = [line.strip() for line in completion.splitlines() if line.strip()]
        if len(lines) != 3:
            raise AdapterParseError(
                protocol=self.protocol,
                detail=f"expected exactly 3 non-empty lines, got {len(lines)}",
            )
        values: dict[str, str] = {}
        for number, (line, label) in enumerate(
            zip(lines, ("THOUGHT", "ACTION", "PAYLOAD"), strict=True), start=1
        ):
            match = _THREE_LINE_RE.match(line)
            if match is None or match.group(1) != label:
                raise AdapterParseError(
                    protocol=self.protocol,
                    detail=f"line {number}: expected {label}: <value>, got {line!r}",
                )
            values[label] = match.group(2).strip()
        return ParsedAction(
            kind=values["ACTION"],
            payload=values["PAYLOAD"],
            rationale=values["THOUGHT"],
            raw=completion,
        )

    def repair(self, completion: str, error: AdapterParseError) -> str | None:
        """Repair a prose-wrapped three-line completion (PR15 strategy).

        Models often wrap the strict format in surrounding text. This
        strategy scans the completion for the labeled lines
        (``THOUGHT:`` / ``ACTION:`` / ``PAYLOAD:``), takes the first
        occurrence of each label, and rebuilds the exact three-line
        completion from the extracted values. Returns None when fewer
        than three distinct labels are found or the rebuild is
        byte-identical to the input (nothing changed). Never raises.
        """
        values: dict[str, str] = {}
        for line in completion.splitlines():
            match = _THREE_LINE_RE.match(line.strip())
            if match is None:
                continue
            label = match.group(1)
            if label in values:
                continue
            values[label] = match.group(2).strip()
            if len(values) == 3:
                break
        if len(values) < 3:
            return None
        rebuilt = "\n".join(
            f"{label}: {values[label]}" for label in ("THOUGHT", "ACTION", "PAYLOAD")
        )
        if rebuilt == completion:
            return None
        return rebuilt


def _strip_code_fence(text: str) -> str | None:
    """Strip one surrounding markdown code fence (`` ``` `` / `` ```json ``).

    Returns the fenced inner text when ``text`` is exactly a fenced
    block (fence markers as the first and last lines, with any
    surrounding whitespace), else None. The inner text is returned
    verbatim — it may or may not be valid JSON; :meth:`JsonAdapter.repair`
    decides salvageability.
    """
    lines = text.splitlines()
    if len(lines) < 3:
        return None
    if lines[0].strip() not in ("```", "```json") or lines[-1].strip() != "```":
        return None
    return "\n".join(lines[1:-1])


def _first_balanced_object(text: str) -> str | None:
    """The first balanced ``{...}`` object in ``text``, or None.

    String-aware: braces inside JSON string values and escaped quotes
    never count toward depth, so prose like ``the {answer}`` cannot
    truncate a later object. An unclosed object yields None. The
    extracted text is returned verbatim — salvageability is decided by
    the caller.
    """
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : position + 1]
        index = start + 1


class JsonAdapter(ModelAdapter):
    """Strict JSON adapter (protocol ``"json"``).

    The structured-output protocol: exactly one JSON object carrying
    the normalized action shape — a required non-empty string ``kind``
    (e.g. ``"run"``, ``"think"``, ``"submit"``, ``"hint"``,
    ``"exit"``; the kind vocabulary is NOT validated here — executor
    policy (PR20) owns that, so schema-valid kinds pass through),
    optional string ``payload``, optional string ``rationale``, and no
    other keys. Every deviation — empty input, unparseable JSON,
    pathological nesting, a non-object top level, a missing / empty /
    non-string ``kind``, a non-string ``payload`` / ``rationale``, or
    extra keys — raises :class:`AdapterParseError` with the parse
    detail. A raw pydantic error never escapes: the ``extra='forbid'``
    schema violation on :class:`ParsedAction` is converted to the
    adapter error type.

    Consistent with the probe: :func:`~ozzgraph.profiles.probe_protocol`
    classifies any parseable object with a non-empty string ``kind`` as
    ``"json"``, and this parser accepts exactly that shape — the
    probe's conservative shape check and the authoritative parser agree.

    Repair (never raising) strips a surrounding markdown code fence or,
    failing that, extracts the first balanced ``{...}`` object from a
    prose-wrapped completion, returning the salvaged JSON text only
    when it parses as the action shape — else None.
    """

    @property
    def protocol(self) -> str:
        return PROTOCOL_JSON

    def compile_prompt(
        self,
        *,
        mission: str,
        graph_summary: str,
        transcript_tail: str,
        skills: Sequence[str],
        output_contract: str,
    ) -> str:
        """Compile the strict JSON prompt (single-object schema)."""
        return _compile_prompt(
            mission=mission,
            graph_summary=graph_summary,
            transcript_tail=transcript_tail,
            skills=skills,
            format_instructions=_JSON_FORMAT_INSTRUCTIONS,
            output_contract=output_contract,
        )

    def parse(self, completion: str) -> ParsedAction:
        """Parse exactly one JSON action object.

        Raises:
            AdapterParseError: On empty input, unparseable JSON,
                pathological nesting, a non-object top level, a
                missing / empty / non-string ``kind``, a non-string
                ``payload`` / ``rationale``, or any extra key.
        """
        if not completion.strip():
            raise AdapterParseError(
                protocol=self.protocol,
                detail="completion is empty or whitespace-only",
            )
        try:
            payload = json.loads(completion)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise AdapterParseError(
                protocol=self.protocol,
                detail=f"completion is not valid JSON: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise AdapterParseError(
                protocol=self.protocol,
                detail=(f"expected a JSON object at the top level, got {type(payload).__name__}"),
            )
        if "kind" not in payload:
            raise AdapterParseError(
                protocol=self.protocol,
                detail="missing required 'kind' key in action JSON",
            )
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            raise AdapterParseError(
                protocol=self.protocol,
                detail="'kind' must be a non-empty string",
            )
        payload_value = payload.get("payload")
        if payload_value is not None and not isinstance(payload_value, str):
            raise AdapterParseError(
                protocol=self.protocol,
                detail="'payload' must be a string when present",
            )
        rationale = payload.get("rationale")
        if rationale is not None and not isinstance(rationale, str):
            raise AdapterParseError(
                protocol=self.protocol,
                detail="'rationale' must be a string when present",
            )
        try:
            # extra='forbid' on ParsedAction: unknown keys fail loudly.
            # Converted so a raw pydantic error never escapes the layer.
            return ParsedAction.model_validate(
                {**payload, "raw": completion},
            )
        except ValidationError as exc:
            raise AdapterParseError(
                protocol=self.protocol,
                detail=f"action schema validation failed: {exc}",
            ) from exc

    def repair(self, completion: str, error: AdapterParseError) -> str | None:
        """Repair a malformed JSON completion (PR15 strategy).

        Deterministic, never-raising salvage: (a) strip a surrounding
        markdown code fence; (b) else extract the first balanced
        ``{...}`` object from prose; (c) return the repaired JSON text
        when it parses as the action shape and differs from the input,
        else None. No LLM calls.
        """
        candidate = _strip_code_fence(completion)
        if candidate is None or not _probe_json(candidate):
            candidate = _first_balanced_object(completion)
        if candidate is None or not _probe_json(candidate):
            return None
        if candidate == completion:
            return None
        return candidate


#: Deterministic registry: protocol family -> adapter class. Populated
#: at import by the concrete adapters below (terminal, three_line,
#: json). Explicit :func:`register_adapter` only — no discovery,
#: AGENTS.md rule #10.
ADAPTERS: dict[str, type[ModelAdapter]] = {}


def register_adapter(protocol: str, cls: type[ModelAdapter]) -> None:
    """Register ``cls`` as the adapter for ``protocol``.

    Raises:
        AdapterRegistryError: If ``protocol`` is empty, ``cls`` is not
            a :class:`ModelAdapter` subclass, or an adapter is already
            registered for the protocol (duplicate registration fails
            loudly).
    """
    if not protocol:
        raise AdapterRegistryError(f"protocol must be a non-empty str, got {protocol!r}")
    if not isinstance(cls, type) or not issubclass(cls, ModelAdapter):
        raise AdapterRegistryError(
            f"adapter for protocol {protocol!r} must be a ModelAdapter subclass, got {cls!r}"
        )
    if protocol in ADAPTERS:
        raise AdapterRegistryError(f"an adapter is already registered for protocol {protocol!r}")
    ADAPTERS[protocol] = cls


def adapter_for(protocol: str) -> type[ModelAdapter]:
    """The adapter class registered for ``protocol``.

    Raises:
        AdapterRegistryError: If no adapter is registered for the
            protocol.
    """
    try:
        return ADAPTERS[protocol]
    except KeyError:
        raise AdapterRegistryError(f"no adapter registered for protocol {protocol!r}") from None


# Register the PR14/15 concrete adapters at import time so
# :func:`adapter_for` resolves them as soon as this module is imported
# (a module nobody imports does not count as registered).
register_adapter(PROTOCOL_TERMINAL, TerminalAdapter)
register_adapter(PROTOCOL_THREE_LINE, ThreeLineAdapter)
register_adapter(PROTOCOL_JSON, JsonAdapter)
