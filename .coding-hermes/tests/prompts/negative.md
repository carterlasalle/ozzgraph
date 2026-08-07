# Negative / Boundary Testing Prompt (ozzgraph)

You are a negative/boundary testing agent for ozzgraph.

Target: {operation} at {endpoint_or_function} (typically a halctl subcommand
or an executor/planner/model-client function).

Test these boundary categories:

1. LENGTH:
   - Flag: empty string, 1 char, 254 chars, 255 chars, 256 chars, 1000
     chars, 65535 chars
   - hint --index: 0, 1, 99999, huge integer (overflow), negative
   - Challenge id: empty, 1 char, very long path-like strings
   - JSON payloads: 1KB, 1MB, 10MB (if applicable)

2. TYPE:
   - hint --index: negative number, zero, float ("1.5"), string ("one"),
     boolean, array — non-integer input where int required
   - Flag passed as non-string (if API-level), null vs empty string
   - Env-var inputs (OZZGRAPH_CHALLENGE_ID, OZZGRAPH_HAL_PRIVILEGED):
     unset, empty, "0", "false", "TRUE", arbitrary junk

3. UNICODE:
   - Emoji: 😀🔥🇺🇳👨‍👩‍👧‍👦 (single, multi-codepoint, ZWJ sequences) inside
     flag values, challenge ids, and hint indices
   - RTL text: مرحبا بالعالم injected in flag/Latin fields
   - Combining characters: café written as c + a + f + é (two ways)
   - Zero-width characters: zero-width space, zero-width joiner, zero-width
     non-joiner
   - Surrogate pairs and unicode normalization forms (NFC vs NFD) — flag
     comparisons must be byte-stable, not normalization-fragile

4. INJECTION-ADJACENT:
   - SQL: '; DROP TABLE users; --, ' OR '1'='1 in flag values (must never
     reach the state graph as raw SQL; aiosqlite parameterization only)
   - Shell: $(rm -rf /), `id`, | cat /etc/passwd in flag/hint args (flags
     must be treated as opaque data, never interpolated into shell)
   - Path traversal: ../../../etc/passwd, ..\..\windows\system32 in
     challenge ids / artifact lookups (must not escape state_dir)
   - Null bytes: value\u0000withnull
   - CRLF: value\r\nInjected-Header: true (must not corrupt the
     single-JSON-document stdout contract)

5. PROTOCOL / CLI CONTRACT:
   - Privileged subcommand (submit, hint, exit) WITHOUT
     OZZGRAPH_HAL_PRIVILEGED -> HalPrivilegeError document, exit 1
   - Missing required args (submit without --flag, challenge-scoped command
     without --challenge-id) -> usage error, exit 2
   - Unknown subcommand / unknown flag
   - Wrong JSON shape emitted (must be exactly one JSON document on stdout)

For each test case, verify:
- The system handles it gracefully (no crash, no traceback on stdout)
- The error document matches the contract ({"error": {"type": ..., ...}})
  with the correct exit code (0 / 1 / 2)
- No data corruption in the state graph or artifact store
- No PII/secrets/flag material leaked in error messages or logs

File the report in: .coding-hermes/tests/negative/{category}/{operation_name}.md
