"""Minimal entry point — satisfies Phase 0 "empty application launches"."""

import sys

from ozzgraph import __version__


def main(argv: list[str] | None = None) -> int:
    """Print the version and exit cleanly."""
    _ = argv
    print(f"ozzgraph {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
