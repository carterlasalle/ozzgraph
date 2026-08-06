"""Budget tracking for OzzGraph (PR3).

Tracks the cumulative and concurrency budgets the supervisor enforces: time,
tokens, model calls, tool calls, workers (concurrency), and paid hints. The
tracker is deterministic and holds no hidden global mutable state — every
``Budgets`` instance is independent and threaded through callers explicitly.

The paid-hint invariant from AGENTS.md ("Paid hint count never exceeds the
configured maximum") is enforced by :meth:`Budgets.consume_hint`, which raises
:class:`BudgetExceeded` rather than silently over-spending.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class BudgetKind(str, Enum):
    """The dimension a budget constrains."""

    RUNTIME = "runtime"
    TOKENS = "tokens"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    WORKERS = "workers"
    HINTS = "hints"


class BudgetExceeded(RuntimeError):
    """Raised when consuming past a configured budget.

    Attributes:
        kind: Which budget was exceeded.
        limit: The configured upper bound.
        used: The resulting usage that violated the limit.
    """

    def __init__(self, kind: BudgetKind, limit: float, used: float) -> None:
        super().__init__(f"{kind.value} budget exceeded: {used} > limit {limit}")
        self.kind = kind
        self.limit = limit
        self.used = used


class Budgets:
    """Deterministic runtime budget tracker.

    Args:
        max_tokens: Cumulative token cap; ``0`` = unlimited.
        max_model_calls: Cumulative model-call cap; ``0`` = unlimited.
        max_tool_calls: Cumulative tool-call cap; ``0`` = unlimited.
        max_workers: Maximum concurrent workers (must be >= 1).
        max_hints: Maximum paid hints (must be >= 1).
        max_runtime_s: Wall-clock runtime cap in seconds (must be > 0).
        clock: Monotonic clock; overridable for deterministic tests.
    """

    def __init__(
        self,
        *,
        max_tokens: int,
        max_model_calls: int,
        max_tool_calls: int,
        max_workers: int,
        max_hints: int,
        max_runtime_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_hints < 1:
            raise ValueError("max_hints must be >= 1")
        if max_runtime_s <= 0:
            raise ValueError("max_runtime_s must be > 0")
        if max_tokens < 0 or max_model_calls < 0 or max_tool_calls < 0:
            raise ValueError("cumulative budgets must be >= 0")

        self._max_tokens = max_tokens
        self._max_model_calls = max_model_calls
        self._max_tool_calls = max_tool_calls
        self._max_workers = max_workers
        self._max_hints = max_hints
        self._max_runtime_s = float(max_runtime_s)
        self._clock = clock

        self._tokens_used = 0
        self._model_calls_used = 0
        self._tool_calls_used = 0
        self._active_workers = 0
        self._hints_used = 0
        self._started_at = clock()

    def elapsed(self) -> float:
        """Seconds since this tracker was constructed."""
        return self._clock() - self._started_at

    def remaining_runtime(self) -> float:
        """Seconds of runtime budget remaining (floored at 0)."""
        return max(0.0, self._max_runtime_s - self.elapsed())

    def tokens_used(self) -> int:
        return self._tokens_used

    def model_calls_used(self) -> int:
        return self._model_calls_used

    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    def hints_used(self) -> int:
        return self._hints_used

    def active_workers(self) -> int:
        return self._active_workers

    def remaining_tokens(self) -> int | None:
        """Remaining token budget, or ``None`` when unbounded."""
        if self._max_tokens == 0:
            return None
        return max(0, self._max_tokens - self._tokens_used)

    def remaining_model_calls(self) -> int | None:
        """Remaining model-call budget, or ``None`` when unbounded."""
        if self._max_model_calls == 0:
            return None
        return max(0, self._max_model_calls - self._model_calls_used)

    def remaining_tool_calls(self) -> int | None:
        """Remaining tool-call budget, or ``None`` when unbounded."""
        if self._max_tool_calls == 0:
            return None
        return max(0, self._max_tool_calls - self._tool_calls_used)

    def consume_tokens(self, amount: int) -> None:
        """Consume ``amount`` tokens from the budget.

        Raises:
            ValueError: If ``amount`` is negative.
            BudgetExceeded: If the cumulative token budget would be exceeded.
        """
        if amount < 0:
            raise ValueError("cannot consume a negative token amount")
        new_used = self._tokens_used + amount
        if self._max_tokens != 0 and new_used > self._max_tokens:
            raise BudgetExceeded(BudgetKind.TOKENS, self._max_tokens, new_used)
        self._tokens_used = new_used

    def consume_model_call(self) -> None:
        """Count one model call against the budget."""
        new_used = self._model_calls_used + 1
        if self._max_model_calls != 0 and new_used > self._max_model_calls:
            raise BudgetExceeded(BudgetKind.MODEL_CALLS, self._max_model_calls, new_used)
        self._model_calls_used = new_used

    def consume_tool_call(self) -> None:
        """Count one tool call against the budget."""
        new_used = self._tool_calls_used + 1
        if self._max_tool_calls != 0 and new_used > self._max_tool_calls:
            raise BudgetExceeded(BudgetKind.TOOL_CALLS, self._max_tool_calls, new_used)
        self._tool_calls_used = new_used

    def consume_hint(self) -> None:
        """Purchase one paid hint, enforcing the max-hint invariant.

        Raises:
            BudgetExceeded: If this would push paid hints past ``max_hints``.
        """
        new_used = self._hints_used + 1
        if new_used > self._max_hints:
            raise BudgetExceeded(BudgetKind.HINTS, self._max_hints, new_used)
        self._hints_used = new_used

    def acquire_worker(self) -> None:
        """Acquire a worker slot (bounded concurrency).

        Raises:
            BudgetExceeded: If all worker slots are already taken.
        """
        if self._active_workers >= self._max_workers:
            raise BudgetExceeded(BudgetKind.WORKERS, self._max_workers, self._active_workers)
        self._active_workers += 1

    def release_worker(self) -> None:
        """Release a previously acquired worker slot."""
        if self._active_workers <= 0:
            raise RuntimeError("worker released without an active acquisition")
        self._active_workers -= 1

    def is_runtime_exhausted(self) -> bool:
        """True when the wall-clock runtime budget is spent."""
        return self.remaining_runtime() <= 0.0

    def _cumulative_exhausted(self, used: int, limit: int) -> bool:
        """A bounded cumulative budget (limit > 0) is exhausted at its cap."""
        return limit != 0 and used >= limit

    def is_exhausted(self) -> bool:
        """True when any bounded cumulative budget or runtime is exhausted.

        Worker slots are a concurrency limit, not a cumulative budget, so a
        full worker pool does not count as "exhausted" here.
        """
        return (
            self.is_runtime_exhausted()
            or self._cumulative_exhausted(self._tokens_used, self._max_tokens)
            or self._cumulative_exhausted(self._model_calls_used, self._max_model_calls)
            or self._cumulative_exhausted(self._tool_calls_used, self._max_tool_calls)
            or self._hints_used >= self._max_hints
        )
