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

``ozzgraph benchmark`` (V10, docs/CHANGES_v2.md milestone 10,
docs/BENCHMARKS.md) runs the full-regression benchmark suite against the
synthetic lab: every target through the full harness — and, with
``--react``, through the plain-ReAct baseline — under a deterministic
scripted model by default (hermetic, zero network), or against a real
model endpoint when ``OZZGRAPH_BENCHMARK_MODEL_ID`` /
``OZZGRAPH_BENCHMARK_MODEL_BASE_URL`` are configured. The deterministic
markdown report goes to stdout or ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

from ozzgraph import __version__
from ozzgraph.benchmarks import (
    BENCHMARK_TARGETS,
    BenchmarkError,
    BenchmarkReport,
    BenchmarkResult,
    assemble_report,
    render_markdown,
    run_all_benchmarks,
    run_ozzgraph_benchmark,
    run_react_benchmark,
)
from ozzgraph.benchmarks.react import ServiceCallable
from ozzgraph.bootstrap import TARGET_ENV
from ozzgraph.config import TARGET_ALLOWLIST_ENV, ConfigError, load_config
from ozzgraph.lab import LabError, get_target
from ozzgraph.model_client import ModelService
from ozzgraph.supervisor import Supervisor, TerminationReason

_EXIT_CODES: dict[TerminationReason, int] = {
    TerminationReason.COMPLETED: 0,
    TerminationReason.INTERRUPTED: 130,
    TerminationReason.FAILED: 1,
    TerminationReason.BUDGET_EXHAUSTED: 3,
}

#: Environment variables selecting the REAL-model benchmark mode (V10,
#: docs/BENCHMARKS.md "Real-model runs"). When
#: ``OZZGRAPH_BENCHMARK_MODEL_ID`` is set, the benchmark evaluates a
#: live OpenAI-compatible endpoint instead of the deterministic
#: scripted model.
BENCHMARK_MODEL_ID_ENV = "OZZGRAPH_BENCHMARK_MODEL_ID"
BENCHMARK_MODEL_BASE_URL_ENV = "OZZGRAPH_BENCHMARK_MODEL_BASE_URL"
BENCHMARK_MODEL_API_KEY_ENV = "OZZGRAPH_BENCHMARK_MODEL_API_KEY"


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
    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="run the V10 full-regression benchmark suite against the synthetic lab",
        description=(
            "Run the full-regression benchmark suite (docs/BENCHMARKS.md): every "
            "selected lab target through the full OzzGraph harness — and, with "
            "--react, through the deterministic plain-ReAct baseline — under a "
            "deterministic scripted model (hermetic, zero network cost, reproducible "
            "in CI). With OZZGRAPH_BENCHMARK_MODEL_ID and "
            "OZZGRAPH_BENCHMARK_MODEL_BASE_URL set, a live OpenAI-compatible "
            "endpoint is evaluated instead. The deterministic markdown report is "
            "printed to stdout (or written to --out)."
        ),
    )
    benchmark_parser.add_argument(
        "--target",
        metavar="NAME",
        help=("a single benchmark target (docs/BENCHMARKS.md matrix); defaults to the full matrix"),
    )
    benchmark_parser.add_argument(
        "--all",
        action="store_true",
        help="run the full benchmark matrix (the default; explicit for clarity)",
    )
    benchmark_parser.add_argument(
        "--react",
        action="store_true",
        help="also run the plain-ReAct baseline and include the comparison",
    )
    benchmark_parser.add_argument(
        "--max-turns",
        type=int,
        default=12,
        metavar="N",
        help="per-run turn cap (default: 12)",
    )
    benchmark_parser.add_argument(
        "--out",
        metavar="FILE",
        help="write the markdown report to FILE instead of stdout",
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
    if namespace.subcommand == "benchmark":
        return _run_benchmark(namespace)
    return _run_supervisor()


def _run_benchmark(args: argparse.Namespace) -> int:
    """Run the V10 full-regression benchmark suite and render the report.

    The default mode is hermetic: a deterministic scripted model (zero
    network cost, reproducible in CI). When
    ``OZZGRAPH_BENCHMARK_MODEL_ID`` (plus ``OZZGRAPH_BENCHMARK_MODEL_BASE_URL``
    and optionally ``OZZGRAPH_BENCHMARK_MODEL_API_KEY``) is configured,
    a live OpenAI-compatible endpoint is evaluated instead
    (docs/BENCHMARKS.md, "Real-model runs"). Exit 0 on success, 1 on a
    configuration or benchmark error (fail loudly, AGENTS.md rule #9).
    """
    if args.max_turns < 1:
        print("ozzgraph: benchmark --max-turns must be >= 1", file=sys.stderr)
        return _EXIT_CODES[TerminationReason.FAILED]
    if args.target and not args.target.strip():
        print("ozzgraph: benchmark --target must be a non-empty name", file=sys.stderr)
        return _EXIT_CODES[TerminationReason.FAILED]

    model_id = os.environ.get(BENCHMARK_MODEL_ID_ENV, "").strip()
    targets = (args.target.strip(),) if args.target else BENCHMARK_TARGETS

    async def _main() -> int:
        try:
            if model_id:
                report = await _benchmark_real_model(
                    targets,
                    model_id=model_id,
                    base_url=os.environ.get(BENCHMARK_MODEL_BASE_URL_ENV, "").strip(),
                    api_key=os.environ.get(BENCHMARK_MODEL_API_KEY_ENV, "").strip() or None,
                    max_turns=args.max_turns,
                    include_react=args.react,
                )
            else:
                with TemporaryDirectory(prefix="ozzgraph-benchmark-") as temporary:
                    report = await run_all_benchmarks(
                        working_directory=Path(temporary),
                        targets=targets,
                        max_turns=args.max_turns,
                        include_react=args.react,
                    )
        except (BenchmarkError, LabError) as exc:
            print(f"ozzgraph: benchmark error: {exc}", file=sys.stderr)
            return _EXIT_CODES[TerminationReason.FAILED]

        document = render_markdown(report)
        if args.out:
            Path(args.out).write_text(document + "\n", encoding="utf-8")
            print(f"benchmark report written to {args.out}", flush=True)
        else:
            print(document, flush=True)
        return 0

    return asyncio.run(_main())


async def _benchmark_real_model(
    targets: tuple[str, ...],
    *,
    model_id: str,
    base_url: str,
    api_key: str | None,
    max_turns: int,
    include_react: bool,
) -> BenchmarkReport:
    """Evaluate a live model endpoint against the benchmark matrix.

    One :class:`~ozzgraph.model_client.ModelService` (the endpoint
    configured via the ``OZZGRAPH_BENCHMARK_*`` variables) runs every
    target through the full harness and, when requested, through the
    plain-ReAct baseline (via the callable adapter). The model decides
    its own actions from the prompts — no solve script, no scripted
    model (docs/BENCHMARKS.md, "Real-model runs").
    """
    service = ModelService(
        base_url=base_url or None,
        api_key=api_key,
        event_log=None,
        run_id="benchmark",
    )
    results: list[BenchmarkResult] = []
    try:
        with TemporaryDirectory(prefix="ozzgraph-benchmark-") as temporary:
            working_directory = Path(temporary)
            for target_name in targets:
                target = get_target(target_name)
                target.start()
                try:
                    results.append(
                        await run_ozzgraph_benchmark(
                            target_name,
                            service,
                            working_directory=working_directory,
                            max_turns=max_turns,
                            target=target,
                        )
                    )
                    if include_react:
                        results.append(
                            await run_react_benchmark(
                                target_name,
                                ServiceCallable(service, model_id=model_id),
                                working_directory=working_directory,
                                max_turns=max_turns,
                                target=target,
                            )
                        )
                finally:
                    target.stop()
    finally:
        await service.aclose()
    return assemble_report(results, targets=targets, max_turns=max_turns, model_id=model_id)


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
