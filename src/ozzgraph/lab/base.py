"""Shared base for the synthetic test lab (PR27).

The lab (docs/SYNTHETIC_LAB.md) provides isolated, deterministic,
loopback-only synthetic challenge targets the harness can be pointed at
via ``OZZGRAPH_TARGET``. Isolation is stdlib-only: every HTTP target
serves from a :class:`http.server.ThreadingHTTPServer` bound to
``127.0.0.1`` on an ephemeral port (``0``), file targets keep their
artifacts in a :class:`tempfile.TemporaryDirectory`, and no target ever
touches the public internet or a non-loopback address.

Design rules:

- Deterministic: every target's :attr:`SyntheticTarget.flag` derives
  from its :attr:`SyntheticTarget.name` via :func:`lab_flag`, so two
  runs against the same target observe the same flag. Response bodies,
  file trees, and auth credentials are fixed per target.

- Hidden per category intent: a flag is only reachable through the
  challenge steps its category implies (a hidden route, a valid
  credential, a strings-like scan, a second hop, a chained stage). It
  never appears in the easy initial surface of the target.

- Fail loudly (AGENTS.md rule #9): :class:`LabError` for lifecycle
  misuse — reading ``target_value`` before ``start()``, starting twice,
  or asking the registry for an unknown target. Nothing is silently
  swallowed.

- Clean lifecycle: :meth:`SyntheticTarget.stop` shuts the HTTP server
  down (``server_close``) and removes the temporary directory, so no
  port or file outlives the target. Targets are context managers
  (sync and async), so ``with target:`` bounds the whole lifecycle.
"""

from __future__ import annotations

import hashlib
import threading
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import ClassVar, Self, cast


class LabError(RuntimeError):
    """Base error for the synthetic lab (AGENTS.md rule #9).

    Raised for lifecycle misuse (accessing a target's value before it
    started, starting a target twice) and for unknown target names in
    the registry. Never raised for a normal 4xx challenge response —
    those are target data.
    """


def lab_flag(name: str) -> str:
    """Deterministic flag for a lab target: ``OZ{lab-<name>-<10 hex>}``.

    The digest is the first 10 hex chars of ``sha256("ozzgraph-lab:" +
    name)``, so the flag is stable across processes and runs while
    remaining derived from the target's identity. The ``OZ{...}``
    envelope keeps lab flags distinct from production ``flag{...}``
    flags; point ``OZZGRAPH_FLAG_PATTERN`` at ``OZ\\{[^{}\\s]+\\}`` when
    the harness is aimed at the lab.
    """
    digest = hashlib.sha256(f"ozzgraph-lab:{name}".encode()).hexdigest()[:10]
    return f"OZ{{lab-{name}-{digest}}}"


class SyntheticTarget(ABC):
    """One isolated, deterministic, loopback-only challenge target.

    Subclasses declare their identity as class attributes (``name``,
    ``category``, ``description``) and implement the lifecycle
    (``start``/``stop``) plus a ``target_value`` usable as
    ``OZZGRAPH_TARGET``. The flag is derived deterministically from
    ``name`` and is only readable through the challenge's intended
    steps.

    Lifecycle: ``start()`` raises :class:`LabError` when the target is
    already started; ``stop()`` is idempotent (safe to call when never
    started, e.g. from an exception path) and always releases every
    resource. ``with target:`` and ``async with target:`` bound the
    full lifecycle.
    """

    name: ClassVar[str]
    category: ClassVar[str]
    description: ClassVar[str]

    def __init__(self, *, flag: str | None = None) -> None:
        self._flag = lab_flag(self.name) if flag is None else flag
        self._started = False

    @property
    def flag(self) -> str:
        """The target's deterministic flag (challenge data, not metadata)."""
        return self._flag

    @property
    def started(self) -> bool:
        """True once :meth:`start` has run and :meth:`stop` has not."""
        return self._started

    @property
    @abstractmethod
    def target_value(self) -> str:
        """The value to set ``OZZGRAPH_TARGET`` to (e.g. a loopback URL).

        Raises:
            LabError: If the target is not started.
        """

    @abstractmethod
    def start(self) -> None:
        """Bind the target's resources (server, temp dir) and serve.

        Raises:
            LabError: If the target is already started.
        """

    @abstractmethod
    def stop(self) -> None:
        """Release every resource; idempotent and safe when never started."""

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


class _LabHTTPServer(ThreadingHTTPServer):
    """Loopback ThreadingHTTPServer carrying a reference to its target.

    ``daemon_threads`` keeps request threads from blocking interpreter
    exit; the explicit ``shutdown``/``server_close`` in the target's
    ``stop`` releases the port deterministically.
    """

    daemon_threads = True

    def __init__(self, target: LoopbackHttpTarget) -> None:
        self.target = target
        super().__init__(("127.0.0.1", 0), _LabHandler)


class _LabHandler(BaseHTTPRequestHandler):
    """Dispatch ``GET``/``HEAD`` to the owning target's ``respond``.

    The target's ``respond(path, headers)`` returns a
    ``(status, headers, body)`` triple; ``HEAD`` mirrors ``GET``
    without a body (so ``curl -I`` works for recon probes). Request
    logging is silenced — the lab is a test fixture, not a web server.
    """

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve(include_body=False)

    def _serve(self, *, include_body: bool = True) -> None:
        status, headers, body = _target(self).respond(self.path, dict(self.headers.items()))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


def _target(handler: BaseHTTPRequestHandler) -> LoopbackHttpTarget:
    """Narrow the handler's generic ``server`` to a lab server's target.

    ``BaseHTTPRequestHandler.server`` is typed as ``BaseServer``;
    every lab server is a :class:`_LabHTTPServer` carrying a target.
    """
    return cast(_LabHTTPServer, handler.server).target


class LoopbackHttpTarget(SyntheticTarget):
    """Base for targets served by one loopback :class:`_LabHTTPServer`.

    Subclasses implement :meth:`respond` — a pure, deterministic
    mapping from ``(path, headers)`` to ``(status, headers, body)`` —
    and inherit the lifecycle, the ``http://127.0.0.1:<port>`` target
    value, and cleanup.
    """

    def __init__(self, *, flag: str | None = None) -> None:
        super().__init__(flag=flag)
        self._server: _LabHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound loopback port; :class:`LabError` when not started."""
        server = self._server
        if server is None:
            raise LabError(f"target {self.name!r} is not started")
        return int(server.server_address[1])

    @property
    def target_value(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self._started:
            raise LabError(f"target {self.name!r} is already started")
        server = _LabHTTPServer(self)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name=f"ozz-lab-{self.name}",
            daemon=True,
        )
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._started = False

    @abstractmethod
    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        """Map one request to ``(status, response_headers, body)``.

        Deterministic by contract: the same path and headers always
        yield the same response. ``headers`` is the request's headers
        (lowercased keys from the HTTP handler), useful for auth
        targets.
        """

    def _plain(self, status: int, text: str) -> tuple[int, dict[str, str], bytes]:
        """A ``text/plain`` response body for the common case."""
        return status, {"Content-Type": "text/plain; charset=utf-8"}, text.encode("utf-8")

    def _html(self, status: int, text: str) -> tuple[int, dict[str, str], bytes]:
        """An ``text/html`` response body for the common case."""
        return status, {"Content-Type": "text/html; charset=utf-8"}, text.encode("utf-8")


class FileTreeTarget(LoopbackHttpTarget):
    """Base for file-based targets: a temp dir served over loopback HTTP.

    The target writes a deterministic file tree into a fresh
    :class:`tempfile.TemporaryDirectory` on :meth:`start` and serves it
    over the same loopback HTTP lifecycle as every other target, so its
    ``target_value`` is still an ``http://127.0.0.1:<port>`` URL. The
    directory listing at ``/`` shows file names only — a flag lives
    INSIDE a file, never in the listing. :meth:`stop` removes the
    whole tree.
    """

    def __init__(self, *, flag: str | None = None) -> None:
        super().__init__(flag=flag)
        self._tmpdir: TemporaryDirectory[str] | None = None

    @property
    def directory(self) -> Path:
        """The live temp directory; :class:`LabError` when not started."""
        tmpdir = self._tmpdir
        if tmpdir is None:
            raise LabError(f"target {self.name!r} is not started")
        return Path(tmpdir.name)

    def start(self) -> None:
        if self._started:
            raise LabError(f"target {self.name!r} is already started")
        tmpdir = TemporaryDirectory(prefix=f"ozz-lab-{self.name}-")
        self._tmpdir = tmpdir
        try:
            self._build_files(Path(tmpdir.name))
            super().start()
        except Exception:
            tmpdir.cleanup()
            self._tmpdir = None
            raise

    def stop(self) -> None:
        super().stop()
        tmpdir, self._tmpdir = self._tmpdir, None
        if tmpdir is not None:
            tmpdir.cleanup()

    @abstractmethod
    def _build_files(self, directory: Path) -> None:
        """Write the deterministic file tree into ``directory``."""

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        return self._serve_file(self.directory, path)

    def _serve_file(self, root: Path, path: str) -> tuple[int, dict[str, str], bytes]:
        """Serve one file (or the sorted directory listing) from ``root``.

        Path traversal is impossible: the resolved candidate must be
        inside ``root`` or it is a 404. The listing is the sorted file
        names only — never contents — so flags stay hidden until the
        right file is fetched.
        """
        clean = urllib.parse.unquote(path.split("?", 1)[0]).lstrip("/")
        if clean == "":
            names = sorted(entry.name for entry in root.iterdir())
            return self._plain(200, "\n".join(names) + ("\n" if names else ""))
        candidate = (root / clean).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return self._plain(404, "not found\n")
        if not candidate.is_file():
            return self._plain(404, "not found\n")
        content_type = (
            "application/octet-stream"
            if candidate.suffix in {".bin", ".dat", ".img"}
            else "text/plain; charset=utf-8"
        )
        return 200, {"Content-Type": content_type}, candidate.read_bytes()
