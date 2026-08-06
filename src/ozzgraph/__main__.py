"""Entry point — runs the OzzGraph supervisor kernel.

``python -m ozzgraph`` parses runtime configuration, starts the supervisor,
and exits with a code derived from the structured termination reason.
"""

from __future__ import annotations

import sys

from ozzgraph import __version__
from ozzgraph.config import ConfigError, load_config
from ozzgraph.supervisor import Supervisor, TerminationReason

_EXIT_CODES: dict[TerminationReason, int] = {
    TerminationReason.COMPLETED: 0,
    TerminationReason.INTERRUPTED: 130,
    TerminationReason.FAILED: 1,
}


def main(argv: list[str] | None = None) -> int:
    """Parse config, run the supervisor, and return the exit code.

    Args:
        argv: CLI arguments. ``--version`` prints the package version and
            exits 0. All other arguments are ignored by the PR2 skeleton.

    Returns:
        Process exit code: 0 on clean completion, 1 on configuration or
        runtime failure, 130 on interruption.
    """
    args = sys.argv[1:] if argv is None else argv

    if "--version" in args:
        print(f"ozzgraph {__version__}")
        return 0

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ozzgraph: configuration error: {exc}", file=sys.stderr)
        return _EXIT_CODES[TerminationReason.FAILED]

    supervisor = Supervisor(config)
    reason = supervisor.run()
    return _EXIT_CODES[reason]


if __name__ == "__main__":
    sys.exit(main())
