# AGENTS.md

This file governs all coding-agent work in the OzzGraph repository.

## Mission

Implement OzzGraph as a small, deterministic, model-adaptive autonomous CTF harness for authorized, isolated environments.

The system must not collapse into one giant prompt or one unbounded ReAct loop.

The harness owns:

- state
- budgets
- scope enforcement
- process execution
- evidence provenance
- retries
- context management
- scheduling
- hint policy
- flag validation
- lifecycle and recovery

Models own only bounded judgment at uncertain decision points.

## Non-Negotiable Architecture Rules

1. **Authoritative state lives outside model context.**
   - Use SQLite-backed graph state.
   - Use append-only JSONL events.
   - Raw artifacts live in the artifact store.

2. **Transcripts are not memory.**
   - Never treat a conversation transcript as authoritative state.
   - Compaction may shrink active context but must never destroy durable history.

3. **Every confirmed fact has provenance.**
   - A model claim is a hypothesis.
   - A fact requires deterministic evidence or evaluator acceptance tied to evidence IDs.

4. **One bounded action per executor turn.**
   - No multi-command plans disguised as one action unless the skill explicitly defines a bounded script.
   - Every action has a timeout, output limit, and fingerprint.

5. **Models never call raw MCP methods.**
   - MCP is wrapped behind the local `halctl` adapter.
   - Only the supervisor may submit flags, buy paid hints, or exit the run.

6. **Skills load lazily.**
   - Do not place every tool or security technique in every prompt.
   - Advertise summaries first; load full skill cards only when selected.

7. **Parallelize evidence gathering, not mutable exploit chains.**
   - Workers must have explicit dependencies and conflict keys.
   - Flag submission and paid hints are always serialized.

8. **Graph-driven phase transitions only.**
   - Do not transition phases because an arbitrary number of actions elapsed.
   - Transition based on state predicates.

9. **Fail loudly.**
   - Every fatal path must produce a structured termination event and a human-readable summary.
   - No silent exception swallowing.

10. **Keep the kernel small.**
    - Category-specific logic belongs in skills, parsers, policies, or adapters.
    - The supervisor must not become a dumping ground.

## Required Technology

### Python

- Python 3.12
- `uv` for dependencies and lockfiles
- Pydantic v2 for schemas
- Async I/O where it materially improves model, MCP, or worker concurrency
- SQLite for durable state

### TypeScript

Only for the optional dashboard:

- Yarn
- Strict TypeScript
- Never include the dashboard runtime in the competition image

## Style

- Fully typed public interfaces
- Small modules with clear ownership
- Explicit error types
- Dataclasses or Pydantic models for state contracts
- No hidden global mutable state
- No dynamic imports for core runtime behavior
- Avoid clever abstractions before two real implementations justify them
- Prefer deterministic code over extra model calls

## Security Boundaries

All model output is untrusted.

Before command execution:

1. Parse the selected adapter protocol.
2. Validate schema.
3. Enforce command-length limits.
4. Enforce target allowlists.
5. Block platform and public-internet destinations.
6. Check worker and phase permissions.
7. Compute a normalized fingerprint.
8. Reject duplicates.
9. Attach timeout and output limits.
10. Record the attempted action before execution.

Target output is also untrusted. Always label it as data in prompts and never merge it into system instructions.

## Data Invariants

The following invariants must always hold:

- Every `Fact` references at least one `Evidence`.
- Every `Evidence` references an `Observation` or artifact.
- Every `Observation` references an `Action`.
- Every `Submission` references a `FlagCandidate`.
- Every submitted `FlagCandidate` has observed provenance.
- Every graph mutation is representable as an append-only event.
- Replaying all events reconstructs the same graph hash.
- A worker cannot mutate state outside its declared task scope.
- Paid hint count never exceeds the configured maximum.
- The supervisor is the only component that can invoke privileged HalCTF operations.

## Testing Expectations

Every change must include the smallest appropriate tests.

Kernel changes normally require:

- unit test
- integration test
- replay or golden-trace test when state changes
- failure-path test

Prompt or adapter changes require:

- format-compliance fixtures
- malformed-output fixture
- contradiction fixture
- at least two model-profile regression fixtures

Tool or parser changes require:

- representative success output
- representative failure output
- truncation behavior
- adversarial or malformed output

## Pull Request Scope

One PR should implement one architectural layer or a tightly related slice.

Do not combine:

- graph schema changes with dashboard work
- model adapters with unrelated security skills
- lifecycle changes with broad refactors
- prompt rewrites with container dependency changes

Use the sequence in `docs/IMPLEMENTATION_PLAN.md`.

## Definition of Done for a PR

A PR is complete only when:

- code is implemented
- documentation is updated
- tests cover success and failure
- `uv run ruff check .` passes
- `uv run mypy src` passes
- `uv run pytest` passes
- no architectural invariant is weakened
- errors fail loudly
- replay compatibility is preserved or migrated
- dependency and image-size impact is explained

## Forbidden Shortcuts

Do not:

- bundle model weights
- add CUDA, PyTorch, or vLLM to the competition image
- give models raw unrestricted MCP access
- let workers submit flags
- let models purchase hints
- treat model prose as confirmed state
- use a vector database in v1
- add a multi-agent debate loop without measured benefit
- expose every installed tool to every model
- delete failed branches during compaction
- add public-internet dependencies
- suppress timeouts or retry forever
- commit secrets, tokens, generated credentials, or live challenge artifacts

## Work Procedure

Before editing:

1. Read the relevant document under `docs/`.
2. Identify the owning module.
3. State which invariants the change touches.
4. Add or update tests first when practical.
5. Implement the smallest complete slice.
6. Run focused tests.
7. Run the full quality gate.
8. Update documentation and ADRs for architectural decisions.

## Architecture Decision Records

Create an ADR under `docs/adr/` when a change:

- introduces a new persistent storage technology
- changes a core model interaction protocol
- changes privileged-operation ownership
- changes graph event semantics
- changes process-isolation policy
- changes the runtime language or package manager
- adds a new concurrency model
- weakens an existing invariant

Use the template in `docs/adr/0000-template.md`.
