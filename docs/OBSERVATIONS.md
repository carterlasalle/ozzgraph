# Semantic Observations (V04)

> Milestone 4 of `docs/CHANGES_v2.md`: typed parsers/projectors for the
> highest-value tools, with mandatory raw-artifact persistence **before**
> summarization.

## Raw-first invariant

Target output is untrusted data (AGENTS.md Security Boundaries), and the
RAW output is authoritative state (AGENTS.md rule #1). The runner therefore
persists raw output to the `ArtifactStore` **first**, and only then parses:

1. `ShellRunner.run(...)` returns a bounded `ToolResult`.
2. The runner stores `result.stdout` bytes in the artifact store
   (`ArtifactStore.put`) and gets back an artifact id.
3. The runner picks the semantic parser for the producing command
   (`parser_for_command`) and parses the `ToolResult`, attaching the
   artifact id (`observation.artifact_ids`).
4. The observation entity (typed `data`, bounded `summary`, `source`,
   `kind`, `ok`, `malformed`, `parse_error`) and the evidence entity
   (referencing observation + artifact) enter the state graph.

Consequences:

- A parse failure never loses the raw output: the artifact is already on
  disk, and the observation is `malformed=True` with a bounded
  `parse_error` and excerpt (fail loudly, never raise).
- The flag-candidate extractor can scan the referenced artifact contents
  because observation payloads carry `artifact_ids`.
- Parsers themselves are pure and perform no I/O (same input → same
  observation, deterministic and replay-safe).

## Parser registry

The registry is the deterministic `PARSERS` dict in
`src/ozzgraph/observations.py`, keyed by `(source, kind)` and populated at
import (no discovery, AGENTS.md rule #10). Add a parser = one class + one
`register_parser` call. `get_parser(source, kind)` resolves keys and
`parser_for_command(command)` resolves the parser for a shell command line
(wrapper/`sh -c`/alias aware, flag-gated on the machine-readable format).

| source       | kind   | Tool output format                          | Typed observation payload                          |
|--------------|--------|---------------------------------------------|----------------------------------------------------|
| `curl`       | `text` | `-w '%{json}'`, `-i`/`-D -` headers, body   | status/URL/redirect, headers, body stats           |
| `nmap`       | `xml`  | `-oX -`                                     | hosts, addresses, ports, services, scripts, OS     |
| `ffuf`       | `json` | `-json` / `-of json` / `-o out.json`        | results, status histogram                          |
| `feroxbuster`| `json` | `--json` / `-o out.json`                    | results, headers, technologies, status histogram   |
| `nuclei`     | `jsonl`| `-jsonl` / `-json`                          | findings (template, severity, host, matcher)       |
| `netexec`    | `jsonl`| `--json`                                    | hosts (address, port, protocol, `json_host`)       |
| `smbmap`     | `text` | share tables, shared paths, permissions     | hosts, shares, paths, errors                       |
| `ldapsearch` | `ldif` | `-LLL`                                      | entries (dn + attribute → values, base64 decoded)  |
| `semgrep`    | `json` | `--json`                                    | findings (check id, path, line, severity)          |
| `semgrep`    | `sarif`| `--sarif`                                   | tool driver, rules, results (file/line)            |
| `codeql`     | `sarif`| `database analyze --format=sarif-*`         | tool driver, rules, results (file/line)            |
| `trivy`      | `json` | `--format json`                             | targets, vulnerabilities (CVE, pkg, versions, CVSS)|
| `gitleaks`   | `json` | `--report-format json` (default)            | redacted findings (rule, file, line, commit)       |
| `file`       | `text` | `path: description` lines                   | file entries                                       |
| `readelf`    | `text` | `-h` / `-S` / `-d` / `-l` / `-s`            | ELF header, sections, libs, program headers        |
| `checksec`   | `text` | table, `[*] '/path'` blocks, `--output=json`| per-file RELRO/canary/NX/PIE/FORTIFY               |
| `exiftool`   | `json` | `-json`                                     | per-file identity + capped tag map                 |
| `exiftool`   | `text` | default `Tag: Value`                        | tag map with FileType/MIMEType promoted            |
| `binwalk`    | `text` | `DECIMAL HEXADECIMAL DESCRIPTION` table     | offset/hex/description entries                     |

Format notes:

- **SARIF** (semgrep + CodeQL) share one normalizer: the document shapes
  are identical, only the `source` differs.
- **XML hardening**: nmap's parser rejects internal entity definitions and
  internal DTD subsets up front (billion-laughs defense); a bare
  `<!DOCTYPE name>` is tolerated because the default ElementTree parser
  never resolves external DTDs.
- **gitleaks**: the observation carries the *location* (rule, file, line,
  commit, author, entropy) with `secrets_redacted: true` — the secret text
  lives only in the raw artifact, never in model context or graph payload.
- **JSONL** (nuclei, netexec) is strict: a broken or non-object line is a
  structured `malformed=True` error naming the line, never skipped.
- Text-format tools (curl, smbmap, file, readelf, checksec, exiftool text,
  binwalk) degrade to labeled line data (`line_count`, `first_line`, ...)
  when their structure is unrecognized — text output is always parseable
  as data, never a crash.

## Command → parser dispatch

`parser_for_command(command)` resolves the leading command (skipping
`sudo`/`env`/`timeout`/... prefixes and unwrapping `sh -c '...'` shells,
mapping `nxc` → `netexec`) and returns:

- the tool's semantic parser when the machine-readable format was
  requested (`nmap -oX`, `nuclei -jsonl`, `semgrep --sarif`, `trivy
  --format json`, `exiftool -json`, ...), or
- the generic `shell`/`text` parser otherwise (plain-text output).

This keeps observations clean: a plain `nmap -sV host` run parses as shell
text; `nmap -oX - host` yields typed hosts/ports.

## Tests

`tests/test_observations.py` covers every parser with realistic fixtures
(nmap XML, ffuf/feroxbuster/trivy/gitleaks/semgrep JSON, semgrep/CodeQL
SARIF, nuclei/netexec JSONL, LDIF, and the text formats), malformed and
adversarial inputs (broken JSON/XML, entity declarations, non-array
reports, non-LDIF banners), registry lookups, command dispatch, and the
raw-first invariant (raw bytes on disk before parse; observations
reference the artifact). `tests/test_runner.py` drives the full
investigate loop with a canned `nmap -oX` run and asserts the typed
observation + evidence entities and byte-for-byte artifact content.
