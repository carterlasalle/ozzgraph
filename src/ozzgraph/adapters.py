"""Model adapter interfaces for OzzGraph (PR13).

Defines the ADAPTER INTERFACE layer (docs/ARCHITECTURE.md, "Model
Adapters"; docs/TECHNICAL_REQUIREMENTS.md, "Model Adapter
Requirements"; PRD goal 2): the normalized, protocol-independent
:class:`ParsedAction` parsed out of a model completion, the typed
:class:`AdapterParseError`, and the :class:`ModelAdapter` abstract base
class that every concrete adapter (PR14/15) must implement — protocol,
prompt compiler, parser, repair strategy, and the protocol-specific
limits carried from a :class:`~ozzgraph.profiles.ModelProfile`.

The registry (:data:`ADAPTERS`) is a plain deterministic dict keyed by
protocol family, populated only by explicit :func:`register_adapter`
calls (AGENTS.md rule #10 — not a plugin system). This PR declares the
contract but registers no concrete adapter: the terminal-native and
three-line adapters land in PR14 and the JSON adapter in PR15.

Concrete parsers beyond the plain-text fallback behavior are NOT part
of this PR: :class:`ParsedAction` is the shape concrete adapters must
produce, and parsing itself is each concrete adapter's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from ozzgraph.profiles import FailureBehavior, ModelProfile


class ParsedAction(BaseModel):
    """A normalized, protocol-independent action parsed from a completion.

    The executor (PR20) consumes this shape regardless of which adapter
    produced it. ``kind`` is one of the action kinds the executor
    understands (e.g. ``"run"``, ``"think"``, ``"submit"``, ``"hint"``,
    ``"exit"``); unknown kinds are schema-valid here and rejected by
    executor policy later (fail loudly at the owning layer). ``raw``
    always carries the original completion text so repair and evidence
    handling never lose the source.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    payload: str | None = None
    rationale: str | None = None
    raw: str


class AdapterError(RuntimeError):
    """Base error for the model adapter layer (AGENTS.md rule #9)."""


class AdapterParseError(AdapterError):
    """A completion that could not be parsed into an action.

    Attributes:
        protocol: The adapter protocol that failed to parse (e.g.
            ``"json"``).
        detail: Human-readable parse failure detail.
    """

    def __init__(self, *, protocol: str, detail: str) -> None:
        super().__init__(detail)
        self.protocol = protocol
        self.detail = detail


class AdapterRegistryError(AdapterError):
    """Raised when the adapter registry cannot resolve or accept an adapter."""


class ModelAdapter(ABC):
    """Contract every concrete model adapter (PR14/15) must implement.

    An adapter owns one protocol family: how a prompt is compiled for
    it, how a completion is parsed into a :class:`ParsedAction`, and
    how parse failures are repaired (docs/ARCHITECTURE.md, "Model
    Adapters": "the adapter owns prompt compilation, parsing, repair,
    and protocol-specific limits").

    Concrete subclasses are constructed with the :class:`ModelProfile`
    the model was discovered with. The profile-derived attributes
    (``context_soft_limit``, ``output_token_limit``, ``temperature``,
    ``supported_roles``, ``max_advertised_skills``,
    ``failure_behavior``) read through to the profile and may be
    overridden by a subclass when the protocol imposes stricter limits
    (e.g. a terminal adapter capping output below the profile's token
    limit).
    """

    def __init__(self, profile: ModelProfile) -> None:
        self.profile = profile

    @property
    @abstractmethod
    def protocol(self) -> str:
        """The protocol family name this adapter implements."""

    @abstractmethod
    def compile_prompt(
        self,
        *,
        mission: str,
        graph_summary: str,
        transcript_tail: str,
        skills: Sequence[str],
        output_contract: str,
    ) -> str:
        """Compile the model prompt for this protocol.

        Exact prompt composition is the context compiler's and each
        concrete adapter's job (PR14/16); this contract fixes the input
        shape: the immutable mission, the bounded graph summary, the
        recent transcript tail, the advertised skill summaries, and the
        output contract describing the expected completion format.
        """

    @abstractmethod
    def parse(self, completion: str) -> ParsedAction:
        """Parse one completion into a normalized action.

        Raises:
            AdapterParseError: When the completion does not conform to
                this protocol's format.
        """

    @abstractmethod
    def repair(self, completion: str, error: AdapterParseError) -> str | None:
        """Repair a failed completion, or say it cannot be repaired.

        Returns repaired completion text when the adapter's repair
        strategy (PR15) produced a fix, else None. Never raises.
        """

    @property
    def context_soft_limit(self) -> int:
        """Usable context budget (chars/tokens) for this adapter."""
        return self.profile.context_soft_limit

    @property
    def output_token_limit(self) -> int:
        """Output token cap for this adapter's completions."""
        return self.profile.output_token_limit

    @property
    def temperature(self) -> float | None:
        """Sampling temperature; None means the adapter/model default."""
        return self.profile.temperature

    @property
    def supported_roles(self) -> list[str]:
        """Message roles this protocol can express (e.g. system/user)."""
        return self.profile.supported_roles

    @property
    def max_advertised_skills(self) -> int:
        """Cap on skills advertised to the model in one prompt."""
        return self.profile.max_advertised_skills

    @property
    def failure_behavior(self) -> FailureBehavior:
        """Conservative failure policy (``repair_retry`` | ``abort_turn``)."""
        return self.profile.failure_behavior


#: Deterministic registry: protocol family -> adapter class. Empty at
#: import: concrete adapters register themselves explicitly (PR14/15)
#: via :func:`register_adapter` — no discovery, AGENTS.md rule #10.
ADAPTERS: dict[str, type[ModelAdapter]] = {}


def register_adapter(protocol: str, cls: type[ModelAdapter]) -> None:
    """Register ``cls`` as the adapter for ``protocol``.

    Raises:
        AdapterRegistryError: If ``protocol`` is empty, ``cls`` is not
            a :class:`ModelAdapter` subclass, or an adapter is already
            registered for the protocol (duplicate registration fails
            loudly).
    """
    if not protocol:
        raise AdapterRegistryError(f"protocol must be a non-empty str, got {protocol!r}")
    if not isinstance(cls, type) or not issubclass(cls, ModelAdapter):
        raise AdapterRegistryError(
            f"adapter for protocol {protocol!r} must be a ModelAdapter subclass, got {cls!r}"
        )
    if protocol in ADAPTERS:
        raise AdapterRegistryError(f"an adapter is already registered for protocol {protocol!r}")
    ADAPTERS[protocol] = cls


def adapter_for(protocol: str) -> type[ModelAdapter]:
    """The adapter class registered for ``protocol``.

    Raises:
        AdapterRegistryError: If no adapter is registered for the
            protocol.
    """
    try:
        return ADAPTERS[protocol]
    except KeyError:
        raise AdapterRegistryError(f"no adapter registered for protocol {protocol!r}") from None
