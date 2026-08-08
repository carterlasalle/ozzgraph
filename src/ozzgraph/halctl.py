"""halctl — local terminal-native HalCTF MCP adapter CLI (PR6).

The model-facing surface for the HalCTF MCP integration
(docs/API_AND_INTEGRATIONS.md, "HalCTF Integration"). Models never call raw
MCP (AGENTS.md invariant 5); ``halctl`` is the only adapter. Every
subcommand prints exactly one JSON document to stdout and exits non-zero on
failure.

Subcommands:

- ``ctfs --json`` — list the available HalCTF competitions (V09)
- ``challenges [--ctf-id <id>] --json`` — list challenges (V09)
- ``challenge show --json`` — normalized challenge details
- ``status --json`` — challenge status
- ``submit --flag <flag> --json`` — submit a flag (supervisor-only)
- ``hint --index N --json`` — request a hint (paid hints supervisor-only)
- ``scoreboard --json`` — competition scoreboard
- ``exit --reason <reason> --json`` — graceful exit (supervisor-only)

Privileged operations fail with a normalized JSON error document and exit
code 1 unless ``OZZGRAPH_HAL_PRIVILEGED`` is set — the supervisor runs the
adapter with that variable; models run it without it.

Failure documents have the shape ``{"error": {"type": ..., ...}}`` and are
printed to stdout (the adapter's single-JSON-document contract), with a
non-zero exit code:

- exit 0 — success
- exit 1 — operational failure (``HalServiceError``, ``HalPrivilegeError``,
  configuration ``ValueError``)
- exit 2 — usage failure (missing challenge id)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from ozzgraph.hal_client import HalClient, HalPrivilegeError, HalServiceError

CHALLENGE_ID_ENV = "OZZGRAPH_CHALLENGE_ID"

# Subcommands that need a challenge id to operate on.
_CHALLENGE_COMMANDS = frozenset({"challenge", "status", "submit", "hint"})


def build_parser() -> argparse.ArgumentParser:
    """Build the ``halctl`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="halctl",
        description="Local terminal-native HalCTF MCP adapter.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Options shared by the challenge-scoped subcommands.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--challenge-id",
        default=None,
        help=f"Challenge id (env {CHALLENGE_ID_ENV} fallback).",
    )
    common.add_argument("--json", action="store_true", help="Emit one JSON document (always on).")

    challenge = subparsers.add_parser("challenge", parents=[common], help="Challenge operations.")
    challenge_sub = challenge.add_subparsers(dest="challenge_command", required=True)
    challenge_sub.add_parser("show", parents=[common], help="Show normalized challenge details.")

    subparsers.add_parser("ctfs", help="List the available competitions (V09).")

    challenges = subparsers.add_parser("challenges", help="List challenges (V09).")
    challenges.add_argument("--ctf-id", default=None, help="Narrow the listing to one competition.")
    challenges.add_argument("--json", action="store_true", help="Emit JSON (always on).")

    subparsers.add_parser("status", parents=[common], help="Challenge status.")

    submit = subparsers.add_parser("submit", parents=[common], help="Submit a flag.")
    submit.add_argument("--flag", required=True, help="Flag to submit.")

    hint = subparsers.add_parser("hint", parents=[common], help="Request a hint.")
    hint.add_argument("--index", required=True, type=int, help="Hint index.")

    scoreboard = subparsers.add_parser("scoreboard", help="Competition scoreboard.")
    scoreboard.add_argument("--json", action="store_true", help="Emit JSON (always on).")

    exit_parser = subparsers.add_parser("exit", help="Gracefully end the run.")
    exit_parser.add_argument("--reason", required=True, help="Termination reason.")
    exit_parser.add_argument("--json", action="store_true", help="Emit JSON (always on).")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``halctl`` console entry point.

    Returns:
        Process exit code (0 success, 1 operational failure, 2 usage).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run_command(args))


async def _run_command(args: argparse.Namespace) -> int:
    """Dispatch one parsed command against a fresh env-configured client."""
    challenge_id = _resolve_challenge_id(args)
    if challenge_id is None and args.command in _CHALLENGE_COMMANDS:
        _emit(
            {
                "error": {
                    "type": "usage",
                    "message": (
                        f"challenge id required: pass --challenge-id or set {CHALLENGE_ID_ENV}"
                    ),
                }
            }
        )
        return 2
    try:
        client = HalClient()
        async with client:
            if args.command == "ctfs":
                ctfs_result = await client.list_ctfs()
                _emit(ctfs_result.model_dump(mode="json"))
            elif args.command == "challenges":
                ctf_id = _arg(args, "ctf_id")
                challenges_result = await client.list_challenges(
                    ctf_id if isinstance(ctf_id, str) else None
                )
                _emit(challenges_result.model_dump(mode="json"))
            elif args.command == "challenge":
                assert challenge_id is not None
                challenge_result = await client.get_challenge(challenge_id)
                _emit(challenge_result.model_dump(mode="json"))
            elif args.command == "status":
                assert challenge_id is not None
                status_result = await client.get_status(challenge_id)
                _emit(status_result.model_dump(mode="json"))
            elif args.command == "submit":
                assert challenge_id is not None
                submission_result = await client.submit_flag(challenge_id, _arg(args, "flag"))
                _emit(submission_result.model_dump(mode="json"))
            elif args.command == "hint":
                assert challenge_id is not None
                hint_result = await client.request_hint(challenge_id, _arg(args, "index"))
                _emit(hint_result.model_dump(mode="json"))
            elif args.command == "scoreboard":
                scoreboard_result = await client.get_scoreboard()
                _emit(scoreboard_result.model_dump(mode="json"))
            elif args.command == "exit":
                reason = _arg(args, "reason")
                await client.graceful_exit(reason)
                _emit({"exited": True, "reason": reason})
            else:  # pragma: no cover - unreachable with a valid parser
                return _fail("usage", f"unknown command {args.command!r}")
        return 0
    except HalServiceError as exc:
        return _fail(
            "HalServiceError",
            exc.message,
            provider=exc.provider,
            status_code=exc.status_code,
            retryable=exc.retryable,
        )
    except HalPrivilegeError as exc:
        return _fail("HalPrivilegeError", str(exc))
    except ValueError as exc:
        return _fail("ValueError", str(exc))


def _resolve_challenge_id(args: argparse.Namespace) -> str | None:
    """Challenge id from ``--challenge-id``, falling back to the env var."""
    explicit = _arg(args, "challenge_id")
    if isinstance(explicit, str) and explicit != "":
        return explicit
    value = _env_str(os.environ, CHALLENGE_ID_ENV, "")
    return value if value != "" else None


def _arg(args: argparse.Namespace, name: str) -> Any:
    """Read an argparse attribute that may not exist on every subcommand."""
    return getattr(args, name, None)


def _env_str(environ: Mapping[str, str], key: str, default: str) -> str:
    """Read ``key`` from ``environ``, ignoring blank values."""
    value = environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _fail(error_type: str, message: str, **extra: object) -> int:
    """Emit a normalized JSON error document and return exit code 1."""
    error: dict[str, object] = {"type": error_type, "message": message}
    error.update(extra)
    _emit({"error": error})
    return 1


def _emit(doc: object) -> None:
    """Print one JSON document to stdout (deterministic key order)."""
    print(json.dumps(doc, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
