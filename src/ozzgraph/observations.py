"""Observation parsers for OzzGraph (PR11).

Implements the Observation and Artifact Pipeline layer
(docs/ARCHITECTURE.md, "Artifact Pipeline"): raw tool output lives
outside model context, and parsers turn it into compact
:class:`Observation` values — a normalized result carrying a
human-readable summary for model context, a validated structured
payload, artifact handles (OBSERVATION STORED_AS ARTIFACT), truncation
carry-through, and explicit ``malformed`` / ``parse_error`` fields.
Observations are the link between an ACTION and later EVIDENCE
(EVIDENCE EXTRACTED_FROM OBSERVATION, docs/DATA_STRATEGY.md).

Design rules:

- Deterministic and pure: parsers do no I/O and keep no state; the same
  input always produces the same observation.
- Target output is untrusted (AGENTS.md Security Boundaries): hostile
  input — ANSI escapes, control characters, fake instructions, broken
  JSON — never crashes a parser and never raises. It becomes labeled
  data: shell summaries carry an "untrusted" prefix, control characters
  are escaped to visible ``\\xNN`` forms in summaries, and unparseable
  documents surface as structured ``malformed=True`` / ``parse_error``
  fields (fail loudly, AGENTS.md rule #9) instead of exceptions.
- The only raised errors are argument errors
  (:class:`ParserArgumentError`, mirroring
  :class:`~ozzgraph.shell.ShellRunnerError`) and registry errors
  (:class:`ParserRegistryError`) — never parse failures.

The registry (:data:`PARSERS`) is a plain deterministic dict keyed by
``(source, kind)``, populated at import with the two built-in parsers.
It is explicitly not a plugin system (AGENTS.md rule #10): adding a
parser means adding a class and one :func:`register_parser` call.

Built-in parsers:

- :class:`ShellTextParser` (``source="shell"``, ``kind="text"``) —
  generic shell stdout/stderr normalized into a line-based summary and
  structured counts, with ANSI escapes stripped and truncation carried
  through from :class:`~ozzgraph.shell.ToolResult`.
- :class:`HalctlJsonParser` (``source="halctl"``, ``kind="json"``) —
  ``halctl``'s single-JSON-document output (challenge / status /
  submit / hint / scoreboard / exit / error documents, see
  :mod:`ozzgraph.halctl`) classified and validated into structured
  data; the observation ``source`` becomes ``halctl:<document>``.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ozzgraph.shell import ToolResult

#: Cap for text embedded in summaries (commands, lines, messages,
#: hints), in characters. Keeps model context compact regardless of
#: output size.
SUMMARY_FIELD_LIMIT = 80

#: Cap for the diagnostic excerpt stored in ``data`` of malformed
#: observations, in characters.
EXCERPT_LIMIT = 200

#: ANSI escape sequences: CSI (``ESC [ ... final byte``), OSC (``ESC ]
#: ... BEL/ST``), other string sequences (DCS/SOS/...), any stray ESC
#: plus one character, and a bare trailing ESC. Removed so terminal
#: control from hostile output can never reach summaries.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[PX^_][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b."
    r"|\x1b"
)

#: C0 control characters and DEL; replaced by visible ``\xNN`` escapes
#: in summaries so hostile output cannot inject terminal control into
#: model context. Structured data keeps the raw characters.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class Observation(BaseModel):
    """Normalized result of one raw tool output.

    An observation is the compact, structured view of a raw tool output
    that may enter model context (the summary) and the state graph
    (structured data plus artifact handles) — never the raw output
    itself, which lives in the artifact store.

    Attributes:
        action_id: The producing Action this observation references
            (ACTION PRODUCED OBSERVATION). Empty only for raw-string
            parses, where the caller must attribute the observation
            before it enters the graph.
        source: Where the output came from, e.g. ``"shell"`` or
            ``"halctl:status"``.
        kind: The parse kind that normalized the output, e.g.
            ``"text"`` or ``"json"``.
        summary: Compact human-readable summary for model context; the
            raw output is never placed here.
        data: Validated structured payload (parsed counts and lines for
            text; the classified/validated document for JSON).
        artifact_ids: Artifact handles produced from this observation
            (OBSERVATION STORED_AS ARTIFACT). Populated by the executor
            when raw output is stored; parsers never perform I/O.
        truncated: True when any captured stream was cut by its output
            limit (carried through from
            :attr:`ToolResult.truncation_state`).
        truncated_streams: Names of the truncated streams, e.g.
            ``["stdout", "stderr"]``.
        exit_code: Process exit code when the output came from a
            :class:`~ozzgraph.shell.ToolResult`, else None.
        ok: True when the producing tool reported success; False for
            nonzero exits, error documents, and malformed output with a
            known exit code; None when unknown.
        malformed: True when the output could not be normalized into the
            expected structure (e.g. broken JSON, shape violations).
        parse_error: Structured failure detail when ``malformed`` is
            True, else None.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str = ""
    source: str
    kind: str
    summary: str
    data: dict[str, object] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    truncated_streams: list[str] = Field(default_factory=list)
    truncated: bool = False
    exit_code: int | None = None
    ok: bool | None = None
    malformed: bool = False
    parse_error: str | None = None

    @model_validator(mode="after")
    def _sync_truncated(self) -> Self:
        """Derive ``truncated`` from ``truncated_streams`` (one-way)."""
        if self.truncated_streams and not self.truncated:
            self.truncated = True
        return self


class ParserError(RuntimeError):
    """Base error for the observation parser layer (AGENTS.md rule #9)."""


class ParserArgumentError(ParserError):
    """Raised when a parser is called with invalid arguments.

    Target output — however malformed or adversarial — is never raised;
    it becomes structured :class:`Observation` fields. This error is for
    caller mistakes only, e.g. passing something that is neither a
    :class:`~ozzgraph.shell.ToolResult` nor a ``str``.
    """


class ParserRegistryError(ParserError):
    """Raised when the registry cannot resolve or accept a parser."""


class Parser(ABC):
    """Normalize one raw tool output into an :class:`Observation`.

    Subclasses declare the registry key ``source`` / ``kind`` (e.g.
    ``source="shell"``, ``kind="text"``) and implement :meth:`parse`.
    Parsers are stateless and deterministic: the same input always
    yields the same observation, and no parser performs I/O.
    """

    source: ClassVar[str]
    kind: ClassVar[str]

    @abstractmethod
    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one raw tool output into a normalized observation.

        Args:
            raw: A bounded :class:`~ozzgraph.shell.ToolResult` (the
                normal path — action id, exit code, and truncation are
                carried through) or a plain ``str`` payload (e.g. a
                document captured by a non-shell transport). A raw
                string parse yields an unattributed observation:
                ``action_id`` is empty and execution metadata is
                absent; callers that have the producing action must
                fill these in before the observation enters the graph.

        Raises:
            ParserArgumentError: If ``raw`` is neither a
                :class:`~ozzgraph.shell.ToolResult` nor a ``str``.

        Returns:
            The normalized observation. Unparseable or
            schema-violating target output is reported through
            ``malformed`` and ``parse_error`` fields — never raised.
        """


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from ``text`` (deterministic)."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _require_tool_result_or_str(raw: object) -> None:
    """Reject anything that is not a ToolResult or str (loud argument error)."""
    if not isinstance(raw, (ToolResult, str)):
        raise ParserArgumentError(f"raw must be a ToolResult or str, got {type(raw).__name__}")


def _tool_metadata(raw: ToolResult | str) -> tuple[str, int | None, list[str], bool]:
    """Execution metadata carried through from a ToolResult.

    Returns ``(action_id, exit_code, truncated_streams, timeout_state)``;
    a plain ``str`` input has no metadata, so it yields
    ``("", None, [], False)``.
    """
    if not isinstance(raw, ToolResult):
        return "", None, [], False
    truncated_streams = [
        stream
        for stream, flag in (
            ("stdout", raw.truncation_state.stdout_truncated),
            ("stderr", raw.truncation_state.stderr_truncated),
        )
        if flag
    ]
    return raw.action_id, raw.exit_code, truncated_streams, raw.timeout_state


def _visible(text: str) -> str:
    """Escape C0 control characters and DEL as visible ``\\xNN`` forms."""
    return _CONTROL_RE.sub(lambda match: f"\\x{ord(match.group(0)):02x}", text)


def _bounded(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters, appending '...' when cut."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _quote_field(text: str) -> str:
    """A bounded, control-escaped, single-quoted field for a summary."""
    return f"'{_bounded(_visible(text), SUMMARY_FIELD_LIMIT)}'"


def _first_non_empty(lines: list[str]) -> str:
    """The first line with content, or '' when every line is blank."""
    for line in lines:
        if line.strip():
            return line
    return ""


class ShellTextParser(Parser):
    """Normalize generic shell stdout/stderr into a compact observation.

    Line-based and defensive: ANSI escapes are stripped, control
    characters are escaped to visible forms in the summary (structured
    data keeps them raw), and adversarial content — fake system
    instructions, shell control noise, huge output — becomes labeled
    data rather than instructions. Text output is always parseable, so
    ``malformed`` stays False; truncation, timeout, and exit code are
    carried through from the :class:`~ozzgraph.shell.ToolResult`.
    """

    source: ClassVar[str] = "shell"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse shell stdout/stderr into a line-based observation."""
        _require_tool_result_or_str(raw)
        stdout = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        stderr = strip_ansi(raw.stderr if isinstance(raw, ToolResult) else "")
        command = raw.command if isinstance(raw, ToolResult) else ""
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        stdout_lines = stdout.splitlines()
        stderr_lines = stderr.splitlines()
        return Observation(
            action_id=action_id,
            source=self.source,
            kind=self.kind,
            summary=_summarize_shell(
                command=command,
                stdout_text=stdout,
                stdout_lines=stdout_lines,
                stderr_text=stderr,
                stderr_lines=stderr_lines,
                exit_code=exit_code,
                truncated_streams=truncated_streams,
                timeout=timeout,
            ),
            data={
                "line_count": len(stdout_lines),
                "char_count": len(stdout),
                "first_line": _first_non_empty(stdout_lines),
                "last_line": stdout_lines[-1] if stdout_lines else "",
                "stderr_line_count": len(stderr_lines),
                "stderr_char_count": len(stderr),
                "stderr_first_line": _first_non_empty(stderr_lines),
            },
            truncated_streams=truncated_streams,
            exit_code=exit_code,
            ok=None if exit_code is None else exit_code == 0,
        )


def _summarize_shell(
    *,
    command: str,
    stdout_text: str,
    stdout_lines: list[str],
    stderr_text: str,
    stderr_lines: list[str],
    exit_code: int | None,
    truncated_streams: list[str],
    timeout: bool,
) -> str:
    """Build one compact, deterministic summary line for shell output."""
    bits: list[str] = ["untrusted shell output"]
    if command:
        bits.append(f"from {_quote_field(command)}")
    bits.append(f"{len(stdout_lines)} line(s), {len(stdout_text)} char(s)")
    if stderr_lines:
        bits.append(f"stderr: {len(stderr_lines)} line(s), {len(stderr_text)} char(s)")
    if exit_code is not None:
        bits.append(f"exit {exit_code}")
    if timeout:
        bits.append("timed out")
    if truncated_streams:
        bits.append(f"truncated ({', '.join(truncated_streams)})")
    first = _first_non_empty(stdout_lines)
    if first:
        bits.append(f"first: {_quote_field(first)}")
    last = stdout_lines[-1] if stdout_lines else ""
    if last and last != first:
        bits.append(f"last: {_quote_field(last)}")
    if stderr_lines and not first:
        bits.append(f"stderr first: {_quote_field(_first_non_empty(stderr_lines))}")
    return "; ".join(bits)


#: Required field -> expected type per halctl document kind. The shapes
#: are the ones ``halctl`` emits (model dumps of the hal_client v1
#: schemas; see src/ozzgraph/halctl.py and src/ozzgraph/hal_client.py).
#: ``error`` and ``scoreboard`` are validated separately (nested shapes).
_HALCTL_DOC_SHAPES: dict[str, dict[str, type]] = {
    "challenge": {
        "id": str,
        "title": str,
        "description": str,
        "category": str,
        "points": int,
        "solved": bool,
    },
    "status": {
        "challenge_id": str,
        "solved": bool,
        "attempts": int,
        "hints_used": int,
        "points_earned": int,
    },
    "submission": {"challenge_id": str, "accepted": bool, "message": str, "points": int},
    "hint": {"challenge_id": str, "index": int, "hint": str, "paid": bool},
    "scoreboard": {"entries": list},
    "exit": {"exited": bool, "reason": str},
}


def _classify_halctl_document(doc: Mapping[str, object]) -> str:
    """Classify a parsed halctl document by its top-level shape.

    Deterministic: checked in a fixed order, error first. Unknown
    shapes yield ``"unknown"`` rather than crashing.
    """
    if "error" in doc:
        return "error"
    if "entries" in doc:
        return "scoreboard"
    if "exited" in doc:
        return "exit"
    if "id" in doc and "title" in doc and "description" in doc:
        return "challenge"
    if "accepted" in doc and "challenge_id" in doc:
        return "submission"
    if "hint" in doc and "challenge_id" in doc:
        return "hint"
    if "attempts" in doc and "challenge_id" in doc and "solved" in doc:
        return "status"
    return "unknown"


def _validate_halctl_document(doc: Mapping[str, object], kind: str) -> str | None:
    """Check a classified document against its halctl shape.

    Returns a ``parse_error`` description for the first violation, or
    None when the document is structurally valid. ``halctl`` output is
    our own normalized JSON, so a document that fails these checks is a
    poisoned or foreign stream — surfaced as ``malformed``, never
    raised.
    """
    if kind == "error":
        error = doc.get("error")
        if not isinstance(error, Mapping):
            return f"error document must carry an object payload, got {_type_name(error)}"
        return _check_fields({"type": str, "message": str}, error)
    expected = _HALCTL_DOC_SHAPES.get(kind)
    if expected is None:
        return None  # unknown shapes have no shape contract
    error = _check_fields(expected, doc)
    if error is not None:
        return error
    if kind == "scoreboard":
        entries = doc["entries"]
        assert isinstance(entries, list)
        if entries:
            first = entries[0]
            if not isinstance(first, Mapping):
                return f"scoreboard entry must be an object, got {_type_name(first)}"
            return _check_fields({"user_id": str, "points": int}, first)
    return None


def _check_fields(expected: Mapping[str, type], payload: Mapping[str, object]) -> str | None:
    """First field whose value is missing or not the expected type."""
    for key, expected_type in expected.items():
        value = payload.get(key)
        if isinstance(value, expected_type):
            continue
        actual = "missing" if key not in payload else _type_name(value)
        return f"field {key!r} must be {expected_type.__name__}, got {actual}"
    return None


def _type_name(value: object) -> str:
    """Name of ``value``'s type; 'None' for None."""
    return "None" if value is None else type(value).__name__


def _parse_single_json_document(text: str) -> tuple[dict[str, object] | None, str | None]:
    """Parse exactly one JSON object document.

    Returns ``(payload, None)`` on success, ``(None, parse_error)`` on
    failure. Empty output, trailing garbage, multiple documents, and
    non-object documents are all reported as structured errors — the
    parser never guesses which document was meant (fail loudly).
    """
    stripped = text.strip()
    if not stripped:
        return None, "empty output: expected exactly one JSON document"
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"expected a JSON object document, got {_type_name(payload)}"
    return payload, None


def _halctl_ok(exit_code: int | None, kind: str) -> bool | None:
    """The ok flag for a valid halctl document.

    True when halctl exited 0 with a non-error document; False for
    error documents and nonzero exits; None when the exit code is
    unknown (plain str input) and the document is not an error.
    """
    if exit_code is not None:
        return exit_code == 0 and kind != "error"
    return False if kind == "error" else None


def _s(payload: Mapping[str, object], key: str) -> str:
    """Typed string field (validated by :func:`_validate_halctl_document`)."""
    value = payload[key]
    assert isinstance(value, str)
    return value


def _i(payload: Mapping[str, object], key: str) -> int:
    """Typed int field (validated by :func:`_validate_halctl_document`)."""
    value = payload[key]
    assert isinstance(value, int)
    return value


def _b(payload: Mapping[str, object], key: str) -> bool:
    """Typed bool field (validated by :func:`_validate_halctl_document`)."""
    value = payload[key]
    assert isinstance(value, bool)
    return value


def _summarize_halctl(doc: Mapping[str, object], kind: str) -> str:
    """One compact, deterministic summary line per halctl document kind."""
    if kind == "error":
        error = cast(Mapping[str, object], doc["error"])
        bits = ["halctl error", _s(error, "type"), _quote_field(_s(error, "message"))]
        extras: list[str] = []
        provider = error.get("provider")
        if isinstance(provider, str):
            extras.append(f"provider={provider}")
        status_code = error.get("status_code")
        if isinstance(status_code, int):
            extras.append(f"status={status_code}")
        retryable = error.get("retryable")
        if isinstance(retryable, bool):
            extras.append(f"retryable={retryable}")
        if extras:
            bits.append(f"({', '.join(extras)})")
        return " ".join(bits)
    if kind == "challenge":
        return (
            f"halctl challenge {_quote_field(_s(doc, 'id'))}: "
            f"{_quote_field(_s(doc, 'title'))} "
            f"({_s(doc, 'category')}, {_i(doc, 'points')} pts, "
            f"solved={_b(doc, 'solved')})"
        )
    if kind == "status":
        return (
            f"halctl status {_quote_field(_s(doc, 'challenge_id'))}: "
            f"solved={_b(doc, 'solved')}, attempts={_i(doc, 'attempts')}, "
            f"hints={_i(doc, 'hints_used')}, points={_i(doc, 'points_earned')}"
        )
    if kind == "submission":
        return (
            f"halctl submission {_quote_field(_s(doc, 'challenge_id'))}: "
            f"accepted={_b(doc, 'accepted')}; {_quote_field(_s(doc, 'message'))}"
        )
    if kind == "hint":
        return (
            f"halctl hint {_quote_field(_s(doc, 'challenge_id'))} "
            f"#{_i(doc, 'index')}: {_quote_field(_s(doc, 'hint'))} "
            f"(paid={_b(doc, 'paid')})"
        )
    if kind == "scoreboard":
        entries = doc["entries"]
        assert isinstance(entries, list)
        bits = [f"halctl scoreboard: {len(entries)} entries"]
        if entries:
            first = cast(Mapping[str, object], entries[0])
            bits.append(f"top: {_quote_field(_s(first, 'user_id'))} ({_i(first, 'points')} pts)")
        return "; ".join(bits)
    if kind == "exit":
        return f"halctl exit: exited={_b(doc, 'exited')}, reason={_quote_field(_s(doc, 'reason'))}"
    return f"halctl document: {len(doc)} top-level fields"


def _malformed_halctl(
    *,
    action_id: str,
    exit_code: int | None,
    truncated_streams: list[str],
    text: str,
    parse_error: str,
) -> Observation:
    """A structured failure observation (fail loudly, never raise).

    A malformed observation is never ``ok`` even when the exit code is
    0 — its payload is unusable; the exit code is still carried as
    data. The diagnostic excerpt is bounded so hostile output cannot
    bloat model context.
    """
    return Observation(
        action_id=action_id,
        source="halctl",
        kind="json",
        summary=f"malformed halctl output: {parse_error}",
        data={"excerpt": _bounded(text, EXCERPT_LIMIT)},
        truncated_streams=truncated_streams,
        exit_code=exit_code,
        ok=False if exit_code is not None else None,
        malformed=True,
        parse_error=parse_error,
    )


class HalctlJsonParser(Parser):
    """Parse halctl's single-JSON-document output into structured data.

    Handles the document shapes emitted by ``halctl`` (challenge show /
    status / submit / hint / scoreboard / exit / error; see
    :mod:`ozzgraph.halctl`), classifying each document and validating
    its shape. Malformed JSON, non-object documents, trailing garbage,
    and shape violations become observations with ``malformed=True`` and
    a structured ``parse_error`` — never raised exceptions (fail loudly
    = a structured result, the pipeline keeps running).
    """

    source: ClassVar[str] = "halctl"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one halctl JSON document into a classified observation."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, _timeout = _tool_metadata(raw)
        payload, parse_error = _parse_single_json_document(text)
        if payload is None:
            return _malformed_halctl(
                action_id=action_id,
                exit_code=exit_code,
                truncated_streams=truncated_streams,
                text=text,
                parse_error=parse_error or "unparseable output",
            )
        kind = _classify_halctl_document(payload)
        validation_error = _validate_halctl_document(payload, kind)
        if validation_error is not None:
            return _malformed_halctl(
                action_id=action_id,
                exit_code=exit_code,
                truncated_streams=truncated_streams,
                text=text,
                parse_error=validation_error,
            )
        return Observation(
            action_id=action_id,
            source=f"halctl:{kind}",
            kind=self.kind,
            summary=_summarize_halctl(payload, kind),
            data=dict(payload),
            truncated_streams=truncated_streams,
            exit_code=exit_code,
            ok=_halctl_ok(exit_code, kind),
        )


#: Deterministic registry: (source, kind) -> parser. Populated at import
#: with the built-in parsers; extensible via :func:`register_parser`
#: (explicit registration only — no discovery, AGENTS.md rule #10).
PARSERS: dict[tuple[str, str], Parser] = {}


def register_parser(parser: Parser) -> None:
    """Register ``parser`` under its (source, kind) key.

    Raises:
        ParserRegistryError: If the parser's ``source`` / ``kind`` are
            missing or empty, or a parser is already registered for the
            key (duplicate registration fails loudly).
    """
    source = getattr(parser, "source", None)
    kind = getattr(parser, "kind", None)
    if not isinstance(source, str) or not source or not isinstance(kind, str) or not kind:
        raise ParserRegistryError(
            f"parser must declare non-empty str source and kind, got {source!r} / {kind!r}"
        )
    key = (source, kind)
    if key in PARSERS:
        raise ParserRegistryError(
            f"a parser is already registered for source={source!r} kind={kind!r}"
        )
    PARSERS[key] = parser


def get_parser(source: str, kind: str) -> Parser:
    """The parser registered for ``source`` / ``kind``.

    Raises:
        ParserRegistryError: If no parser is registered for the key.
    """
    try:
        return PARSERS[(source, kind)]
    except KeyError:
        raise ParserRegistryError(
            f"no parser registered for source={source!r} kind={kind!r}"
        ) from None


#: The built-in shell text parser (source="shell", kind="text").
SHELL_TEXT_PARSER = ShellTextParser()

#: The built-in halctl JSON parser (source="halctl", kind="json").
HALCTL_JSON_PARSER = HalctlJsonParser()

register_parser(SHELL_TEXT_PARSER)
register_parser(HALCTL_JSON_PARSER)
