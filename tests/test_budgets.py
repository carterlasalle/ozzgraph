"""Unit tests for the budget tracker (PR3)."""

import pytest

from ozzgraph.budgets import BudgetExceeded, BudgetKind, Budgets


def _budgets(**overrides) -> Budgets:
    base = {
        "max_tokens": 100,
        "max_model_calls": 10,
        "max_tool_calls": 20,
        "max_workers": 2,
        "max_hints": 1,
        "max_runtime_s": 100.0,
    }
    base.update(overrides)
    return Budgets(**base)


def test_cumulative_consume_tracks_usage() -> None:
    b = _budgets()
    assert b.tokens_used() == 0
    b.consume_tokens(30)
    b.consume_tokens(20)
    assert b.tokens_used() == 50
    assert b.remaining_tokens() == 50


def test_consume_tokens_exceeding_raises() -> None:
    b = _budgets()
    b.consume_tokens(90)
    with pytest.raises(BudgetExceeded) as exc:
        b.consume_tokens(20)
    assert exc.value.kind == BudgetKind.TOKENS
    assert exc.value.limit == 100
    assert exc.value.used == 110


def test_consume_negative_tokens_raises_valueerror() -> None:
    b = _budgets()
    with pytest.raises(ValueError):
        b.consume_tokens(-1)


def test_model_call_and_tool_call_budgets() -> None:
    b = _budgets(max_model_calls=2, max_tool_calls=1)
    b.consume_model_call()
    b.consume_model_call()
    assert b.model_calls_used() == 2
    with pytest.raises(BudgetExceeded):
        b.consume_model_call()
    b.consume_tool_call()
    with pytest.raises(BudgetExceeded):
        b.consume_tool_call()


def test_unlimited_cumulative_budget_never_exhausts() -> None:
    b = _budgets(max_tokens=0, max_model_calls=0, max_tool_calls=0)
    assert b.remaining_tokens() is None
    b.consume_tokens(10_000)
    b.consume_model_call()
    b.consume_tool_call()
    assert not b.is_exhausted()


def test_hint_invariant_enforced() -> None:
    """Paid hint count never exceeds the configured maximum (AGENTS.md)."""
    b = _budgets(max_hints=1)
    b.consume_hint()
    assert b.hints_used() == 1
    with pytest.raises(BudgetExceeded) as exc:
        b.consume_hint()
    assert exc.value.kind == BudgetKind.HINTS


def test_worker_concurrency_bounds() -> None:
    b = _budgets(max_workers=2)
    b.acquire_worker()
    b.acquire_worker()
    with pytest.raises(BudgetExceeded):
        b.acquire_worker()
    b.release_worker()
    b.acquire_worker()  # a slot freed up


def test_release_without_acquire_raises() -> None:
    b = _budgets()
    with pytest.raises(RuntimeError):
        b.release_worker()


def test_runtime_budget_exhaustion_detects_time() -> None:
    # Deterministic clock: returns a fixed time until manually advanced.
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    b = _budgets(max_runtime_s=100.0, clock=clock)
    assert not b.is_exhausted()
    now["t"] += 200.0
    assert b.is_exhausted()
    assert b.is_runtime_exhausted()


def test_cumulative_exhaustion_flips_is_exhausted() -> None:
    b = _budgets(max_tokens=5)
    assert not b.is_exhausted()
    b.consume_tokens(5)
    assert b.is_exhausted()


def test_full_worker_pool_is_not_cumulative_exhaustion() -> None:
    b = _budgets(max_workers=1)
    b.acquire_worker()
    assert not b.is_exhausted()  # concurrency full != run exhausted


def test_constructor_validates_limits() -> None:
    with pytest.raises(ValueError):
        _budgets(max_workers=0)
    with pytest.raises(ValueError):
        _budgets(max_hints=0)
    with pytest.raises(ValueError):
        _budgets(max_runtime_s=0)
    with pytest.raises(ValueError):
        _budgets(max_tokens=-1)


def test_trackers_are_independent() -> None:
    """No hidden global mutable state: two trackers don't share counters."""
    a = _budgets()
    b = _budgets()
    a.consume_tokens(60)
    assert a.tokens_used() == 60
    assert b.tokens_used() == 0
