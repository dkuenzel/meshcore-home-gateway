"""Airtime budget (token bucket over seconds of transmission) and deduplication."""

import math

import pytest

from meshcore_home_gateway.core.budget import AirtimeBudget, Deduplicator


def test_budget_starts_full_and_consumes() -> None:
    budget = AirtimeBudget(per_hour_s=36.0, burst_s=10.0, now=0.0)
    assert budget.wait_s(4.0, now=0.0) == 0.0
    budget.consume(4.0, now=0.0)
    assert budget.available_s(now=0.0) == pytest.approx(6.0)


def test_budget_refills_at_the_hourly_rate() -> None:
    budget = AirtimeBudget(per_hour_s=36.0, burst_s=10.0, now=0.0)  # 0.01 s per second
    budget.consume(10.0, now=0.0)
    assert budget.available_s(now=100.0) == pytest.approx(1.0)
    assert budget.available_s(now=10_000.0) == pytest.approx(10.0)  # capped at the burst


def test_wait_time_until_enough_airtime_is_available() -> None:
    budget = AirtimeBudget(per_hour_s=36.0, burst_s=10.0, now=0.0)
    budget.consume(10.0, now=0.0)
    assert budget.wait_s(2.0, now=0.0) == pytest.approx(200.0)
    assert budget.wait_s(2.0, now=150.0) == pytest.approx(50.0)


def test_message_larger_than_the_burst_can_never_be_sent() -> None:
    budget = AirtimeBudget(per_hour_s=36.0, burst_s=10.0, now=0.0)
    assert math.isinf(budget.wait_s(10.5, now=0.0))


def test_consuming_more_than_available_is_a_programming_error() -> None:
    budget = AirtimeBudget(per_hour_s=36.0, burst_s=10.0, now=0.0)
    with pytest.raises(ValueError, match="available"):
        budget.consume(11.0, now=0.0)


def test_sustained_use_cannot_exceed_the_hourly_budget() -> None:
    """Independent check: greedy sending for 10 hours stays within burst + rate * time."""
    budget = AirtimeBudget(per_hour_s=36.0, burst_s=10.0, now=0.0)
    sent, t = 0.0, 0.0
    while t < 36_000.0:
        wait = budget.wait_s(0.8, now=t)
        t += wait
        budget.consume(0.8, now=t)
        sent += 0.8
    assert sent <= 10.0 + 36.0 * t / 3600.0 + 1e-6
    assert sent >= 36.0 * 10 - 1.0


def test_used_in_the_last_hour_is_a_sliding_window() -> None:
    budget = AirtimeBudget(per_hour_s=3600.0, burst_s=100.0, now=0.0)
    budget.consume(5.0, now=0.0)
    budget.consume(3.0, now=1800.0)
    assert budget.used_s(now=1800.0) == pytest.approx(8.0)
    assert budget.used_s(now=3600.5) == pytest.approx(3.0)
    assert budget.used_s(now=5401.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("per_hour", "burst"), [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0), (3601.0, 1.0)]
)
def test_budget_rejects_impossible_settings(per_hour: float, burst: float) -> None:
    with pytest.raises(ValueError, match="budget"):
        AirtimeBudget(per_hour_s=per_hour, burst_s=burst, now=0.0)


def test_duplicate_within_window_is_detected_without_extending_it() -> None:
    dedup = Deduplicator(window_s=300.0)
    key = ("alerts", "Gate open")
    assert not dedup.is_duplicate(key, now=0.0)
    assert dedup.is_duplicate(key, now=100.0)
    assert dedup.is_duplicate(key, now=299.0)
    assert not dedup.is_duplicate(key, now=300.0)  # a reminder per window, not suppression forever
    assert dedup.is_duplicate(key, now=301.0)


def test_different_targets_or_texts_are_not_duplicates() -> None:
    dedup = Deduplicator(window_s=300.0)
    assert not dedup.is_duplicate(("alerts", "a"), now=0.0)
    assert not dedup.is_duplicate(("alerts", "b"), now=1.0)
    assert not dedup.is_duplicate(("ops", "a"), now=2.0)


def test_zero_window_disables_deduplication() -> None:
    dedup = Deduplicator(window_s=0.0)
    assert not dedup.is_duplicate(("t", "x"), now=0.0)
    assert not dedup.is_duplicate(("t", "x"), now=0.0)


def test_old_entries_are_pruned() -> None:
    dedup = Deduplicator(window_s=10.0)
    for i in range(1000):
        dedup.is_duplicate(("t", str(i)), now=float(i))
    assert len(dedup) <= 11


def test_forgotten_key_is_no_longer_a_duplicate() -> None:
    """A request rejected after the check (queue full) must not block its own retry."""
    dedup = Deduplicator(300.0)
    assert not dedup.is_duplicate(("alerts", "Smoke"), now=0.0)
    dedup.forget(("alerts", "Smoke"))
    assert not dedup.is_duplicate(("alerts", "Smoke"), now=1.0)
    dedup.forget(("never", "seen"))  # no error


def test_the_hourly_budget_reads_back_as_configured() -> None:
    assert AirtimeBudget(per_hour_s=7.1, burst_s=10.0, now=0.0).per_hour_s == 7.1
