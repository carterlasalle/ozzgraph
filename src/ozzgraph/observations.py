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

V04 semantic parsers (docs/CHANGES_v2.md milestone 4,
docs/OBSERVATIONS.md): one typed parser per high-value tool, consuming
the tool's machine-readable output — XML (nmap), JSON (ffuf,
feroxbuster, semgrep, trivy, gitleaks, exiftool), SARIF (semgrep,
CodeQL), JSONL (nuclei, netexec), LDIF (ldapsearch), and structured
text (curl, smbmap, file, readelf, checksec, binwalk). Every parser
produces a TYPED observation payload (hosts, ports, findings, vulns,
sections, ...) instead of prose, and the raw output is ALWAYS persisted
to the artifact store BEFORE parsing (the raw-first invariant enforced
by the runner; parsers themselves never perform I/O). Dispatch happens
deterministically from the producing command via
:func:`parser_for_command` — flag-gated tools (nmap ``-oX``, nuclei
``-jsonl``, semgrep ``--sarif``, ...) map to their semantic parser only
when the machine-readable format was actually requested, and everything
else falls back to :class:`ShellTextParser`.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
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
#: ``error`` and ``scoreboard`` are validated separately (nested shapes);
#: the ``ctfs`` / ``challenges`` list documents (V09, the official tool
#: set) are validated at the entry level like ``scoreboard``.
_HALCTL_DOC_SHAPES: dict[str, dict[str, type]] = {
    "ctfs": {"ctfs": list},
    "challenges": {"challenges": list},
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
    if "ctfs" in doc:
        return "ctfs"
    if "challenges" in doc:
        return "challenges"
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
    if kind == "ctfs":
        ctfs = doc["ctfs"]
        assert isinstance(ctfs, list)
        if ctfs:
            first = ctfs[0]
            if not isinstance(first, Mapping):
                return f"ctf entry must be an object, got {_type_name(first)}"
            return _check_fields({"id": str, "name": str, "challenge_count": int}, first)
    if kind == "challenges":
        challenges = doc["challenges"]
        assert isinstance(challenges, list)
        if challenges:
            first = challenges[0]
            if not isinstance(first, Mapping):
                return f"challenge entry must be an object, got {_type_name(first)}"
            return _check_fields({"id": str, "title": str, "points": int}, first)
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
    if kind == "ctfs":
        ctfs = doc["ctfs"]
        assert isinstance(ctfs, list)
        bits = [f"halctl ctfs: {len(ctfs)} competitions"]
        if ctfs:
            first = cast(Mapping[str, object], ctfs[0])
            bits.append(
                f"first: {_quote_field(_s(first, 'id'))} "
                f"({_i(first, 'challenge_count')} challenges)"
            )
        return "; ".join(bits)
    if kind == "challenges":
        challenges = doc["challenges"]
        assert isinstance(challenges, list)
        bits = [f"halctl challenges: {len(challenges)} challenges"]
        if challenges:
            first = cast(Mapping[str, object], challenges[0])
            bits.append(
                f"first: {_quote_field(_s(first, 'id'))} "
                f"{_quote_field(_s(first, 'title'))} ({_i(first, 'points')} pts)"
            )
        return "; ".join(bits)
    if kind == "exit":
        return f"halctl exit: exited={_b(doc, 'exited')}, reason={_quote_field(_s(doc, 'reason'))}"
    return f"halctl document: {len(doc)} top-level fields"


def _malformed(
    *,
    source: str,
    kind: str,
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
    bloat model context. Shared by every semantic parser
    (V04); the summary reads ``malformed <source> output: <error>``.
    """
    return Observation(
        action_id=action_id,
        source=source,
        kind=kind,
        summary=f"malformed {source} output: {parse_error}",
        data={"excerpt": _bounded(text, EXCERPT_LIMIT)},
        truncated_streams=truncated_streams,
        exit_code=exit_code,
        ok=False if exit_code is not None else None,
        malformed=True,
        parse_error=parse_error,
    )


def _malformed_halctl(
    *,
    action_id: str,
    exit_code: int | None,
    truncated_streams: list[str],
    text: str,
    parse_error: str,
) -> Observation:
    """A structured halctl failure observation (backward-compatible)."""
    return _malformed(
        source="halctl",
        kind="json",
        action_id=action_id,
        exit_code=exit_code,
        truncated_streams=truncated_streams,
        text=text,
        parse_error=parse_error,
    )


class HalctlJsonParser(Parser):
    """Parse halctl's single-JSON-document output into structured data.

    Handles the document shapes emitted by ``halctl`` (ctfs /
    challenges / challenge show / status / submit / hint / scoreboard /
    exit / error; see :mod:`ozzgraph.halctl`), classifying each document
    and validating its shape. Malformed JSON, non-object documents,
    trailing garbage, and shape violations become observations with
    ``malformed=True`` and a structured ``parse_error`` — never raised
    exceptions (fail loudly = a structured result, the pipeline keeps
    running).
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


# ---------------------------------------------------------------------------
# V04 semantic observations — shared helpers
# (docs/CHANGES_v2.md milestone 4, docs/OBSERVATIONS.md)
# ---------------------------------------------------------------------------


def _ok(exit_code: int | None, timeout: bool) -> bool | None:
    """The ok flag for a valid semantic parse.

    False on timeout or nonzero exit; None when the exit code is
    unknown (plain str input); True only for a zero exit with no
    timeout. Malformed observations are handled separately (never ok
    when the exit code is known).
    """
    if timeout:
        return False
    if exit_code is None:
        return None
    return exit_code == 0


def _count_by(values: Sequence[str]) -> dict[str, int]:
    """First-seen counts of non-empty values (e.g. severities)."""
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _render_counts(counts: Mapping[str, int], limit: int = 4) -> str:
    """``high:2, medium:1`` from a counts map (sorted, bounded)."""
    parts = [f"{key}:{counts[key]}" for key in sorted(counts)]
    if len(parts) > limit:
        parts = parts[:limit]
        parts.append("...")
    return ", ".join(parts) if parts else "none"


def _as_int(value: object) -> int | None:
    """A typed int from an untrusted JSON value (no bool/None coercion)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_float(value: object) -> float | None:
    """A typed float from an untrusted JSON value (no bool coercion)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _str_field(payload: Mapping[str, object], key: str) -> str:
    """The string value of ``key``, or '' when absent/wrong-typed."""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _parse_json_value(text: str) -> tuple[object, str | None]:
    """Parse one JSON document of ANY shape (object, array, scalar).

    Returns ``(value, None)`` on success and ``(None, parse_error)`` on
    failure; empty output and trailing garbage are structured errors —
    the parser never guesses which document was meant (fail loudly).
    """
    stripped = text.strip()
    if not stripped:
        return None, "empty output: expected a JSON document"
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def _parse_jsonl(text: str) -> tuple[list[Mapping[str, object]], str | None]:
    """Parse strict JSONL: one JSON object per non-blank line.

    A non-object line is a structured error naming the line number —
    the parser never skips or guesses at a broken record (fail loudly).
    """
    stripped = text.strip()
    if not stripped:
        return [], "empty output: expected JSONL"
    documents: list[Mapping[str, object]] = []
    for line_number, line in enumerate(stripped.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            return [], f"line {line_number}: invalid JSON: {exc}"
        if not isinstance(value, dict):
            return [], f"line {line_number}: expected a JSON object, got {_type_name(value)}"
        documents.append(value)
    if not documents:
        return [], "empty output: expected JSONL"
    return documents, None


def _text_fallback_data(text: str) -> dict[str, object]:
    """Line-based labeled data for unrecognized text output.

    Mirrors the :class:`ShellTextParser` data shape so downstream reads
    stay uniform: hostile or unexpected output becomes labeled data
    (AGENTS.md Security Boundaries), never an instruction.
    """
    lines = text.splitlines()
    return {
        "line_count": len(lines),
        "char_count": len(text),
        "first_line": _first_non_empty(lines),
        "last_line": lines[-1] if lines else "",
    }


def _semantic_observation(
    *,
    parser: Parser,
    text: str,
    action_id: str,
    exit_code: int | None,
    truncated_streams: list[str],
    timeout: bool,
    data: dict[str, object],
    parse_error: str | None,
    summary: str,
) -> Observation:
    """The common parser -> Observation plumbing for semantic parsers.

    A ``parse_error`` becomes a structured malformed observation (the
    raw excerpt is preserved, never raised); otherwise the typed
    ``data`` payload and deterministic ``summary`` are attached with
    execution metadata carried through from the ToolResult.
    """
    if parse_error is not None:
        return _malformed(
            source=parser.source,
            kind=parser.kind,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            text=text,
            parse_error=parse_error,
        )
    return Observation(
        action_id=action_id,
        source=parser.source,
        kind=parser.kind,
        summary=summary,
        data=data,
        truncated_streams=truncated_streams,
        exit_code=exit_code,
        ok=_ok(exit_code, timeout),
    )


# ---------------------------------------------------------------------------
# curl
# ---------------------------------------------------------------------------

#: HTTP status line, e.g. ``HTTP/1.1 200 OK`` (also ``HTTP/2 200``).
_CURL_STATUS_RE = re.compile(r"^HTTP/(\d(?:\.\d)?)\s+(\d{3})(?:\s+(.*))?$")

#: Write-out JSON keys that identify a curl ``-w '%{json}'`` document.
_CURL_WRITE_OUT_KEYS: frozenset[str] = frozenset(
    {
        "response_code",
        "http_code",
        "url_effective",
        "redirect_url",
        "content_type",
        "size_download",
    }
)


def _parse_curl(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize curl output into typed response data.

    Three deterministic shapes, detected in order:

    - ``-w '%{json}'`` write-out document: status/URL/redirect typed
      fields (kind ``write_out_json``).
    - Response headers (``-i`` / ``-D -``): status line, ordered header
      pairs, and body statistics (kind ``headers``). Header values are
      bounded; hostile header content stays labeled data.
    - Anything else: line-based labeled text (kind ``text``).
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        value, _ = _parse_json_value(stripped)
        if isinstance(value, Mapping) and any(key in value for key in _CURL_WRITE_OUT_KEYS):
            return _curl_write_out_data(value), None
    lines = text.splitlines()
    status_index = -1
    status_match: re.Match[str] | None = None
    for index, line in enumerate(lines):
        match = _CURL_STATUS_RE.match(line.strip())
        if match is not None:
            status_index = index
            status_match = match
            break
    if status_match is not None:
        return _curl_headers_data(lines, status_index, status_match), None
    data = _text_fallback_data(text)
    data["format"] = "text"
    return data, None


def _curl_write_out_data(doc: Mapping[str, object]) -> dict[str, object]:
    """Typed fields from a curl ``-w '%{json}'`` document."""
    return {
        "format": "write_out_json",
        "response_code": _as_int(doc.get("response_code") or doc.get("http_code")),
        "url": _str_field(doc, "url") or _str_field(doc, "url_effective"),
        "url_effective": _str_field(doc, "url_effective"),
        "redirect_url": _str_field(doc, "redirect_url"),
        "content_type": _str_field(doc, "content_type"),
        "method": _str_field(doc, "method"),
        "http_version": _str_field(doc, "http_version"),
        "remote_ip": _str_field(doc, "remote_ip"),
        "remote_port": _as_int(doc.get("remote_port")),
        "size_download": _as_int(doc.get("size_download")),
        "num_redirects": _as_int(doc.get("num_redirects")),
        "time_total": _as_float(doc.get("time_total")),
    }


def _curl_headers_data(
    lines: list[str],
    status_index: int,
    status_match: re.Match[str],
) -> dict[str, object]:
    """Typed fields from a curl response-header block (``-i`` / ``-D -``)."""
    http_version, status_code, reason = status_match.groups()
    headers: list[dict[str, str]] = []
    body_lines: list[str] = []
    in_headers = True
    for line in lines[status_index + 1 :]:
        if in_headers and line.strip() == "":
            in_headers = False
            continue
        if in_headers:
            name, separator, value = line.partition(":")
            if separator and name.strip():
                headers.append(
                    {
                        "name": _bounded(_visible(name.strip()), SUMMARY_FIELD_LIMIT),
                        "value": _bounded(_visible(value.strip()), SUMMARY_FIELD_LIMIT),
                    }
                )
            continue
        body_lines.append(line)
    return {
        "format": "headers",
        "http_version": http_version,
        "status_code": _as_int(status_code),
        "reason": _bounded(_visible(reason or ""), SUMMARY_FIELD_LIMIT),
        "header_count": len(headers),
        "headers": headers,
        "body_line_count": len(body_lines),
        "body_char_count": sum(len(line) for line in body_lines),
        "body_first_line": _bounded(_visible(_first_non_empty(body_lines)), SUMMARY_FIELD_LIMIT),
    }


def _summarize_curl(data: Mapping[str, object]) -> str:
    """One compact, deterministic summary line per curl shape."""
    format_kind = data.get("format")
    if format_kind == "write_out_json":
        code = data.get("response_code")
        url = data.get("url")
        bits = ["curl write-out"]
        if isinstance(code, int):
            bits.append(f"status {code}")
        if isinstance(url, str) and url:
            bits.append(f"from {_quote_field(url)}")
        size = data.get("size_download")
        if isinstance(size, int):
            bits.append(f"{size} bytes")
        return "; ".join(bits)
    if format_kind == "headers":
        code = data.get("status_code")
        reason = data.get("reason")
        header_count = data.get("header_count")
        body_lines = data.get("body_line_count")
        bits = ["curl response"]
        if isinstance(code, int):
            bits.append(f"status {code}")
        if isinstance(reason, str) and reason:
            bits.append(_quote_field(reason))
        if isinstance(header_count, int):
            bits.append(f"{header_count} header(s)")
        if isinstance(body_lines, int):
            bits.append(f"{body_lines} body line(s)")
        return "; ".join(bits)
    return "untrusted curl output; " + "; ".join(
        f"{key}: {value}"
        for key, value in (
            ("line_count", data.get("line_count")),
            ("char_count", data.get("char_count")),
        )
        if isinstance(value, int)
    )


class CurlTextParser(Parser):
    """Normalize curl output into typed response data (V04).

    Handles the ``-w '%{json}'`` write-out document, response header
    blocks (``-i`` / ``-D -``), and plain body text. Always parseable:
    unrecognized output becomes line-based labeled data
    (``malformed`` stays False), never a raised error.
    """

    source: ClassVar[str] = "curl"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one curl stdout into typed response fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_curl(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_curl(data),
        )


# ---------------------------------------------------------------------------
# nmap (XML, -oX)
# ---------------------------------------------------------------------------


def _parse_nmap_xml(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize an nmap XML document (``-oX -``) into typed host data.

    Entity declarations are rejected up front and the parser itself is
    configured to forbid DTDs/entities/external references, so hostile
    XML can never expand entities (untrusted input, AGENTS.md Security
    Boundaries).
    """
    stripped = text.strip()
    if not stripped:
        return {}, "empty output: expected an nmap XML document"
    # Internal entity definitions are the billion-laughs vector; a bare
    # ``<!DOCTYPE name>`` (no internal subset) is harmless because the
    # default ElementTree parser never resolves external DTDs.
    if "<!ENTITY" in text:
        return {}, "XML entity declarations are rejected (untrusted input)"
    if "<!DOCTYPE" in text and "[" in text:
        return {}, "XML internal DTD subsets are rejected (untrusted input)"
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        return {}, f"invalid XML: {exc}"
    if root.tag != "nmaprun":
        return {}, f"expected an <nmaprun> root element, got <{root.tag}>"
    hosts: list[dict[str, object]] = []
    open_ports: list[str] = []
    port_count = 0
    for host in root.findall("host"):
        hosts.append(_nmap_host_data(host, open_ports))
        port_count += len(_nmap_host_ports(host))
    scaninfo: list[dict[str, object]] = []
    for info in root.findall("scaninfo"):
        scaninfo.append(
            {
                "type": info.attrib.get("type", ""),
                "protocol": info.attrib.get("protocol", ""),
                "services": info.attrib.get("services", ""),
                "numservices": _as_int(info.attrib.get("numservices")),
            }
        )
    return {
        "scanner": root.attrib.get("scanner", "nmap"),
        "version": root.attrib.get("version", ""),
        "args": _bounded(root.attrib.get("args", ""), 200),
        "scaninfo": scaninfo,
        "host_count": len(hosts),
        "hosts": hosts,
        "port_count": port_count,
        "open_ports": open_ports[:50],
    }, None


def _nmap_host_ports(host: ET.Element) -> list[ET.Element]:
    """The ``<port>`` elements of one nmap host element."""
    return list(host.findall("ports/port"))


def _nmap_host_data(host: ET.Element, open_ports: list[str]) -> dict[str, object]:
    """Typed per-host fields: status, addresses, ports, OS matches."""
    status = host.find("status")
    addresses = [
        {"addr": address.attrib.get("addr", ""), "addrtype": address.attrib.get("addrtype", "")}
        for address in host.findall("address")
    ]
    hostnames = [
        name.attrib.get("name", "")
        for name in host.findall("hostnames/hostname")
        if name.attrib.get("name")
    ]
    ports: list[dict[str, object]] = []
    for port in _nmap_host_ports(host):
        port_state = port.find("state")
        service = port.find("service")
        scripts = [
            {
                "id": script.attrib.get("id", ""),
                "output": _bounded(_visible(script.attrib.get("output", "")), 200),
            }
            for script in port.findall("script")
        ]
        portid = port.attrib.get("portid", "")
        portid_int = _as_int(portid)
        service_name = service.attrib.get("name", "") if service is not None else ""
        state = port_state.attrib.get("state", "") if port_state is not None else ""
        if state == "open":
            open_ports.append(f"{port.attrib.get('protocol', '')}/{portid}")
        ports.append(
            {
                "protocol": port.attrib.get("protocol", ""),
                "portid": portid_int if portid_int is not None else portid,
                "state": state,
                "service": {
                    "name": service_name,
                    "product": service.attrib.get("product", "") if service is not None else "",
                    "version": service.attrib.get("version", "") if service is not None else "",
                    "extrainfo": (
                        service.attrib.get("extrainfo", "") if service is not None else ""
                    ),
                },
                "script_count": len(scripts),
                "scripts": scripts,
            }
        )
    os_matches = [
        {
            "name": match.attrib.get("name", ""),
            "accuracy": _as_int(match.attrib.get("accuracy")),
        }
        for match in host.findall("os/osmatch")
    ]
    return {
        "status": status.attrib.get("state", "") if status is not None else "",
        "addresses": addresses,
        "hostnames": hostnames,
        "port_count": len(ports),
        "ports": ports,
        "os_match_count": len(os_matches),
        "os_matches": os_matches,
    }


def _summarize_nmap(data: Mapping[str, object]) -> str:
    """One compact summary line for an nmap scan document."""
    bits = ["nmap scan"]
    host_count = data.get("host_count")
    if isinstance(host_count, int):
        bits.append(f"{host_count} host(s)")
    open_ports = data.get("open_ports")
    if isinstance(open_ports, list) and open_ports:
        sample = ", ".join(str(port) for port in open_ports[:5])
        bits.append(f"open: {sample}")
        if len(open_ports) > 5:
            bits.append(f"+{len(open_ports) - 5} more")
    args = data.get("args")
    if isinstance(args, str) and args:
        bits.append(f"args={_quote_field(args)}")
    return "; ".join(bits)


class NmapXmlParser(Parser):
    """Normalize an nmap XML document (``-oX -``) into typed host data.

    Extracts per-host status/addresses/hostnames, ports (protocol,
    portid, state, service product/version, scripts), and OS matches
    into typed observation fields. Invalid XML, foreign roots, and
    entity-declaring documents become ``malformed=True`` observations —
    never raised exceptions.
    """

    source: ClassVar[str] = "nmap"
    kind: ClassVar[str] = "xml"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one nmap XML document into typed host fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_nmap_xml(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_nmap(data),
        )


# ---------------------------------------------------------------------------
# ffuf (JSON, -json / -of json)
# ---------------------------------------------------------------------------


def _ffuf_result(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-result fields from one ffuf result object."""
    input_map = item.get("input")
    return {
        "input": dict(input_map) if isinstance(input_map, Mapping) else None,
        "position": _as_int(item.get("position")),
        "status": _as_int(item.get("status")),
        "length": _as_int(item.get("length")),
        "words": _as_int(item.get("words")),
        "lines": _as_int(item.get("lines")),
        "url": _str_field(item, "url"),
        "content_type": _str_field(item, "contenttype") or _str_field(item, "content_type"),
        "redirect_location": _str_field(item, "redirectlocation")
        or _str_field(item, "redirect_location"),
        "duration": _as_int(item.get("duration")),
        "host": _str_field(item, "host"),
    }


def _parse_ffuf(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize ffuf JSON output into typed discovery results."""
    value, parse_error = _parse_json_value(text)
    if parse_error is not None:
        return {}, parse_error
    if isinstance(value, Mapping):
        raw_results = value.get("results")
        if not isinstance(raw_results, list):
            return {}, "ffuf document must carry a results array"
    elif isinstance(value, list):
        raw_results = value
    else:
        return {}, f"expected an ffuf results array or document, got {_type_name(value)}"
    results = [entry for entry in raw_results if isinstance(entry, Mapping)]
    statuses = [entry.get("status") for entry in results]
    status_counts = _count_by([str(status) for status in statuses if isinstance(status, int)])
    return {
        "result_count": len(results),
        "results": [_ffuf_result(entry) for entry in results],
        "status_counts": status_counts,
    }, None


def _summarize_ffuf(data: Mapping[str, object]) -> str:
    """One compact summary line for an ffuf discovery run."""
    result_count = data.get("result_count")
    bits = ["ffuf discovery"]
    if isinstance(result_count, int):
        bits.append(f"{result_count} result(s)")
    status_counts = data.get("status_counts")
    if isinstance(status_counts, Mapping) and status_counts:
        bits.append(f"statuses: {_render_counts(status_counts)}")
    return "; ".join(bits)


class FfufJsonParser(Parser):
    """Normalize ffuf JSON output (``-json`` / ``-of json``).

    Extracts each result's typed fields (URL, status, length, words,
    lines, content type, redirect) plus a status histogram. Broken JSON
    or a missing results array becomes ``malformed=True``.
    """

    source: ClassVar[str] = "ffuf"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one ffuf JSON document into typed result fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_ffuf(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_ffuf(data),
        )


# ---------------------------------------------------------------------------
# feroxbuster (JSON, --json / -o out.json)
# ---------------------------------------------------------------------------


def _feroxbuster_result(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-result fields from one feroxbuster result object."""
    header_map = item.get("header")
    technologies = item.get("technologies")
    return {
        "url": _str_field(item, "url"),
        "status": _as_int(item.get("status")),
        "content_length": _as_int(item.get("content_length")),
        "content_type": _str_field(item, "content_type"),
        "method": _str_field(item, "method"),
        "words": _as_int(item.get("words")),
        "lines": _as_int(item.get("lines")),
        "wildcard": item.get("wildcard") if isinstance(item.get("wildcard"), bool) else None,
        "header_count": len(header_map) if isinstance(header_map, Mapping) else 0,
        "headers": (
            [
                {
                    "name": _bounded(_visible(name), SUMMARY_FIELD_LIMIT),
                    "value": _bounded(_visible(str(value)), SUMMARY_FIELD_LIMIT),
                }
                for name, value in header_map.items()
                if isinstance(name, str)
            ]
            if isinstance(header_map, Mapping)
            else []
        ),
        "technologies": (
            [str(tech) for tech in technologies if isinstance(tech, str)]
            if isinstance(technologies, list)
            else []
        ),
    }


def _parse_feroxbuster(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize feroxbuster JSON output into typed discovery results."""
    value, parse_error = _parse_json_value(text)
    if parse_error is not None:
        return {}, parse_error
    if not isinstance(value, list):
        return {}, f"expected a feroxbuster results array, got {_type_name(value)}"
    results = [entry for entry in value if isinstance(entry, Mapping)]
    statuses = [entry.get("status") for entry in results]
    status_counts = _count_by([str(status) for status in statuses if isinstance(status, int)])
    return {
        "result_count": len(results),
        "results": [_feroxbuster_result(entry) for entry in results],
        "status_counts": status_counts,
    }, None


def _summarize_feroxbuster(data: Mapping[str, object]) -> str:
    """One compact summary line for a feroxbuster discovery run."""
    result_count = data.get("result_count")
    bits = ["feroxbuster discovery"]
    if isinstance(result_count, int):
        bits.append(f"{result_count} result(s)")
    status_counts = data.get("status_counts")
    if isinstance(status_counts, Mapping) and status_counts:
        bits.append(f"statuses: {_render_counts(status_counts)}")
    return "; ".join(bits)


class FeroxbusterJsonParser(Parser):
    """Normalize feroxbuster JSON output (``--json`` / ``-o out.json``).

    Extracts each result's typed fields (URL, status, content length,
    headers, technologies) plus a status histogram. Broken JSON or a
    non-array document becomes ``malformed=True``.
    """

    source: ClassVar[str] = "feroxbuster"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one feroxbuster JSON document into typed result fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_feroxbuster(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_feroxbuster(data),
        )


# ---------------------------------------------------------------------------
# nuclei (JSONL, -jsonl / -json)
# ---------------------------------------------------------------------------


def _matcher_status(doc: Mapping[str, object]) -> str:
    """nuclei's matcher-status as a string (bool or str, '' when absent)."""
    value = doc.get("matcher-status", doc.get("matcher_status"))
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    return ""


def _nuclei_finding(doc: Mapping[str, object]) -> dict[str, object]:
    """Typed per-finding fields from one nuclei JSONL record."""
    info = doc.get("info")
    info_map = info if isinstance(info, Mapping) else {}
    tags = info_map.get("tags")
    return {
        "template_id": _str_field(doc, "template-id") or _str_field(doc, "template_id"),
        "name": _str_field(info_map, "name"),
        "severity": _str_field(info_map, "severity"),
        "tags": [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else [],
        "type": _str_field(doc, "type"),
        "host": _str_field(doc, "host"),
        "matched_at": _str_field(doc, "matched-at") or _str_field(doc, "matched_at"),
        "matcher_status": _matcher_status(doc),
        "ip": _str_field(doc, "ip"),
        "timestamp": _str_field(doc, "timestamp"),
        "curl_command": _bounded(
            _str_field(doc, "curl-command") or _str_field(doc, "curl_command"), 200
        ),
    }


def _parse_nuclei(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize nuclei JSONL output into typed finding fields."""
    documents, parse_error = _parse_jsonl(text)
    if parse_error is not None:
        return {}, parse_error
    findings = [_nuclei_finding(doc) for doc in documents]
    severities = [str(finding["severity"]) for finding in findings]
    return {
        "finding_count": len(findings),
        "findings": findings,
        "severity_counts": _count_by(severities),
    }, None


def _summarize_nuclei(data: Mapping[str, object]) -> str:
    """One compact summary line for a nuclei scan."""
    finding_count = data.get("finding_count")
    bits = ["nuclei scan"]
    if isinstance(finding_count, int):
        bits.append(f"{finding_count} finding(s)")
    severity_counts = data.get("severity_counts")
    if isinstance(severity_counts, Mapping) and severity_counts:
        bits.append(f"severities: {_render_counts(severity_counts)}")
    return "; ".join(bits)


class NucleiJsonlParser(Parser):
    """Normalize nuclei JSONL output (``-jsonl`` / ``-json``).

    Each JSON record becomes a typed finding (template id, name,
    severity, tags, host, matcher status). A broken line is a
    structured error naming the line — never skipped (fail loudly).
    """

    source: ClassVar[str] = "nuclei"
    kind: ClassVar[str] = "jsonl"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one nuclei JSONL stream into typed finding fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_nuclei(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_nuclei(data),
        )


# ---------------------------------------------------------------------------
# netexec (JSONL, --json)
# ---------------------------------------------------------------------------


def _netexec_host(doc: Mapping[str, object]) -> dict[str, object]:
    """Typed per-host fields from one netexec JSONL record."""
    details = doc.get("json_host")
    data_line = _str_field(doc, "data")
    return {
        "host": _str_field(doc, "host"),
        "port": _as_int(doc.get("port")),
        "protocol": _str_field(doc, "protocol"),
        "data": _bounded(_visible(data_line), 200),
        "details": dict(details) if isinstance(details, Mapping) else None,
    }


def _parse_netexec(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize netexec JSONL output (``--json``) into typed hosts."""
    documents, parse_error = _parse_jsonl(text)
    if parse_error is not None:
        return {}, parse_error
    hosts = [_netexec_host(doc) for doc in documents]
    protocols = _count_by([str(host["protocol"]) for host in hosts])
    return {"host_count": len(hosts), "hosts": hosts, "protocol_counts": protocols}, None


def _summarize_netexec(data: Mapping[str, object]) -> str:
    """One compact summary line for a netexec enumeration run."""
    host_count = data.get("host_count")
    bits = ["netexec enumeration"]
    if isinstance(host_count, int):
        bits.append(f"{host_count} host(s)")
    hosts = data.get("hosts")
    if isinstance(hosts, list) and hosts:
        first = hosts[0]
        if isinstance(first, Mapping):
            address = first.get("host")
            protocol = first.get("protocol")
            port = first.get("port")
            if isinstance(address, str) and address:
                bits.append(f"first: {_quote_field(address)}")
                if isinstance(protocol, str) and protocol:
                    bits.append(f"{protocol}:{port if isinstance(port, int) else '?'}")
    return "; ".join(bits)


class NetexecJsonlParser(Parser):
    """Normalize netexec JSONL output (``--json``) into typed hosts.

    Each record becomes a typed host (address, port, protocol, the
    module's data line, and the parsed ``json_host`` details object).
    A broken line is a structured error naming the line — never
    skipped (fail loudly).
    """

    source: ClassVar[str] = "netexec"
    kind: ClassVar[str] = "jsonl"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one netexec JSONL stream into typed host fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_netexec(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_netexec(data),
        )


# ---------------------------------------------------------------------------
# smbmap (text)
# ---------------------------------------------------------------------------

#: smbmap host banner: ``[+] IP: 10.0.0.1:445  Name: target.local`` (with
#: an optional trailing ``(domain:...)`` / ``(workgroup:...)`` note).
_SMBMAP_HOST_RE = re.compile(r"IP:\s*(\S+?):(\d+)\s+Name:\s*(\S+)(?:\s+\(([^)]+)\))?$")

#: smbmap share-table header row (``Sharename Type Comment``).
_SMBMAP_SHARE_HEADER_RE = re.compile(r"^Sharename\s+Type\s+Comment")

#: A smbmap shared-path line, e.g. ``\\10.0.0.1\share\`` or
#: ``\\10.0.0.1\share\dir\file.txt`` with optional permissions.
_SMBMAP_PATH_RE = re.compile(r"^\\\\([^\\]+)\\([^\\]+)\\(.*)$")

#: Permission markers on shared-path lines, e.g. ``(READ)(WRITE)``.
_SMBMAP_PERMS_RE = re.compile(r"\(([A-Z]+)\)")


def _parse_smbmap(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize smbmap text output into typed share/host data.

    Detects host banners, the share table (name/type/comment), shared
    paths with permissions, and ``[!]`` error lines. Unrecognized
    content degrades to line-based labeled data — smbmap output is
    text, so it stays parseable rather than malformed.
    """
    lines = text.splitlines()
    hosts: list[dict[str, object]] = []
    shares: list[dict[str, object]] = []
    paths: list[dict[str, object]] = []
    errors: list[str] = []
    in_share_table = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            in_share_table = False
            continue
        if line.startswith("[!"):
            errors.append(_bounded(_visible(line[3:].strip()), SUMMARY_FIELD_LIMIT))
            in_share_table = False
            continue
        stripped = line[3:].strip() if line.startswith("[+]") else line
        host_match = _SMBMAP_HOST_RE.search(stripped)
        if host_match is not None:
            ip, port, name, note = host_match.groups()
            hosts.append(
                {
                    "ip": ip,
                    "port": _as_int(port),
                    "name": name,
                    "note": _bounded(_visible(note or ""), SUMMARY_FIELD_LIMIT),
                }
            )
            in_share_table = False
            continue
        if _SMBMAP_SHARE_HEADER_RE.match(stripped):
            in_share_table = True
            continue
        if in_share_table:
            columns = stripped.split()
            if len(columns) >= 2 and not set(columns[0]) <= {"-", " "}:
                shares.append(
                    {
                        "name": columns[0],
                        "type": columns[1],
                        "comment": _bounded(_visible(" ".join(columns[2:])), SUMMARY_FIELD_LIMIT),
                    }
                )
            continue
        path_match = _SMBMAP_PATH_RE.match(stripped)
        if path_match is not None:
            share_host, share, rest = path_match.groups()
            perms = _SMBMAP_PERMS_RE.findall(rest)
            cleaned = _SMBMAP_PERMS_RE.sub("", rest).rstrip()
            paths.append(
                {
                    "host": share_host,
                    "share": share,
                    "path": _bounded(_visible(cleaned), SUMMARY_FIELD_LIMIT),
                    "permissions": perms,
                    "is_directory": cleaned.endswith("\\") or cleaned == "",
                }
            )
            continue
    if hosts or shares or paths or errors:
        return {
            "host_count": len(hosts),
            "hosts": hosts,
            "share_count": len(shares),
            "shares": shares,
            "path_count": len(paths),
            "paths": paths,
            "errors": errors,
        }, None
    return _text_fallback_data(text), None


def _summarize_smbmap(data: Mapping[str, object]) -> str:
    """One compact summary line for an smbmap run."""
    bits = ["smbmap"]
    share_count = data.get("share_count")
    host_count = data.get("host_count")
    if isinstance(share_count, int):
        bits.append(f"{share_count} share(s)")
    if isinstance(host_count, int):
        bits.append(f"{host_count} host(s)")
    hosts = data.get("hosts")
    if isinstance(hosts, list) and hosts:
        first = hosts[0]
        if isinstance(first, Mapping) and isinstance(first.get("name"), str) and first["name"]:
            bits.append(f"host: {_quote_field(first['name'])}")
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        bits.append(f"{len(errors)} error(s)")
    return "; ".join(bits)


class SmbmapTextParser(Parser):
    """Normalize smbmap text output into typed share/host data (V04).

    Extracts host banners, share tables (name/type/comment), shared
    paths with permissions, and ``[!]`` error lines. Unrecognized
    output degrades to labeled line data; ``malformed`` stays False —
    smbmap output is text and always parseable as data.
    """

    source: ClassVar[str] = "smbmap"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one smbmap text output into typed share fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_smbmap(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_smbmap(data),
        )


# ---------------------------------------------------------------------------
# ldapsearch (LDIF, -LLL)
# ---------------------------------------------------------------------------


def _parse_ldif(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize ldapsearch LDIF output into typed directory entries.

    Records are blank-line separated; each carries a ``dn:`` line plus
    attribute lines (``attr: value`` or base64 ``attr:: value`` with
    single-space continuation lines). Comment lines and the trailing
    ``search result`` pseudo-record are skipped. Non-LDIF output (e.g.
    a bind error banner) is a structured malformed observation.
    """
    stripped = text.strip()
    if not stripped:
        return {}, "empty output: expected LDIF"
    if "dn:" not in text:
        return {}, "no dn: records found (expected LDIF search output)"
    entries: list[dict[str, object]] = []
    current_dn: str | None = None
    current_attributes: dict[str, list[str]] = {}
    last_attribute = ""
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            if current_dn is not None:
                if current_dn:
                    entries.append(_ldif_entry(current_dn, current_attributes))
                current_dn = None
                current_attributes = {}
                last_attribute = ""
            continue
        if line.startswith(" ") and last_attribute and current_dn is not None:
            values = current_attributes[last_attribute]
            values[-1] = f"{values[-1]} {line.strip()}"
            continue
        name, separator, raw_value = line.partition(":")
        if not separator:
            continue
        if name == "dn":
            current_dn = raw_value.strip()
            current_attributes = {}
            last_attribute = ""
            continue
        if name == "version":
            continue
        if current_dn is None:
            continue
        if raw_value.startswith(":"):
            try:
                value = base64.b64decode(raw_value[1:].strip()).decode("utf-8", errors="replace")
            except ValueError:
                value = raw_value[1:].strip()
        else:
            value = raw_value.lstrip()
        current_attributes.setdefault(name, []).append(_bounded(_visible(value), 500))
        last_attribute = name
    if current_dn is not None and current_dn:
        entries.append(_ldif_entry(current_dn, current_attributes))
    if not entries:
        return {}, "no LDIF entries with a non-empty dn"
    return {
        "entry_count": len(entries),
        "entries": entries,
    }, None


def _ldif_entry(dn: str, attributes: Mapping[str, list[str]]) -> dict[str, object]:
    """One typed LDIF entry: dn plus its attribute name -> values map."""
    return {
        "dn": _bounded(_visible(dn), 500),
        "attribute_count": len(attributes),
        "attributes": dict(attributes),
    }


def _summarize_ldif(data: Mapping[str, object]) -> str:
    """One compact summary line for an ldapsearch result."""
    entry_count = data.get("entry_count")
    bits = ["ldapsearch"]
    if isinstance(entry_count, int):
        bits.append(f"{entry_count} entr{'y' if entry_count == 1 else 'ies'}")
    entries = data.get("entries")
    if isinstance(entries, list) and entries:
        first = entries[0]
        if isinstance(first, Mapping) and isinstance(first.get("dn"), str) and first["dn"]:
            bits.append(f"first: {_quote_field(first['dn'])}")
    return "; ".join(bits)


class LdapsearchLdifParser(Parser):
    """Normalize ldapsearch LDIF output (``-LLL``) into typed entries.

    Each record becomes a typed entry (dn + attribute -> values map),
    with base64 values decoded and continuation lines rejoined.
    Non-LDIF output becomes ``malformed=True`` with a bounded excerpt.
    """

    source: ClassVar[str] = "ldapsearch"
    kind: ClassVar[str] = "ldif"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one ldapsearch LDIF output into typed entry fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_ldif(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_ldif(data),
        )


# ---------------------------------------------------------------------------
# semgrep (JSON, --json)
# ---------------------------------------------------------------------------


def _semgrep_result(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-result fields from one semgrep JSON result."""
    start = item.get("start")
    extra = item.get("extra")
    start_map = start if isinstance(start, Mapping) else {}
    extra_map = extra if isinstance(extra, Mapping) else {}
    return {
        "check_id": _str_field(item, "check_id"),
        "path": _str_field(item, "path"),
        "line": _as_int(start_map.get("line")),
        "message": _bounded(_visible(_str_field(extra_map, "message")), 200),
        "severity": _str_field(extra_map, "severity"),
        "snippet": _bounded(_visible(_str_field(extra_map, "lines")), 200),
    }


def _parse_semgrep_json(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize semgrep JSON output (``--json``) into typed findings."""
    payload, parse_error = _parse_single_json_document(text)
    if parse_error is not None or payload is None:
        return {}, parse_error or "unparseable semgrep output"
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return {}, "semgrep document must carry a results array"
    results = [entry for entry in raw_results if isinstance(entry, Mapping)]
    severities = [str(result["severity"]) for result in (_semgrep_result(e) for e in results)]
    errors = payload.get("errors")
    return {
        "result_count": len(results),
        "results": [_semgrep_result(entry) for entry in results],
        "severity_counts": _count_by(severities),
        "error_count": len(errors) if isinstance(errors, list) else 0,
        "version": _str_field(payload, "version"),
    }, None


def _summarize_semgrep(data: Mapping[str, object]) -> str:
    """One compact summary line for a semgrep run."""
    result_count = data.get("result_count")
    bits = ["semgrep findings"]
    if isinstance(result_count, int):
        bits.append(f"{result_count} result(s)")
    severity_counts = data.get("severity_counts")
    if isinstance(severity_counts, Mapping) and severity_counts:
        bits.append(f"severities: {_render_counts(severity_counts)}")
    return "; ".join(bits)


class SemgrepJsonParser(Parser):
    """Normalize semgrep JSON output (``--json``) into typed findings.

    Extracts each result's typed fields (check id, path, line, message,
    severity, code snippet) plus a severity histogram. Broken JSON or a
    missing results array becomes ``malformed=True``.
    """

    source: ClassVar[str] = "semgrep"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one semgrep JSON document into typed finding fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_semgrep_json(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_semgrep(data),
        )


# ---------------------------------------------------------------------------
# SARIF 2.1.0 (semgrep --sarif, CodeQL database analyze --format=sarif-*)
# ---------------------------------------------------------------------------


def _parse_sarif(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize a SARIF 2.1.0 document into typed tool/result data.

    Extracts the tool driver (name/version), its rules, and each
    result's typed fields (rule id, level, message, file URI, start and
    end lines) plus a level histogram. Shared by the semgrep and CodeQL
    SARIF parsers (the document shapes are identical).
    """
    payload, parse_error = _parse_single_json_document(text)
    if parse_error is not None or payload is None:
        return {}, parse_error or "unparseable SARIF document"
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        return {}, "SARIF document must carry a non-empty runs array"
    run = runs[0]
    if not isinstance(run, Mapping):
        return {}, "SARIF run must be an object"
    tool_raw = run.get("tool")
    driver = tool_raw.get("driver") if isinstance(tool_raw, Mapping) else None
    driver = driver if isinstance(driver, Mapping) else {}
    tool_name = _str_field(driver, "name") or "sarif"
    rules: dict[str, dict[str, object]] = {}
    rules_raw = driver.get("rules")
    if isinstance(rules_raw, list):
        for rule in rules_raw:
            if not isinstance(rule, Mapping):
                continue
            rule_id = _str_field(rule, "id")
            if not rule_id:
                continue
            short = rule.get("shortDescription")
            properties = rule.get("properties")
            rules[rule_id] = {
                "id": rule_id,
                "name": _str_field(rule, "name") or rule_id,
                "description": _bounded(
                    _visible(_str_field(short, "text") if isinstance(short, Mapping) else ""), 200
                ),
                "severity": (
                    _str_field(properties, "severity") if isinstance(properties, Mapping) else ""
                ),
                "precision": (
                    _str_field(properties, "precision") if isinstance(properties, Mapping) else ""
                ),
            }
    results: list[dict[str, object]] = []
    levels: list[str] = []
    results_raw = run.get("results")
    if isinstance(results_raw, list):
        for item in results_raw:
            if not isinstance(item, Mapping):
                continue
            results.append(_sarif_result(item))
            level = _str_field(item, "level")
            if level:
                levels.append(level)
    return {
        "tool": tool_name,
        "version": _str_field(driver, "version"),
        "rule_count": len(rules),
        "rules": rules,
        "result_count": len(results),
        "results": results,
        "level_counts": _count_by(levels),
    }, None


def _sarif_result(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-result fields from one SARIF result object."""
    message = item.get("message")
    locations = item.get("locations")
    uri = ""
    start_line: int | None = None
    end_line: int | None = None
    if isinstance(locations, list) and locations and isinstance(locations[0], Mapping):
        physical = locations[0].get("physicalLocation")
        if isinstance(physical, Mapping):
            artifact = physical.get("artifactLocation")
            if isinstance(artifact, Mapping):
                uri = _str_field(artifact, "uri")
            region = physical.get("region")
            if isinstance(region, Mapping):
                start_line = _as_int(region.get("startLine"))
                end_line = _as_int(region.get("endLine"))
    return {
        "rule_id": _str_field(item, "ruleId"),
        "level": _str_field(item, "level"),
        "message": _bounded(
            _visible(_str_field(message, "text") if isinstance(message, Mapping) else ""), 200
        ),
        "uri": uri,
        "start_line": start_line,
        "end_line": end_line,
    }


def _summarize_sarif(data: Mapping[str, object]) -> str:
    """One compact summary line for a SARIF document."""
    tool = data.get("tool")
    result_count = data.get("result_count")
    bits = [f"{tool if isinstance(tool, str) and tool else 'sarif'} sarif"]
    if isinstance(result_count, int):
        bits.append(f"{result_count} result(s)")
    level_counts = data.get("level_counts")
    if isinstance(level_counts, Mapping) and level_counts:
        bits.append(f"levels: {_render_counts(level_counts)}")
    return "; ".join(bits)


class SemgrepSarifParser(Parser):
    """Normalize semgrep SARIF output (``--sarif``).

    The shared SARIF normalizer (:func:`_parse_sarif`) with the
    ``semgrep`` source: tool driver, rules, and typed results with
    file/line locations.
    """

    source: ClassVar[str] = "semgrep"
    kind: ClassVar[str] = "sarif"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one semgrep SARIF document into typed result fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_sarif(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_sarif(data),
        )


class CodeqlSarifParser(Parser):
    """Normalize CodeQL SARIF output (``database analyze --format=sarif-*``).

    The shared SARIF normalizer (:func:`_parse_sarif`) with the
    ``codeql`` source: tool driver, rules, and typed results with
    file/line locations.
    """

    source: ClassVar[str] = "codeql"
    kind: ClassVar[str] = "sarif"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one CodeQL SARIF document into typed result fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_sarif(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_sarif(data),
        )


# ---------------------------------------------------------------------------
# trivy (JSON, --format json)
# ---------------------------------------------------------------------------


def _trivy_vulnerability(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-vulnerability fields from one trivy finding."""
    return {
        "id": _str_field(item, "VulnerabilityID"),
        "package": _str_field(item, "PkgName"),
        "installed": _str_field(item, "InstalledVersion"),
        "fixed": _str_field(item, "FixedVersion"),
        "severity": _str_field(item, "Severity"),
        "title": _bounded(_visible(_str_field(item, "Title")), 160),
        "cvss_score": _as_float(item.get("CVSS") and _max_cvss(item.get("CVSS"))),
    }


def _max_cvss(value: object) -> object:
    """The highest CVSS base score across a trivy CVSS vector map."""
    if not isinstance(value, Mapping):
        return None
    best: float | None = None
    for vector in value.values():
        if not isinstance(vector, Mapping):
            continue
        score = _as_float(vector.get("V3Score"))
        if score is not None and (best is None or score > best):
            best = score
    return best


def _parse_trivy(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize trivy JSON output (``--format json``) into typed vulns."""
    payload, parse_error = _parse_single_json_document(text)
    if parse_error is not None or payload is None:
        return {}, parse_error or "unparseable trivy output"
    raw_results = payload.get("Results")
    if raw_results is None:
        raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return {}, "trivy document must carry a Results array"
    targets: list[dict[str, object]] = []
    vulnerabilities: list[dict[str, object]] = []
    severities: list[str] = []
    for target in raw_results:
        if not isinstance(target, Mapping):
            continue
        raw_vulns = target.get("Vulnerabilities")
        if raw_vulns is None:
            raw_vulns = target.get("vulnerabilities")
        target_vulns: list[dict[str, object]] = []
        if isinstance(raw_vulns, list):
            for item in raw_vulns:
                if not isinstance(item, Mapping):
                    continue
                vuln = _trivy_vulnerability(item)
                target_vulns.append(vuln)
                vulnerabilities.append(vuln)
                if vuln["severity"]:
                    severities.append(str(vuln["severity"]))
        targets.append(
            {
                "target": _str_field(target, "Target"),
                "class": _str_field(target, "Class"),
                "type": _str_field(target, "Type"),
                "vulnerability_count": len(target_vulns),
            }
        )
    return {
        "artifact": _str_field(payload, "ArtifactName") or _str_field(payload, "artifact_name"),
        "artifact_type": _str_field(payload, "ArtifactType")
        or _str_field(payload, "artifact_type"),
        "target_count": len(targets),
        "targets": targets,
        "vulnerability_count": len(vulnerabilities),
        "vulnerabilities": vulnerabilities,
        "severity_counts": _count_by(severities),
    }, None


def _summarize_trivy(data: Mapping[str, object]) -> str:
    """One compact summary line for a trivy scan."""
    artifact = data.get("artifact")
    vulnerability_count = data.get("vulnerability_count")
    bits = ["trivy scan"]
    if isinstance(artifact, str) and artifact:
        bits.append(f"of {_quote_field(artifact)}")
    if isinstance(vulnerability_count, int):
        bits.append(f"{vulnerability_count} vuln(s)")
    severity_counts = data.get("severity_counts")
    if isinstance(severity_counts, Mapping) and severity_counts:
        bits.append(f"severities: {_render_counts(severity_counts)}")
    return "; ".join(bits)


class TrivyJsonParser(Parser):
    """Normalize trivy JSON output (``--format json``) into typed vulns.

    Extracts the scanned artifact, per-target breakdowns, and typed
    vulnerabilities (CVE id, package, installed/fixed versions,
    severity, title, max CVSS score). Broken JSON or a missing Results
    array becomes ``malformed=True``.
    """

    source: ClassVar[str] = "trivy"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one trivy JSON document into typed vulnerability fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_trivy(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_trivy(data),
        )


# ---------------------------------------------------------------------------
# gitleaks (JSON, --report-format json)
# ---------------------------------------------------------------------------


def _gitleaks_finding(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-finding fields from one gitleaks report entry.

    The secret and full match are deliberately NOT extracted: the raw
    report is already persisted as an artifact, and the observation is
    a redacted location index (rule, file, line, commit) that never
    re-exposes the credential in model context or graph payload.
    """
    return {
        "rule": _str_field(item, "RuleID"),
        "description": _bounded(_visible(_str_field(item, "Description")), 160),
        "file": _str_field(item, "File"),
        "start_line": _as_int(item.get("StartLine")),
        "end_line": _as_int(item.get("EndLine")),
        "commit": _str_field(item, "Commit"),
        "author": _str_field(item, "Author"),
        "email": _str_field(item, "Email"),
        "date": _str_field(item, "Date"),
        "message": _bounded(_visible(_str_field(item, "Message")), 160),
        "entropy": _as_float(item.get("Entropy")),
        "fingerprint": _str_field(item, "Fingerprint"),
    }


def _parse_gitleaks(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize gitleaks JSON report output into typed findings.

    The report is a JSON array (``gitleaks detect`` with the default
    ``--report-format json``); anything else — including the plain
    ``N leaks found`` notice printed when a report path was used — is a
    structured malformed observation (fail loudly).
    """
    value, parse_error = _parse_json_value(text)
    if parse_error is not None:
        return {}, parse_error
    if not isinstance(value, list):
        return {}, f"expected a gitleaks report array, got {_type_name(value)}"
    findings = [entry for entry in value if isinstance(entry, Mapping)]
    return {
        "finding_count": len(findings),
        "findings": [_gitleaks_finding(entry) for entry in findings],
        "secrets_redacted": True,
    }, None


def _summarize_gitleaks(data: Mapping[str, object]) -> str:
    """One compact summary line for a gitleaks report."""
    finding_count = data.get("finding_count")
    bits = ["gitleaks report"]
    if isinstance(finding_count, int):
        bits.append(f"{finding_count} finding(s)")
    findings = data.get("findings")
    if isinstance(findings, list) and findings:
        first = findings[0]
        if isinstance(first, Mapping):
            rule = first.get("rule")
            file_path = first.get("file")
            line = first.get("start_line")
            if isinstance(rule, str) and rule:
                location = f"{file_path if isinstance(file_path, str) and file_path else '?'}"
                if isinstance(line, int):
                    location = f"{location}:{line}"
                bits.append(f"top: {_quote_field(rule)} at {_quote_field(location)}")
    return "; ".join(bits)


class GitleaksJsonParser(Parser):
    """Normalize gitleaks JSON report output into typed findings.

    Extracts each finding's redacted location fields (rule, file,
    lines, commit, author, entropy); the secret text itself stays in
    the raw artifact only (``secrets_redacted`` is set in the payload).
    Non-array output becomes ``malformed=True``.
    """

    source: ClassVar[str] = "gitleaks"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one gitleaks JSON report into typed finding fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_gitleaks(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_gitleaks(data),
        )


# ---------------------------------------------------------------------------
# file (text)
# ---------------------------------------------------------------------------


def _parse_file_output(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize file(1) output (``path: description`` lines) into entries.

    Each non-empty line that splits on ``: `` becomes a typed entry
    (path + description). Unrecognized output — version banners, error
    text — degrades to line-based labeled data; file(1) output is text
    and stays parseable.
    """
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        path, separator, description = line.partition(": ")
        if separator and path.strip() and description.strip():
            entries.append(
                {"path": path.strip(), "description": _bounded(_visible(description.strip()), 200)}
            )
    if entries:
        return {"file_count": len(entries), "files": entries}, None
    return _text_fallback_data(text), None


def _summarize_file(data: Mapping[str, object]) -> str:
    """One compact summary line for a file(1) run."""
    file_count = data.get("file_count")
    bits = ["file analysis"]
    if isinstance(file_count, int):
        bits.append(f"{file_count} file(s)")
    files = data.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, Mapping):
            path = first.get("path")
            description = first.get("description")
            if isinstance(path, str) and path:
                bits.append(f"first: {_quote_field(path)}")
                if isinstance(description, str) and description:
                    bits.append(_quote_field(description))
    return "; ".join(bits)


class FileTextParser(Parser):
    """Normalize file(1) output into typed path/description entries.

    Each ``path: description`` line becomes a typed entry. Unrecognized
    output degrades to labeled line data; ``malformed`` stays False —
    file(1) output is text and always parseable as data.
    """

    source: ClassVar[str] = "file"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one file(1) output into typed file entries."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_file_output(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_file(data),
        )


# ---------------------------------------------------------------------------
# readelf (text)
# ---------------------------------------------------------------------------

_READELF_KEY_RE = re.compile(r"^(Class|Data|Machine|Type|Entry point address):\s*(.+)$")
_READELF_SECTION_RE = re.compile(
    r"^\[\s*\d+\]\s+(\S*)\s+(\S+)\s+[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+"
    r"([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s*(.*)$"
)
_READELF_NEEDED_RE = re.compile(r"\(NEEDED\)\s+Shared library:\s*\[([^\]]+)\]")
_READELF_SYM_COUNT_RE = re.compile(r"Symbol table '([^']+)' contains (\d+) entries:")
_READELF_PHDR_RE = re.compile(
    r"^(\S+)\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+"
    r"(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+([A-Za-z]+)\s+(0x[0-9a-fA-F]+)$"
)


def _parse_readelf(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize readelf output into typed ELF metadata.

    Recognizes the ELF header fields (``-h``), section table (``-S``),
    needed libraries (``-d``), program headers (``-l``), and symbol
    counts (``-s``). Unrecognized output degrades to line-based labeled
    data — readelf output is text and stays parseable.
    """
    elf: dict[str, object] = {}
    sections: list[dict[str, object]] = []
    needed_libraries: list[str] = []
    program_headers: list[dict[str, object]] = []
    symbol_count = 0
    saw_any = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        key_match = _READELF_KEY_RE.match(stripped)
        if key_match is not None:
            key, value = key_match.groups()
            elf[key.lower().replace(" ", "_")] = _bounded(_visible(value), 120)
            saw_any = True
            continue
        section_match = _READELF_SECTION_RE.match(stripped)
        if section_match is not None:
            name, section_type, size, flags = section_match.groups()
            sections.append(
                {
                    "name": _bounded(_visible(name), SUMMARY_FIELD_LIMIT),
                    "type": _bounded(_visible(section_type), SUMMARY_FIELD_LIMIT),
                    "size": int(size, 16),
                    "flags": _bounded(
                        _visible(flags.split()[0] if flags else ""), SUMMARY_FIELD_LIMIT
                    ),
                }
            )
            saw_any = True
            continue
        needed_match = _READELF_NEEDED_RE.search(stripped)
        if needed_match is not None:
            needed_libraries.append(needed_match.group(1))
            saw_any = True
            continue
        symbol_match = _READELF_SYM_COUNT_RE.match(stripped)
        if symbol_match is not None:
            symbol_count += int(symbol_match.group(2))
            saw_any = True
            continue
        phdr_match = _READELF_PHDR_RE.match(stripped)
        if phdr_match is not None:
            phdr_type, file_size, mem_size, flags, align = phdr_match.groups()
            program_headers.append(
                {
                    "type": phdr_type,
                    "file_size": int(file_size, 16),
                    "mem_size": int(mem_size, 16),
                    "flags": flags,
                    "align": int(align, 16),
                }
            )
            saw_any = True
            continue
    if not saw_any:
        return _text_fallback_data(text), None
    return {
        "elf": elf,
        "section_count": len(sections),
        "sections": sections,
        "needed_libraries": needed_libraries,
        "symbol_count": symbol_count,
        "program_header_count": len(program_headers),
        "program_headers": program_headers,
    }, None


def _summarize_readelf(data: Mapping[str, object]) -> str:
    """One compact summary line for a readelf run."""
    bits = ["readelf"]
    elf = data.get("elf")
    if isinstance(elf, Mapping):
        machine = elf.get("machine")
        elf_class = elf.get("class")
        elf_type = elf.get("type")
        if isinstance(machine, str) and machine:
            bits.append(
                f"{elf_class if isinstance(elf_class, str) and elf_class else '?'} {machine}"
            )
        if isinstance(elf_type, str) and elf_type:
            bits.append(_quote_field(elf_type))
    section_count = data.get("section_count")
    if isinstance(section_count, int):
        bits.append(f"{section_count} section(s)")
    symbol_count = data.get("symbol_count")
    if isinstance(symbol_count, int):
        bits.append(f"{symbol_count} symbol(s)")
    libraries = data.get("needed_libraries")
    if isinstance(libraries, list) and libraries:
        bits.append(f"libs: {', '.join(str(lib) for lib in libraries[:5])}")
    return "; ".join(bits)


class ReadelfTextParser(Parser):
    """Normalize readelf output into typed ELF metadata (V04).

    Extracts the ELF header (class/data/machine/type/entry point),
    section table, needed shared libraries, program headers, and symbol
    counts into typed observation fields. Unrecognized output degrades
    to labeled line data; ``malformed`` stays False.
    """

    source: ClassVar[str] = "readelf"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one readelf output into typed ELF metadata."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_readelf(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_readelf(data),
        )


# ---------------------------------------------------------------------------
# checksec (text table / --file= blocks / --output=json)
# ---------------------------------------------------------------------------

#: checksec.sh table row (``RELRO STACK CANARY NX PIE ... FILE``).
_CHECKSEC_ROW_RE = re.compile(
    r"^\s*(Full RELRO|Partial RELRO|No RELRO)\s+"
    r"(Canary found|No canary found)\s+"
    r"(NX enabled|NX disabled)\s+"
    r"(PIE enabled|No PIE|PIE disabled)\s+"
    r"(No RPATH|RPATH)\s+"
    r"(No RUNPATH|RUNPATH)\s+"
    r"(\d+) Symbols\s+"
    r"(Yes|No)\s+"
    r"(\d+)\s+(\d+)\s+"
    r"(.+)$"
)

#: checksec v2 block format: ``[*] '/bin/ls'`` followed by indented
#: ``KEY: value`` lines.
_CHECKSEC_BLOCK_RE = re.compile(r"^\[\*\]\s+'([^']+)'$")
_CHECKSEC_PROPERTY_RE = re.compile(r"^\s{4}([A-Za-z ]+?):\s*(.+)$")


def _parse_checksec(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize checksec output into typed per-file hardening data.

    Handles the table format (``RELRO ... FILE`` rows), the v2
    ``[*] '/path'`` block format, and ``--output=json`` documents.
    Unrecognized output degrades to line-based labeled data.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        value, _ = _parse_json_value(stripped)
        if isinstance(value, Mapping):
            return _checksec_json_data(value), None
    files: list[dict[str, object]] = []
    for line in text.splitlines():
        row_match = _CHECKSEC_ROW_RE.match(line)
        if row_match is not None:
            (
                relro,
                canary,
                nx,
                pie,
                rpath,
                runpath,
                symbols,
                fortify,
                fortified,
                fortify_weakened,
                path,
            ) = row_match.groups()
            files.append(
                {
                    "path": path.strip(),
                    "relro": relro,
                    "canary": canary,
                    "nx": nx,
                    "pie": pie,
                    "rpath": rpath,
                    "runpath": runpath,
                    "symbols": _as_int(symbols),
                    "fortify": fortify == "Yes",
                    "fortified": _as_int(fortified),
                    "fortify_weakened": _as_int(fortify_weakened),
                }
            )
            continue
    if not files:
        files = _checksec_block_files(text)
    if not files:
        return _text_fallback_data(text), None
    return {"file_count": len(files), "files": files}, None


def _checksec_json_data(doc: Mapping[str, object]) -> dict[str, object]:
    """Typed per-file hardening fields from a checksec ``--output=json`` doc."""
    path = _str_field(doc, "file") or _str_field(doc, "path")
    arch = _str_field(doc, "arch")
    relro = _str_field(doc, "relro")
    canary = _str_field(doc, "canary")
    nx = _str_field(doc, "nx")
    pie = _str_field(doc, "pie")
    fortify = _str_field(doc, "fortify")
    return {
        "file_count": 1,
        "files": [
            {
                "path": path or "?",
                "arch": arch,
                "relro": relro,
                "canary": canary,
                "nx": nx,
                "pie": pie,
                "fortify": fortify,
                "fortified": _as_int(doc.get("fortified")),
                "fortify_weakened": _as_int(doc.get("fortify_weakened")),
            }
        ],
    }


def _checksec_block_files(text: str) -> list[dict[str, object]]:
    """Per-file hardening fields from checksec v2 ``[*] '/path'`` blocks."""
    files: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in text.splitlines():
        block_match = _CHECKSEC_BLOCK_RE.match(line)
        if block_match is not None:
            if current is not None:
                files.append(current)
            current = {"path": block_match.group(1)}
            continue
        property_match = _CHECKSEC_PROPERTY_RE.match(line)
        if property_match is not None and current is not None:
            key, value = property_match.groups()
            current[key.lower().replace(" ", "_")] = _bounded(_visible(value), SUMMARY_FIELD_LIMIT)
    if current is not None:
        files.append(current)
    return files


def _summarize_checksec(data: Mapping[str, object]) -> str:
    """One compact summary line for a checksec run."""
    file_count = data.get("file_count")
    bits = ["checksec"]
    if isinstance(file_count, int):
        bits.append(f"{file_count} file(s)")
    files = data.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, Mapping):
            path = first.get("path")
            if isinstance(path, str) and path:
                bits.append(f"e.g. {_quote_field(path)}")
            relro = first.get("relro")
            canary = first.get("canary")
            nx = first.get("nx")
            pie = first.get("pie")
            if isinstance(relro, str) and relro:
                bits.append(f"RELRO={relro}")
            if isinstance(canary, str) and canary:
                bits.append(f"canary={canary}")
            if isinstance(nx, str) and nx:
                bits.append(f"NX={nx}")
            if isinstance(pie, str) and pie:
                bits.append(f"PIE={pie}")
    return "; ".join(bits)


class ChecksecTextParser(Parser):
    """Normalize checksec output into typed per-file hardening data.

    Handles the table format, the ``[*] '/path'`` block format, and
    ``--output=json`` documents: RELRO, stack canary, NX, PIE, RPATH,
    RUNPATH, FORTIFY per binary. Unrecognized output degrades to
    labeled line data; ``malformed`` stays False.
    """

    source: ClassVar[str] = "checksec"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one checksec output into typed hardening fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_checksec(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_checksec(data),
        )


# ---------------------------------------------------------------------------
# exiftool (JSON, -json / text)
# ---------------------------------------------------------------------------

#: Cap on tags kept per file: exiftool dumps every tag, and the
#: observation payload must stay compact for model context.
_MAX_TAGS_PER_FILE = 64

#: Cap on a single tag value in characters.
_TAG_VALUE_LIMIT = 200


def _bounded_tag_value(value: object) -> str:
    """A tag value coerced to a bounded, control-escaped string."""
    if isinstance(value, (str, int, float, bool)):
        return _bounded(_visible(str(value)), _TAG_VALUE_LIMIT)
    if isinstance(value, list):
        return _bounded(_visible(", ".join(str(item) for item in value)), _TAG_VALUE_LIMIT)
    return ""


def _exiftool_file(item: Mapping[str, object]) -> dict[str, object]:
    """Typed per-file fields from one exiftool JSON record (tag-capped)."""
    tags: dict[str, str] = {}
    truncated = False
    for index, (name, value) in enumerate(item.items()):
        if index >= _MAX_TAGS_PER_FILE:
            truncated = True
            break
        if isinstance(name, str):
            tags[name] = _bounded_tag_value(value)
    return {
        "source_file": _str_field(item, "SourceFile"),
        "file_name": _str_field(item, "FileName"),
        "file_type": _str_field(item, "FileType"),
        "mime_type": _str_field(item, "MIMEType"),
        "file_size": _str_field(item, "FileSize"),
        "tag_count": len(tags),
        "tags_truncated": truncated,
        "tags": tags,
    }


def _parse_exiftool_json(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize exiftool JSON output (``-json``) into typed metadata."""
    value, parse_error = _parse_json_value(text)
    if parse_error is not None:
        return {}, parse_error
    if not isinstance(value, list):
        return {}, f"expected an exiftool file array, got {_type_name(value)}"
    files = [entry for entry in value if isinstance(entry, Mapping)]
    return {
        "file_count": len(files),
        "files": [_exiftool_file(entry) for entry in files],
    }, None


def _parse_exiftool_text(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize exiftool text output (``Tag: Value`` lines)."""
    tags: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(("======== ", "--------")):
            continue
        name, separator, value = line.partition(":")
        if separator and name.strip():
            tags[name.strip()] = _bounded(_visible(value.strip()), _TAG_VALUE_LIMIT)
            if len(tags) >= _MAX_TAGS_PER_FILE:
                break
    if not tags:
        return _text_fallback_data(text), None
    return {
        "file_count": 1,
        "files": [
            {
                "source_file": "",
                "file_name": "",
                "file_type": tags.get("File Type", ""),
                "mime_type": tags.get("MIME Type", ""),
                "file_size": tags.get("File Size", ""),
                "tag_count": len(tags),
                "tags_truncated": False,
                "tags": tags,
            }
        ],
    }, None


def _summarize_exiftool(data: Mapping[str, object]) -> str:
    """One compact summary line for an exiftool run."""
    file_count = data.get("file_count")
    bits = ["exiftool"]
    if isinstance(file_count, int):
        bits.append(f"{file_count} file(s)")
    files = data.get("files")
    if isinstance(files, list) and files:
        first = files[0]
        if isinstance(first, Mapping):
            name = first.get("file_name") or first.get("source_file")
            file_type = first.get("file_type")
            mime = first.get("mime_type")
            if isinstance(name, str) and name:
                bits.append(f"first: {_quote_field(name)}")
            if isinstance(file_type, str) and file_type:
                bits.append(f"({file_type})")
            if isinstance(mime, str) and mime:
                bits.append(f"{mime}")
    return "; ".join(bits)


class ExiftoolJsonParser(Parser):
    """Normalize exiftool JSON output (``-json``) into typed metadata.

    Each file record keeps its typed identity fields (name, type, MIME,
    size) plus a tag map capped at ``_MAX_TAGS_PER_FILE`` entries so the
    observation payload stays compact. Non-array output becomes
    ``malformed=True``.
    """

    source: ClassVar[str] = "exiftool"
    kind: ClassVar[str] = "json"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one exiftool JSON document into typed metadata fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_exiftool_json(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_exiftool(data),
        )


class ExiftoolTextParser(Parser):
    """Normalize exiftool text output into typed metadata (V04).

    Parses the ``Tag: Value`` lines into a tag map (capped), with
    FileType/MIMEType/FileSize promoted to typed identity fields.
    Unrecognized output degrades to labeled line data.
    """

    source: ClassVar[str] = "exiftool"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one exiftool text output into typed metadata fields."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_exiftool_text(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_exiftool(data),
        )


# ---------------------------------------------------------------------------
# binwalk (text)
# ---------------------------------------------------------------------------

#: binwalk table header row (``DECIMAL HEXADECIMAL DESCRIPTION``).
_BINWALK_HEADER_RE = re.compile(r"^DECIMAL\s+HEXADECIMAL\s+DESCRIPTION")

#: binwalk table row: decimal offset, hex offset, description.
_BINWALK_ROW_RE = re.compile(r"^\s*(\d+)\s+(0x[0-9a-fA-F]+)\s+(.*)$")


def _parse_binwalk(text: str) -> tuple[dict[str, object], str | None]:
    """Normalize binwalk output into typed offset/description entries.

    Parses the ``DECIMAL HEXADECIMAL DESCRIPTION`` table; unrecognized
    output (e.g. ``binwalk: cannot open ...`` errors) degrades to
    line-based labeled data — binwalk output is text and stays
    parseable.
    """
    lines = text.splitlines()
    saw_header = any(_BINWALK_HEADER_RE.match(line.strip()) for line in lines)
    if not saw_header:
        return _text_fallback_data(text), None
    entries: list[dict[str, object]] = []
    for line in lines:
        if _BINWALK_HEADER_RE.match(line.strip()) or not line.strip():
            continue
        if set(line.strip()) <= {"-", " "}:
            continue
        row_match = _BINWALK_ROW_RE.match(line)
        if row_match is not None:
            offset, hex_offset, description = row_match.groups()
            entries.append(
                {
                    "offset": int(offset),
                    "hex_offset": hex_offset,
                    "description": _bounded(_visible(description), 200),
                }
            )
    return {"entry_count": len(entries), "entries": entries}, None


def _summarize_binwalk(data: Mapping[str, object]) -> str:
    """One compact summary line for a binwalk run."""
    entry_count = data.get("entry_count")
    bits = ["binwalk"]
    if isinstance(entry_count, int):
        bits.append(f"{entry_count} entr{'y' if entry_count == 1 else 'ies'}")
    entries = data.get("entries")
    if isinstance(entries, list) and entries:
        first = entries[0]
        if isinstance(first, Mapping):
            hex_offset = first.get("hex_offset")
            description = first.get("description")
            if isinstance(hex_offset, str) and hex_offset:
                bits.append(f"first: {hex_offset}")
                if isinstance(description, str) and description:
                    bits.append(_quote_field(description))
    return "; ".join(bits)


class BinwalkTextParser(Parser):
    """Normalize binwalk output into typed offset/description entries.

    Each table row becomes a typed entry (decimal offset, hex offset,
    description). Unrecognized output degrades to labeled line data;
    ``malformed`` stays False.
    """

    source: ClassVar[str] = "binwalk"
    kind: ClassVar[str] = "text"

    def parse(self, raw: ToolResult | str) -> Observation:
        """Parse one binwalk output into typed file-scan entries."""
        _require_tool_result_or_str(raw)
        text = strip_ansi(raw.stdout if isinstance(raw, ToolResult) else raw)
        action_id, exit_code, truncated_streams, timeout = _tool_metadata(raw)
        data, parse_error = _parse_binwalk(text)
        return _semantic_observation(
            parser=self,
            text=text,
            action_id=action_id,
            exit_code=exit_code,
            truncated_streams=truncated_streams,
            timeout=timeout,
            data=data,
            parse_error=parse_error,
            summary=_summarize_binwalk(data),
        )


# ---------------------------------------------------------------------------
# Deterministic command -> parser dispatch (V04 raw-first flow)
# ---------------------------------------------------------------------------

#: Prefixes that do not change what a command is (mirrors the policy
#: gate's family-override set; the policy gate is authoritative for
#: execution, this set only picks the parser).
_WRAPPER_TOKENS: frozenset[str] = frozenset(
    {"sudo", "env", "nohup", "time", "nice", "command", "timeout", "stdbuf", "setsid", "taskset"}
)

#: Shell wrappers that may carry ``-c '<command>'``.
_SHELL_WRAPPER_NAMES: frozenset[str] = frozenset({"sh", "bash", "dash", "zsh", "ksh"})

#: Binary aliases -> canonical tool id.
_TOOL_ALIASES: dict[str, str] = {"nxc": "netexec", "crackmapexec": "netexec"}


def _command_tokens(command: str) -> list[str]:
    """Tokenize a command line defensively (quotes honored, no crash)."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _resolve_command(command: str) -> tuple[str, str]:
    """``(tool, unwrapped_command)`` for one command line.

    Walks wrapper prefixes (``sudo``/``env``/``timeout``/...), unwraps
    ``sh -c '...'`` / ``bash -c "..."`` shells, and maps binary aliases
    (``nxc`` -> netexec). The unwrapped command is the innermost command
    string, which is what flag-based format detection runs against.
    Unknown or empty commands yield ``("", "")`` (the caller falls back
    to the shell text parser).
    """
    tokens = _command_tokens(command)
    while tokens:
        head = tokens[0]
        if "=" in head:
            # ``env VAR=value ...`` style assignments precede the command.
            tokens = tokens[1:]
            continue
        if head in _WRAPPER_TOKENS:
            if head == "timeout" and len(tokens) >= 2 and tokens[1].isdigit():
                tokens = tokens[2:]  # timeout <seconds> <command>
                continue
            tokens = tokens[1:]
            continue
        if head in _SHELL_WRAPPER_NAMES and len(tokens) >= 2 and tokens[1] == "-c":
            return _resolve_command(" ".join(tokens[2:]))
        return _TOOL_ALIASES.get(head.casefold(), head.casefold()), " ".join(tokens)
    return "", ""


def _command_tool(command: str) -> str:
    """The canonical tool id a command invokes, or '' when unknown."""
    return _resolve_command(command)[0]


def _has_option_value(tokens: Sequence[str], flag: str, value: str) -> bool:
    """True when ``flag`` is followed by exactly ``value`` in ``tokens``."""
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            return tokens[index + 1].casefold() == value.casefold()
    return False


def _parser_key_for_tool(tool: str, command: str) -> tuple[str, str] | None:
    """The registered (source, kind) key for a command, or None.

    Flag-gated tools map to their semantic parser ONLY when the
    machine-readable output format was actually requested (nmap
    ``-oX``, nuclei ``-jsonl``/``-json``, semgrep ``--sarif``/``--json``,
    ...); otherwise None sends the observation through the generic
    shell text parser (their output is then plain text).
    """
    tokens = _command_tokens(command)
    if tool == "curl":
        return ("curl", "text")
    if tool == "nmap":
        return ("nmap", "xml") if any(token.startswith("-oX") for token in tokens) else None
    if tool == "ffuf":
        if "-json" in tokens or "--json" in tokens or _has_option_value(tokens, "-of", "json"):
            return ("ffuf", "json")
        return None
    if tool == "feroxbuster":
        if "--json" in tokens or any(
            token.endswith(".json") for token in tokens if not token.startswith("-")
        ):
            return ("feroxbuster", "json")
        return None
    if tool == "nuclei":
        if any(token.startswith("-json") for token in tokens) or "-je" in tokens:
            return ("nuclei", "jsonl")
        return None
    if tool == "netexec":
        return ("netexec", "jsonl") if "--json" in tokens else None
    if tool == "smbmap":
        return ("smbmap", "text")
    if tool == "ldapsearch":
        return ("ldapsearch", "ldif")
    if tool == "semgrep":
        if any(token.startswith("--sarif") for token in tokens):
            return ("semgrep", "sarif")
        if "--json" in tokens or "-json" in tokens:
            return ("semgrep", "json")
        return None
    if tool == "codeql":
        return ("codeql", "sarif") if any("sarif" in token for token in tokens) else None
    if tool == "trivy":
        if (
            "--json" in tokens
            or any(token.startswith("--format=json") for token in tokens)
            or _has_option_value(tokens, "--format", "json")
            or _has_option_value(tokens, "-f", "json")
        ):
            return ("trivy", "json")
        return None
    if tool == "gitleaks":
        # ``gitleaks version`` prints a banner, not a report.
        return None if "version" in tokens else ("gitleaks", "json")
    if tool == "file":
        return ("file", "text")
    if tool == "readelf":
        return ("readelf", "text")
    if tool == "checksec":
        return ("checksec", "text")
    if tool == "exiftool":
        return (
            ("exiftool", "json")
            if any(token in ("-json", "-j") for token in tokens)
            else (
                "exiftool",
                "text",
            )
        )
    if tool == "binwalk":
        return ("binwalk", "text")
    return None


def parser_for_command(command: str) -> Parser:
    """The deterministic parser for one shell command line.

    Resolves the command's tool (wrapper/shell/alias aware) against the
    registered semantic parsers and falls back to the generic
    :class:`ShellTextParser` when the tool is unknown or its output is
    not machine-readable. This is the dispatch the runner uses so that
    every observation is parsed by the parser matching the producing
    command — never guessed.
    """
    tool, unwrapped = _resolve_command(command)
    if tool:
        key = _parser_key_for_tool(tool, unwrapped)
        if key is not None:
            return get_parser(*key)
    return SHELL_TEXT_PARSER


def observation_for_result(result: ToolResult, artifact_id: str | None = None) -> Observation:
    """Parse one tool result with the parser for its command (V04).

    The raw-first flow: the caller persists ``result``'s raw output to
    the :class:`~ozzgraph.artifacts.ArtifactStore` FIRST, then calls
    this with the returned artifact id; the observation then references
    the artifact (``artifact_ids``), so raw output never depends on the
    parse succeeding. Parsers themselves never perform I/O.
    """
    observation = parser_for_command(result.command).parse(result)
    if artifact_id is not None:
        observation.artifact_ids = [artifact_id]
    return observation


# ---------------------------------------------------------------------------
# Deterministic registry: (source, kind) -> parser. Populated at import
# with the built-in parsers; extensible via :func:`register_parser`
# (explicit registration only — no discovery, AGENTS.md rule #10).
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

#: V04 semantic parsers (docs/OBSERVATIONS.md), one per high-value tool.
CURL_TEXT_PARSER = CurlTextParser()
NMAP_XML_PARSER = NmapXmlParser()
FFUF_JSON_PARSER = FfufJsonParser()
FEROXBUSTER_JSON_PARSER = FeroxbusterJsonParser()
NUCLEI_JSONL_PARSER = NucleiJsonlParser()
NETEXEC_JSONL_PARSER = NetexecJsonlParser()
SMBMAP_TEXT_PARSER = SmbmapTextParser()
LDAPSEARCH_LDIF_PARSER = LdapsearchLdifParser()
SEMGREP_JSON_PARSER = SemgrepJsonParser()
SEMGREP_SARIF_PARSER = SemgrepSarifParser()
CODEQL_SARIF_PARSER = CodeqlSarifParser()
TRIVY_JSON_PARSER = TrivyJsonParser()
GITLEAKS_JSON_PARSER = GitleaksJsonParser()
FILE_TEXT_PARSER = FileTextParser()
READELF_TEXT_PARSER = ReadelfTextParser()
CHECKSEC_TEXT_PARSER = ChecksecTextParser()
EXIFTOOL_JSON_PARSER = ExiftoolJsonParser()
EXIFTOOL_TEXT_PARSER = ExiftoolTextParser()
BINWALK_TEXT_PARSER = BinwalkTextParser()

register_parser(SHELL_TEXT_PARSER)
register_parser(HALCTL_JSON_PARSER)
register_parser(CURL_TEXT_PARSER)
register_parser(NMAP_XML_PARSER)
register_parser(FFUF_JSON_PARSER)
register_parser(FEROXBUSTER_JSON_PARSER)
register_parser(NUCLEI_JSONL_PARSER)
register_parser(NETEXEC_JSONL_PARSER)
register_parser(SMBMAP_TEXT_PARSER)
register_parser(LDAPSEARCH_LDIF_PARSER)
register_parser(SEMGREP_JSON_PARSER)
register_parser(SEMGREP_SARIF_PARSER)
register_parser(CODEQL_SARIF_PARSER)
register_parser(TRIVY_JSON_PARSER)
register_parser(GITLEAKS_JSON_PARSER)
register_parser(FILE_TEXT_PARSER)
register_parser(READELF_TEXT_PARSER)
register_parser(CHECKSEC_TEXT_PARSER)
register_parser(EXIFTOOL_JSON_PARSER)
register_parser(EXIFTOOL_TEXT_PARSER)
register_parser(BINWALK_TEXT_PARSER)
