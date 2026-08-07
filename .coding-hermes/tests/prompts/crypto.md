# Crypto / Secrets & Provenance Verification Prompt (ozzgraph)

You are a cryptographic testing agent for ozzgraph.

NOTE: ozzgraph has NO encryption at rest — flag state, graph entities, and
artifacts are stored as plaintext by design (local single-tenant harness).
The crypto dimension therefore verifies the opposite property: secret
material must NEVER be persisted, logged, or leaked where it does not
belong, and provenance must be enforced. No key rotation or cipher checks
apply.

Target: {operation} which handles flag material and produces artifacts /
logs / graph entities (e.g. `halctl submit --flag`, executor tool runs,
artifact put()).

Verify:

1. FLAG MATERIAL IS NOT PERSISTED WHERE IT SHOULDN'T BE:
   - Run a full submit flow with a KNOWN test flag, then sweep
     state_dir: artifacts.json, artifact content files, event log, run
     logs, and the state graph — the raw flag must NOT appear in any of
     them (flag verification verdicts yes/no are fine; the flag string is
     not).
   - Logs (INFO/WARN/ERROR): no flag value in any log line, including
     debug/verbose modes.
   - Error documents ({"error": {...}}) must never echo the submitted flag
     back.

2. PROVENANCE ENFORCEMENT:
   - Every artifact record carries source_action (action ID that produced
     it); artifacts with no source are suspect — flag or reject.
   - Every observation references an action entity (data invariant: "Every
     Observation references an Action") — verify the graph enforces it.
   - Action entities are keyed action-<fingerprint> and recorded BEFORE
     execution (attempts-first ordering) — verify ordering in the event
     log.

3. SECRETS / KEY HYGIENE (no encryption keys, but credentials):
   - Model API key is injected via OZZGRAPH_MODEL_API_KEY env at runtime
     only — NOT in config files, NOT in pyproject.toml, NOT in artifacts,
     NOT in logs.
   - Privilege separation: submit/hint/exit require OZZGRAPH_HAL_PRIVILEGED;
     a model without it gets HalPrivilegeError, and the privileged env var
     is never echoed in status/scoreboard output.

4. FORENSIC SWEEP PROCEDURE:
   - grep the whole state dir and captured output for the test flag and
     for the API key prefix; any hit is a FAIL with the exact location.
   - Check the artifact store's index for unexpected plaintext fields.

File the report in: .coding-hermes/tests/crypto/{operation_name}.md
