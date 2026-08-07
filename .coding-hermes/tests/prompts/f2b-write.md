# F2B — Write Path Testing Prompt (ozzgraph)

You are a Front-to-Back testing agent testing ozzgraph.

Operation: {operation_description}
Write path: {entry_point} -> {hop1} -> {hop2} -> ... -> {final_store}

For each hop in the write path, verify:
1. VALUE: Is the data byte-for-byte correct?
2. STRUCTURE: Is the field at the right depth? Right type? Right nesting?
3. WIRING: Did it arrive via the correct channel? Correct module boundary?
4. SIDE EFFECTS: Were logs written? Events emitted? Graph entities persisted?

## Canonical ozzgraph write paths (pick one per test)

- halctl submit --flag <flag> -> hal_client (HalCTF MCP adapter) -> flag
  validation (flags/submissions) -> state graph (aiosqlite) + artifact
  store (artifacts.json index + content-addressed content files) + event
  log (graph.* events). Privileged: requires OZZGRAPH_HAL_PRIVILEGED.
- Executor turn: model proposal -> executor.turn -> PhaseRouter -> Planner
  -> ModelAction contract validation -> bounded approved action ->
  action-<fingerprint> entity persisted -> exactly ONE ActionRequest ->
  tool plane -> observation attached to the action entity.
- hint request: halctl hint --index N -> supervisor -> hint issuance ->
  hint entity + event log.
- artifact write: parser/tool runner put() -> temp file -> atomic
  os.replace -> artifacts.json index rewrite (atomic) -> ArtifactRecord
  (artifact_id, sha256 hash, mime_type, size, source_action, created_at).

At the final store (state graph / artifact store / event log):
- Is the column/field type correct? (SQLite column types, JSON field types)
- Is the artifact content-addressed hash correct (sha256) and dedupe working?
- Is the graph mutation atomic (transactional, no torn writes)?
- Is the audit trail complete (every mutation mirrored to the event log)?
- Does the persisted plan/plan_step/action entity carry the correct id
  scheme (plan ids from graph hash; action-<fingerprint>)?

Negative cases to test:
- {negative_case_1} (e.g. submit malformed flag, wrong flag format)
- {negative_case_2} (e.g. non-privileged model calls submit -> HalPrivilegeError, exit 1)
- {negative_case_3} (e.g. budget exhausted mid-turn -> BudgetExceeded, no partial write)

Output a test report with:
- PASS for each verification point
- FAIL with exact mismatch details (expected X, got Y, at hop Z)
- UNTESTABLE with reason (e.g. "no live HalCTF MCP to verify kernel verdict")

File the report in: .coding-hermes/tests/f2b/{category}/{operation_name}.md
