"""Concrete synthetic challenge targets for the OzzGraph lab (PR27).

One target per category from docs/TESTING_AND_QA.md "Synthetic
Challenge Suite" (HTTP reconnaissance, hidden routes, authentication
logic, source vulnerability localization, file forensics, binary
string extraction, credential reuse, simple network pivot, multi-stage
flag discovery). Every target is loopback-only and deterministic; see
docs/SYNTHETIC_LAB.md for the full catalogue and solve walkthroughs.

Challenge design per target (flags are OZ{...}; see :func:`lab_flag`):

- :class:`HttpReconTarget` — the flag rides in a custom response
  header (``X-Ozz-Lab-Flag``), the canonical ``curl -I`` recon step;
  the body itself never contains it.
- :class:`HiddenRoutesTarget` — ``/robots.txt`` advertises a disallowed
  ``/admin`` route that holds the flag; ``/`` never contains it.
- :class:`AuthLogicTarget` — a deterministic Basic-auth challenge: the
  credentials are discoverable from the challenge page, and the flag is
  returned only after a valid ``Authorization`` header.
- :class:`SourceVulnTarget` — serves a small source file whose
  vulnerable line carries the flag in an adjacent comment (localize
  the vulnerability, read the flag).
- :class:`FileForensicsTarget` — a temp file tree whose directory
  listing shows names only; the flag lives inside a non-obvious file.
- :class:`BinaryStringsTarget` — a deterministic binary blob with the
  flag embedded as printable ASCII; extractable only via a
  strings-like scan (``strings``), never in the listing or the raw
  HTTP framing text.
- :class:`CredentialReuseTarget` — a leaked credential file on one
  endpoint unlocks a different endpoint; the flag requires reusing
  those credentials.
- :class:`NetworkPivotTarget` — two loopback servers; the entry target
  discloses the second server's address, and the flag lives on the
  second hop only.
- :class:`MultiStageTarget` — two chained steps: stage 1 reveals the
  stage 2 path, stage 2 holds the flag; the token is deterministic.

All responses are plain deterministic text/HTML; every ``stop()``
releases the server and (for file targets) the temp directory.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from ozzgraph.lab.base import FileTreeTarget, LabError, LoopbackHttpTarget

#: Credentials for the Basic-auth targets. Deterministic by design:
#: they are discoverable from the challenge surface, never guessed.
AUTH_USER = "admin"
AUTH_PASSWORD = "labpass"

#: ``Authorization`` header value for the valid credential pair.
AUTH_HEADER = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASSWORD}".encode()).decode()


def _authorized(headers: Mapping[str, str]) -> bool:
    """True when ``headers`` carries the deterministic valid credential."""
    return headers.get("Authorization", "") == AUTH_HEADER


class HttpReconTarget(LoopbackHttpTarget):
    """HTTP reconnaissance: the flag is in a custom response header.

    The intended step is a plain HTTP probe (``curl -I`` / ``-D -``):
    the ``X-Ozz-Lab-Flag`` response header carries the flag, while the
    body is an ordinary-looking page that never contains it.
    """

    name: ClassVar[str] = "http-recon"
    category: ClassVar[str] = "HTTP reconnaissance"
    description: ClassVar[str] = (
        "A loopback web service whose X-Ozz-Lab-Flag response header "
        "holds the flag; reachable with a plain HTTP probe (curl -I)."
    )

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path != "/":
            return self._plain(404, "not found\n")
        body = (
            "<!doctype html><html><body><h1>OzzGraph Lab</h1>"
            "<p>HTTP reconnaissance target. The flag is in a header.</p>"
            "</body></html>\n"
        )
        return (
            200,
            {
                "Content-Type": "text/html; charset=utf-8",
                "Server": "OzzGraphLab/1.0",
                "X-Ozz-Lab-Flag": self.flag,
            },
            body.encode("utf-8"),
        )


class HiddenRoutesTarget(LoopbackHttpTarget):
    """Hidden routes: the flag sits behind an advertised /admin route.

    ``/robots.txt`` disallows ``/admin``; only ``/admin`` returns the
    flag. ``/`` is a plain page and never contains it (the isolation
    failure-path fixture for the lab).
    """

    name: ClassVar[str] = "hidden-routes"
    category: ClassVar[str] = "hidden routes"
    description: ClassVar[str] = (
        "robots.txt disallows /admin, which is the only route holding "
        "the flag; the root page never contains it."
    )

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/":
            return self._html(200, "<!doctype html><html><body><h1>Home</h1></body></html>\n")
        if path == "/robots.txt":
            return self._plain(200, "User-agent: *\nDisallow: /admin\n")
        if path == "/admin":
            return self._plain(200, f"{self.flag}\n")
        return self._plain(404, "not found\n")


class AuthLogicTarget(LoopbackHttpTarget):
    """Authentication logic: the flag requires a valid credential.

    The challenge page discloses the deterministic credentials
    (admin/labpass) as an HTML comment; ``/admin`` answers 401 without
    a matching ``Authorization`` header and returns the flag only
    after it. Credentials are challenge data, discoverable by the
    intended step (read the page), never by guessing.
    """

    name: ClassVar[str] = "auth-logic"
    category: ClassVar[str] = "authentication logic"
    description: ClassVar[str] = (
        "A Basic-auth gate: /admin requires the credentials disclosed "
        "on the challenge page; the flag is served only after a valid "
        "Authorization header."
    )

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/":
            return self._html(
                200,
                "<!doctype html><html><body><h1>Login</h1>"
                f"<!-- default credentials: {AUTH_USER} / {AUTH_PASSWORD} -->"
                "</body></html>\n",
            )
        if path == "/admin":
            if _authorized(headers):
                return self._plain(200, f"{self.flag}\n")
            return (
                401,
                {"WWW-Authenticate": 'Basic realm="ozzgraph-lab"'},
                b"unauthorized\n",
            )
        return self._plain(404, "not found\n")


class SourceVulnTarget(LoopbackHttpTarget):
    """Source vulnerability localization: the flag sits next to the vuln.

    The target serves a tiny Python source file whose vulnerable line
    (``os.system`` on unsanitized input) carries the flag in the
    adjacent comment. The intended step is fetching the source and
    localizing the vulnerability.
    """

    name: ClassVar[str] = "source-vuln"
    category: ClassVar[str] = "source vulnerability localization"
    description: ClassVar[str] = (
        "Serves /src/app.py: the flag is in the comment adjacent to "
        "the vulnerable os.system call, so localization finds it."
    )

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/":
            return self._html(
                200,
                "<!doctype html><html><body><h1>App</h1>"
                '<p><a href="/src/app.py">source</a></p></body></html>\n',
            )
        if path == "/src/app.py":
            source = (
                '"""Order processing service."""\n'
                "import os\n"
                "\n"
                "\n"
                "def run(order_id: str) -> None:\n"
                f"    # {self.flag}\n"
                '    os.system(f"echo {order_id}")  # vulnerable: unsanitized input\n'
            )
            return self._plain(200, source)
        return self._plain(404, "not found\n")


class FileForensicsTarget(FileTreeTarget):
    """File forensics: the flag lives inside a non-obvious file.

    The directory listing shows file names only; the flag is inside
    ``.backup/creds.old``, a dot-directory file that only a forensic
    look at the tree (``find`` / directory listing) reveals.
    """

    name: ClassVar[str] = "file-forensics"
    category: ClassVar[str] = "file forensics"
    description: ClassVar[str] = (
        "A temp file tree whose listing shows names only; the flag is "
        "inside .backup/creds.old, found by inspecting the tree."
    )

    def _build_files(self, directory: Path) -> None:
        (directory / "README.txt").write_text(
            "Operator notes for the challenge lab.\nNothing sensitive here.\n",
            encoding="utf-8",
        )
        (directory / "logs.txt").write_text(
            "2026-08-07 09:00:00 INFO startup complete\n2026-08-07 09:01:00 INFO heartbeat ok\n",
            encoding="utf-8",
        )
        backup = directory / ".backup"
        backup.mkdir()
        (backup / "creds.old").write_text(
            f"service_account_password={self.flag}\n", encoding="utf-8"
        )


class BinaryStringsTarget(FileTreeTarget):
    """Binary string extraction: the flag is embedded ASCII in a blob.

    ``data.bin`` is a deterministic pseudo-random byte blob with the
    flag embedded as printable ASCII between binary junk; the intended
    step is a strings-like scan (``strings data.bin``). The directory
    listing and the HTTP framing never contain the flag text.
    """

    name: ClassVar[str] = "binary-strings"
    category: ClassVar[str] = "binary string extraction"
    description: ClassVar[str] = (
        "data.bin embeds the flag as ASCII inside deterministic binary "
        "junk; extractable only via a strings-like scan."
    )

    def _build_files(self, directory: Path) -> None:
        junk = bytes((index * 31 + 7) % 256 for index in range(4096))
        blob = (
            b"\x00\x01\x02BINF\x00"
            + junk[:1024]
            + b"\x00"
            + self.flag.encode("ascii")
            + b"\x00"
            + junk[1024:]
            + b"\x00\xff\xfeEND"
        )
        (directory / "data.bin").write_bytes(blob)


class CredentialReuseTarget(LoopbackHttpTarget):
    """Credential reuse: a leaked credential unlocks a second endpoint.

    ``/backup/creds.txt`` leaks the deterministic credentials
    (admin/labpass); ``/admin`` accepts exactly those credentials and
    serves the flag. The intended step is finding the leak and reusing
    the credential on the protected endpoint.
    """

    name: ClassVar[str] = "credential-reuse"
    category: ClassVar[str] = "credential reuse"
    description: ClassVar[str] = (
        "/backup/creds.txt leaks a credential that /admin accepts; the "
        "flag requires reusing the leaked credential."
    )

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/":
            return self._html(
                200,
                "<!doctype html><html><body><h1>Admin Portal</h1>"
                '<p><a href="/backup/creds.txt">backup</a></p>'
                "</body></html>\n",
            )
        if path == "/backup/creds.txt":
            return self._plain(200, f"username={AUTH_USER}\npassword={AUTH_PASSWORD}\n")
        if path == "/admin":
            if _authorized(headers):
                return self._plain(200, f"{self.flag}\n")
            return (
                401,
                {"WWW-Authenticate": 'Basic realm="ozzgraph-lab"'},
                b"unauthorized\n",
            )
        return self._plain(404, "not found\n")


class NetworkPivotTarget(LoopbackHttpTarget):
    """Simple network pivot: the entry target discloses a second hop.

    Two loopback servers: the entry target (this target's
    ``target_value``) responds to ``/pivot`` with the internal
    server's full address; the flag lives only on that second server.
    The intended step is probing the entry, learning the internal
    address, and pivoting to it.
    """

    name: ClassVar[str] = "network-pivot"
    category: ClassVar[str] = "simple network pivot"
    description: ClassVar[str] = (
        "The /pivot endpoint discloses a second loopback server's "
        "address; the flag lives only on that internal server."
    )

    def __init__(self, *, flag: str | None = None) -> None:
        super().__init__(flag=flag)
        self._internal: InternalFlagTarget | None = None

    def start(self) -> None:
        if self._started:
            raise LabError(f"target {self.name!r} is already started")
        internal = InternalFlagTarget(flag=self.flag)
        internal.start()
        try:
            super().start()
        except Exception:
            internal.stop()
            raise
        self._internal = internal

    def stop(self) -> None:
        super().stop()
        internal, self._internal = self._internal, None
        if internal is not None:
            internal.stop()

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/":
            return self._html(
                200,
                "<!doctype html><html><body><h1>Entry</h1>"
                '<p><a href="/pivot">pivot</a></p></body></html>\n',
            )
        if path != "/pivot":
            return self._plain(404, "not found\n")
        internal = self._internal
        if internal is None:
            raise LabError(f"target {self.name!r} is not started")
        return self._plain(200, f"internal admin at {internal.target_value}/flag\n")


class InternalFlagTarget(LoopbackHttpTarget):
    """The second hop of the pivot: serves the flag at /flag.

    Not registered in the lab registry; constructed by
    :class:`NetworkPivotTarget` with the pivot's flag so both hops
    share one deterministic flag.
    """

    name: ClassVar[str] = "network-pivot-internal"
    category: ClassVar[str] = "simple network pivot"
    description: ClassVar[str] = "Internal hop of the network pivot (not registered)."

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/flag":
            return self._plain(200, f"{self.flag}\n")
        return self._plain(404, "not found\n")


class MultiStageTarget(LoopbackHttpTarget):
    """Multi-stage flag discovery: two chained steps, deterministic token.

    Stage 1 (``/stage1``) reveals the stage 2 path, which embeds a
    deterministic token derived from the target name; stage 2
    (``/stage2/<token>``) holds the flag. The token is stable across
    runs, so the challenge is deterministic while still requiring the
    chained discovery.
    """

    name: ClassVar[str] = "multi-stage"
    category: ClassVar[str] = "multi-stage flag discovery"
    description: ClassVar[str] = (
        "/stage1 reveals a deterministic /stage2/<token> path; only stage 2 holds the flag."
    )

    def __init__(self, *, flag: str | None = None) -> None:
        super().__init__(flag=flag)
        self._token = hashlib.sha256(f"ozzgraph-lab:{self.name}".encode()).hexdigest()[:16]

    def respond(self, path: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        if path == "/":
            return self._html(
                200,
                "<!doctype html><html><body><h1>Stages</h1>"
                '<p><a href="/stage1">stage 1</a></p></body></html>\n',
            )
        if path == "/stage1":
            return self._plain(200, f"next: /stage2/{self._token}\n")
        if path == f"/stage2/{self._token}":
            return self._plain(200, f"{self.flag}\n")
        return self._plain(404, "not found\n")
