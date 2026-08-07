# Visual / Render Testing Prompt (ozzgraph — terminal-native adaptation)

You are a visual testing agent for ozzgraph.

NOTE: ozzgraph is a terminal-native CLI (halctl) — there are NO browser
breakpoints, no DOM, no CSS. Replace browser viewport checks with terminal
render checks. Reports are filed under .coding-hermes/tests/b2f/render/ per
the coding-hermes-testing v1.0 convention.

Target: {command_or_component} (e.g. `halctl scoreboard --json`,
`halctl status --json`, executor TERMINATION summary, error documents)

Render conditions to test (TTY vs pipe):
1. Run the command in a real TTY at widths 80, 100, 120 cols AND piped
   (stdout must be identical — the single-JSON-document contract must not
   depend on isatty).
2. Verify:
   - Scoreboard table alignment: columns align, no ragged edges, no
     horizontal overflow at 80 cols
   - Unicode/emoji rendering: emoji flags and unicode text render without
     mojibake (no surrogate escapes, no broken ZWJ sequences) — and degrade
     gracefully on non-UTF-8 terminals if applicable
   - Error-message visibility: error documents render on stdout with a
     non-zero exit code; errors are NEVER hidden or written only to stderr
   - ANSI/color handling: color codes present in TTY, absent/stripped when
     piped (no raw escape sequences leaking into piped JSON)
   - Long-value handling: long flag strings, long challenge ids, and long
     error reasons wrap or truncate cleanly — no layout corruption
   - TERMINATION summary: human-readable summary renders correctly on every
     exit path (AGENTS.md rule 9)

Content verification:
- {content_check_1} (e.g. scoreboard contains exactly the columns from the
  HalCTF contract)
- {content_check_2} (e.g. status progress field present with correct units)
- {content_check_3} (e.g. JSON parses with json.loads when piped)

File the report in: .coding-hermes/tests/b2f/render/{component_name}.md
