# Synthetic Test Lab (PR27)

The synthetic lab provides **isolated, deterministic, loopback-only**
challenge targets the OzzGraph harness can be pointed at via
`OZZGRAPH_TARGET`. It implements Phase 10's "synthetic targets"
deliverable (docs/IMPLEMENTATION_PLAN.md, PR #27) and the target
catalogue of docs/TESTING_AND_QA.md ("Synthetic Challenge Suite").

Isolation is stdlib-only:

- every HTTP target is a `http.server.ThreadingHTTPServer` bound to
  `127.0.0.1` on an ephemeral port (`0`) — never a public address;
- file targets keep their artifacts in a
  `tempfile.TemporaryDirectory` and serve the same tree over the same
  loopback HTTP lifecycle, so their `target_value` is still a URL;
- `stop()` shuts the server down (`server_close`) and removes the temp
  tree — no port or file outlives the target;
- no new runtime dependencies, no Docker, no public internet.

The lab lives in `src/ozzgraph/lab/` — a category-specific fixture
outside the supervisor, keeping the kernel small (AGENTS.md rule #10).

## Target Catalogue

One target per suite category, in registry order
(`ozzgraph.lab.LAB_REGISTRY`):

| name | category | flag location |
|------|----------|---------------|
| `http-recon` | HTTP reconnaissance | `X-Ozz-Lab-Flag` response header on `/` |
| `hidden-routes` | hidden routes | `/admin` (advertised by `/robots.txt`) |
| `auth-logic` | authentication logic | `/admin` after valid Basic credentials |
| `source-vuln` | source vulnerability localization | comment next to the vulnerable line in `/src/app.py` |
| `file-forensics` | file forensics | inside `.backup/creds.old` (listing shows names only) |
| `binary-strings` | binary string extraction | ASCII flag embedded in `data.bin`, found via `strings` |
| `credential-reuse` | credential reuse | `/admin` after reusing the credential leaked in `/backup/creds.txt` |
| `network-pivot` | simple network pivot | second hop server, whose address `/pivot` discloses |
| `multi-stage` | multi-stage flag discovery | `/stage2/<token>`, whose path `/stage1` reveals |

Flags are deterministic: `OZ{lab-<name>-<10 hex>}`, derived from the
target name (see `ozzgraph.lab.lab_flag`), and are only reachable
through the intended challenge steps — never in the easy initial
surface of the target.

## Running the Lab

The lab is exercised by the test suite:

```bash
uv run pytest tests/test_lab.py tests/test_lab_solve.py
```

- `tests/test_lab.py` — lifecycle, registry determinism, and per-target
  flag discovery through the bounded shell runner (`ShellRunner` +
  `curl`), including the isolation failure-path fixtures.
- `tests/test_lab_solve.py` — integration solves that drive the REAL
  harness code paths against a live target: deterministic bootstrap
  probes, the executor loop, observation parsing, provenance-backed
  flag extraction, supervisor-only submission, and the phase router's
  DONE state.

## Pointing the Harness at a Target

Start a target, read its `target_value`, and set it as
`OZZGRAPH_TARGET`:

```python
from ozzgraph.lab import get_target

with get_target("hidden-routes") as target:
    print(target.target_value)  # e.g. http://127.0.0.1:39821
```

Because lab flags use the `OZ{...}` envelope (distinct from production
`flag{...}` flags), also point the flag pattern at the lab and
allowlist the loopback address:

```bash
export OZZGRAPH_TARGET=http://127.0.0.1:39821
export OZZGRAPH_TARGET_ALLOWLIST=127.0.0.1
export OZZGRAPH_FLAG_PATTERN='OZ\{[^{}\s]+\}'
```

All lab servers bind `127.0.0.1` only, so the harness's fail-closed
scope policy blocks them unless `OZZGRAPH_TARGET_ALLOWLIST` includes
the loopback address — exactly as it would for any authorized target.

## Design Contract

- **Determinism**: same target name → same flag, same responses, same
  credentials, same stage tokens, across processes and runs.
- **Isolation**: flags are hidden per category intent; the registry's
  `list_targets()` catalogue never contains a flag; `get_target(name)`
  returns a fresh instance per call.
- **Fail loudly**: lifecycle misuse (reading `target_value` before
  `start()`, starting twice) and unknown target names raise
  `ozzgraph.lab.LabError`.
- **Clean lifecycle**: targets are sync and async context managers;
  `stop()` always releases the server and the temp tree.
