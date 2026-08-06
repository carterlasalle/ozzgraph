"""Bootstrap smoke tests — placeholder suite until Phase 0 tests land."""

import ozzgraph


def test_package_imports() -> None:
    assert ozzgraph.__version__ == "0.1.0"
