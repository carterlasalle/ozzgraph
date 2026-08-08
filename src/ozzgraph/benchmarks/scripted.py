"""Deterministic scripted model for the V10 benchmark suite.

The V10 benchmarks are hermetic: they run a scripted deterministic
model (this module) instead of a real LLM, so the suite is
reproducible in CI with zero network cost and zero nondeterminism
(docs/BENCHMARKS.md, "Hermetic determinism"). The scripted model is a
stand-in for a competent (or naive) agent: it emits one bounded
terminal-protocol action per turn, following a per-target probe script
(:mod:`ozzgraph.benchmarks.registry`) that mirrors the challenge's
intended steps, and optionally submits the flag.

Two forms, mirroring the matrix evaluator's client forms
(:mod:`ozzgraph.matrix`):

- :class:`ScriptedModel` — the callable form (``prompt -> completion``
  string), consumed by the plain-ReAct baseline loop.
- :class:`ScriptedModelService` — the service form (an object with an
  async ``complete(ModelRequest)``), consumed by the full
  :class:`~ozzgraph.runner.AutonomousRunner` through its injected
  model service.

The scripted model is prompt-aware ONLY to locate the live target URL
(the lab binds an ephemeral loopback port per instance); its behavior
otherwise depends only on its own turn counter, so a run is a pure
function of the script and the target. The flag value is injected as
constructor data (the same convention as the matrix suite's
:class:`~ozzgraph.matrix` scripted client) — the scripted model is a
test fixture, not a real model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ozzgraph.model_client import ModelChoice, ModelMessage, ModelRequest, ModelResponse, ModelUsage

#: The lab binds 127.0.0.1 on an ephemeral port; the model locates the
#: live target URL in the prompt to substitute into its script.
_URL_RE = re.compile(r"http://127\.0\.0\.1:\d+")


class ScriptedModel:
    """A deterministic scripted model: one bounded action per turn.

    Args:
        script: The probe-script commands, in order; the model emits
            ``ACTION: run`` with each command (``{url}`` placeholders
            are substituted with the live target URL found in the
            prompt). Commands must be unique within a script — the
            executor rejects duplicate fingerprints.
        flag: The target's flag; emitted by the trailing submit action.
        submit: When True the model emits one ``ACTION: submit`` with
            ``flag`` after exhausting the script (the competent-agent
            ending). When False the model never submits — it keeps
            re-emitting the script's last command (the naive looping
            behavior the ReAct baseline cannot escape from).
    """

    def __init__(self, script: Sequence[str], flag: str, *, submit: bool = True) -> None:
        self._script = list(script)
        self._flag = flag
        self._submit = submit
        self._turn = 0
        #: Every prompt the model was called with (recorded for tests).
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        """One terminal-protocol completion for ``prompt``."""
        self.calls.append(prompt)
        self._turn += 1
        url = self._live_url(prompt)
        if self._turn > len(self._script):
            # Script exhausted: a competent model submits; a naive one
            # keeps re-probing the last command (bounded by the loop's
            # max_turns — never infinite).
            if self._submit:
                return f"The flag is in hand.\nACTION: submit\nPAYLOAD: {self._flag}"
            command = self._script[-1].replace("{url}", url)
            return f"Keep probing the surface.\nACTION: run\nPAYLOAD: {command}"
        command = self._script[self._turn - 1].replace("{url}", url)
        return f"Probe the surface.\nACTION: run\nPAYLOAD: {command}"

    def _live_url(self, prompt: str) -> str:
        """The live target URL embedded in ``prompt`` (lab ephemeral port)."""
        match = _URL_RE.search(prompt)
        if match is None:  # pragma: no cover - every benchmark prompt embeds the URL
            raise RuntimeError("scripted model could not locate the target URL in the prompt")
        return match.group(0)


class ScriptedModelService:
    """The :class:`~ozzgraph.model_client.ModelService`-like form.

    Wraps a :class:`ScriptedModel` (or any ``prompt -> completion``
    callable) behind the service contract the
    :class:`~ozzgraph.runner.AutonomousRunner` consumes: an async
    ``complete(ModelRequest)`` returning a :class:`ModelResponse` whose
    content is the scripted completion, plus a no-op ``aclose``. The
    runner never owns this service (it is injected), so nothing is
    closed or leaked.
    """

    def __init__(self, model: ScriptedModel, *, model_id: str = "scripted-benchmark") -> None:
        self._model = model
        self._model_id = model_id
        #: Completed requests (recorded for tests).
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """One scripted completion for ``request``."""
        self.requests.append(request)
        content = await self._model(request.messages[-1].content or "")
        return ModelResponse(
            id=f"bench-{len(self.requests)}",
            model=self._model_id,
            choices=[
                ModelChoice(message=ModelMessage(role="assistant", content=content)),
            ],
            usage=ModelUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            created=1,
        )

    async def aclose(self) -> None:
        """No owned resources; idempotent no-op."""
