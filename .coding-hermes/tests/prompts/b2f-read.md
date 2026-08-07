# B2F — Read Path Testing Prompt (ozzgraph)

You are a Back-to-Front testing agent testing ozzgraph.

Operation: {operation_description}
Read path: {data_store} -> {service1} -> {service2} -> ... -> {exit_point}

At each hop, verify:
1. DATA: Is the value byte-for-byte what was written in the F2B test?
2. TRANSFORM: Is the transformation correct? (serialization, encoding,
   normalization, JSON document shape)
3. PRESENTATION: At the exit point (halctl stdout JSON, terminal render),
   verify:
   - Structure: correct JSON shape, correct nesting, required fields present
   - Render: table alignment, unicode/emoji rendering, no mangled output
   - Content: all expected fields present, no extra fields, correct order

## Canonical ozzgraph read paths (pick one per test)

- Replay: event log (append-only) -> replay.py -> state graph reconstruction
  -> graph hash must equal the original run's hash byte-for-byte.
- Artifact retrieval: artifact store get() -> ArtifactRecord lookup in
  artifacts.json -> content file -> bytes must equal what put() stored
  (hash verified, dedupe respected).
- halctl status --json -> challenge status document -> stdout (one JSON
  document, exit 0). Fields: progress/state as documented.
- halctl scoreboard --json -> scoreboard rows -> table rendering: column
  alignment, unicode/emoji flags, no wrapping/truncation bugs.
- halctl challenge show --json -> normalized challenge details document.

Exit path verification (halctl CLI contract):
- Exit code correct: 0 success, 1 operational failure (HalServiceError,
  HalPrivilegeError, config ValueError), 2 usage failure.
- stdout carries exactly ONE JSON document; no stray logging on stdout.
- stderr empty (or correct for --verbose/debug), no error noise.
- Error documents have the shape {"error": {"type": ..., ...}} and never
  embed flag material or secrets.

Test with terminal rendering when applicable:
- Run the command in a TTY and a pipe (JSON must be identical both ways).
- Verify {visual_check_1} (e.g. scoreboard columns align at 80 cols)
- Verify {visual_check_2} (e.g. emoji/unicode render without mojibake)
- Check long-value handling: no overflow, no truncation of error text

File the report in: .coding-hermes/tests/b2f/{category}/{operation_name}.md
