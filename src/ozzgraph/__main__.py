"""Entry point — runs the OzzGraph supervisor kernel.

``python -m ozzgraph`` parses runtime configuration, starts the supervisor,
and exits with a code derived from the structured termination reason.

``ozzgraph run <target>`` (V02, docs/CHANGES_v2.md milestone 2) runs one
autonomous assessment of the given target end-to-end as a REAL process:
discover -> model -> tool -> parse -> graph -> hypothesis -> validate ->
Finding -> exit. The CLI seeds the target into the runtime configuration
(``OZZGRAPH_TARGET`` plus a target-derived allowlist when none is
configured), runs the supervisor, and maps the structured termination
reason to a process exit code (0 completed, 130 interrupted, 3 budget
exhausted, 1 failed).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse

from ozzgraph import __version__
from ozzgraph.bootstrap import TARGET_ENV
from ozzgraph.config import TARGET_ALLOWLIST_ENV, ConfigError, load_config
from ozzgraph.supervisor import Supervisor, TerminationReason

_EXIT_CODES: dict[TerminationReason, int] = {
    TerminationReason.COMPLETED: 0,
    TerminationReason.INTERRUPTED: 130,
    TerminationReason.FAILED: 1,
    TerminationReason.BUDGET_EXHAUSTED: 3,
}


def _build_parser() -> argparse.ArgumentParser:
    """The ``ozzgraph`` command-line interface (V02)."""
    parser = argparse.ArgumentParser(
        prog="ozzgraph",
        description=(
            "Autonomous security-research harness: scope -> assets -> "
            "observations -> evidence -> hypotheses -> validated findings."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="COMMAND")
    run_parser = subparsers.add_parser(
        "run",
        help="assess one target end-to-end",
        description=(
            "Run one autonomous assessment of TARGET end-to-end: discover -> "
            "model -> tool -> parse -> graph -> hypothesis -> validate -> "
            "Finding -> report -> exit. TARGET is classified into a local "
            "assessment mode: http(s) URL (url), CIDR (network), hostname/IP "
            "(host), a path to a git repository (repository), or a path to a "
            "Docker Compose project (docker-compose); mixed targets form a "
            "hybrid scope. The target becomes OZZGRAPH_TARGET; when no "
            "allowlist is configured it is derived from the target. With no "
            "HAL_* configuration the run uses the local assessment "
            "environment (the default experience); OZZGRAPH_SCOPE_FILE and "
            "OZZGRAPH_CREDENTIALS_FILE extend the allowlist and supply "
            "credential references. A completed run renders the report "
            "bundle (report.md / report.json / report.sarif / evidence/)."
        ),
    )
    run_parser.add_argument(
        "target",
        metavar="TARGET",
        help=(
            "the authorized target to assess: http(s) URL, hostname, IP, "
            "CIDR, git repository path, or Docker Compose project path"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the supervisor, and return the exit code.

    Every terminal path prints a human-readable ``TERMINATION: <reason>``
    summary as the final stdout line (AGENTS.md rule 9 — fail loudly with
    a structured termination event AND a human-readable summary); the
    structured event itself lives in the run's event log.

    ``--version`` prints the package version and exits 0 (checked before
    argument parsing so it never touches configuration). With no
    subcommand, the supervisor runs from the environment (the legacy
    ``python -m ozzgraph`` behavior).

    Args:
        argv: CLI arguments.

    Returns:
        Process exit code: 0 on clean completion, 1 on configuration or
        runtime failure, 130 on interruption, 3 on budget exhaustion.
    """
    args = sys.argv[1:] if argv is None else argv

    if "--version" in args:
        print(f"ozzgraph {__version__}")
        return 0

    namespace = _build_parser().parse_args(args)
    if namespace.subcommand == "run":
        return _run_target(namespace.target)
    return _run_supervisor()


def _run_target(target: str) -> int:
    """Run one end-to-end assessment of ``target`` and map the outcome.

    Seeding failures (an invalid target, an unsupported scheme, a target
    without a host) are configuration errors: printed to stderr and
    mapped to exit 1 (failed), never a silent partial run.
    """
    if target.strip() == "":
        print("ozzgraph: run requires a non-empty target", file=sys.stderr)
        return _EXIT_CODES[TerminationReason.FAILED]
    if any(character.isspace() for character in target):
        print(
            f"ozzgraph: invalid target {target!r}: whitespace is not allowed",
            file=sys.stderr,
        )
        return _EXIT_CODES[TerminationReason.FAILED]
    try:
        _seed_target(target)
    except ConfigError as exc:
        print(f"ozzgraph: configuration error: {exc}", file=sys.stderr)
        return _EXIT_CODES[TerminationReason.FAILED]
    return _run_supervisor()


def _seed_target(target: str) -> None:
    """Seed the target into the runtime configuration.

    Sets ``OZZGRAPH_TARGET`` to the target (the local environment's
    pointer). When no target allowlist is configured, it is derived from
    the target's host so the scope policy admits the assessment's own
    destination — fail-open only for the explicitly requested target
    (the allowlist remains the single source of truth for everything
    else).

    Raises:
        ConfigError: If the target has an unsupported URL scheme or no
            host.
    """
    os.environ[TARGET_ENV] = target
    if os.environ.get(TARGET_ALLOWLIST_ENV, "").strip():
        return
    if "://" in target:
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme.casefold() not in ("http", "https"):
            raise ConfigError(
                f"unsupported target scheme {parsed.scheme!r}; only http(s) URLs are supported"
            )
        host = parsed.hostname
        if not host:
            raise ConfigError(f"target {target!r} has no host")
        os.environ[TARGET_ALLOWLIST_ENV] = host
        return
    # A scheme-less target (hostname or bare IP): allowlist it as-is,
    # stripping a trailing :port so the policy's exact-host matching
    # still admits the address.
    bare = target
    if bare.count(":") == 1 and bare.rsplit(":", 1)[1].isdigit():
        bare = bare.rsplit(":", 1)[0]
    os.environ[TARGET_ALLOWLIST_ENV] = bare


def _run_supervisor() -> int:
    """Load configuration, run the supervisor, print the termination line.

    The legacy ``python -m ozzgraph`` path (V01): configuration comes
    entirely from the environment.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ozzgraph: configuration error: {exc}", file=sys.stderr)
        return _EXIT_CODES[TerminationReason.FAILED]

    supervisor = Supervisor(config)
    reason = asyncio.run(supervisor.run())
    # Human-readable termination summary (AGENTS.md rule 9); the structured
    # termination event is already appended to the run's event log.
    print(f"TERMINATION: {reason.value}", flush=True)
    return _EXIT_CODES[reason]


if __name__ == "__main__":
    sys.exit(main())
