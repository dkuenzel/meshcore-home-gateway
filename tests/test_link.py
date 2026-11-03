"""Whether another client took the radio's TCP link (SEC-003, IMP-041)."""

from __future__ import annotations

import pytest

from meshcore_home_gateway.core.link import CONTESTED_HOLD_S, UPTIME_SLACK_S, LinkWatch

T = 1_800_000_000.0


def closed(uptime_s: int = 3600, read_at: float = T, *, by_radio: bool = True) -> LinkWatch:
    watch = LinkWatch()
    watch.started(uptime_s - 600, read_at - 600)  # the link before, from its start
    watch.ended(uptime_s, read_at, by_radio=by_radio)
    return watch


def test_a_radio_that_closed_the_link_and_kept_running_was_taken_over() -> None:
    """Its uptime went on by about the time between the reads: it did not restart."""
    watch = closed()
    assert watch.started(3600 + 75, T + 70)  # the reads 70 s apart, the uptime 75 s on
    assert watch.taken_at == T + 70
    assert watch.contested(T + 70) is True


@pytest.mark.parametrize(
    ("uptime_s", "read_at"),
    [
        (12, T + 70),  # restarted: the uptime began anew
        (3600 + 70 - UPTIME_SLACK_S - 1, T + 70),  # less than the time between: a restart
        (3500, T + 70),  # less than before: a restart (or the uptime wrapped after 49.7 days)
    ],
)
def test_a_radio_that_restarted_was_not_taken_over(uptime_s: int, read_at: float) -> None:
    watch = closed()
    assert not watch.started(uptime_s, read_at)
    assert watch.contested(read_at) is False


def test_only_a_link_the_radio_closed_can_have_been_taken() -> None:
    """Unplugged, a broken network, a timeout: the gateway's side ended it."""
    watch = closed(by_radio=False)
    assert not watch.started(3600 + 70, T + 70)
    assert watch.contested(T + 70) is False


def test_each_closed_link_is_judged_once() -> None:
    watch = closed()
    assert watch.started(3600 + 70, T + 70)
    assert not watch.started(3600 + 140, T + 140)  # a second start without an end between


def test_a_takeover_counts_for_an_hour() -> None:
    """One alert covers a fight; a later restart does not end it."""
    watch = closed()
    watch.started(3600 + 70, T + 70)
    assert watch.contested(T + 70 + CONTESTED_HOLD_S - 1) is True
    assert watch.contested(T + 70 + CONTESTED_HOLD_S) is False
    assert watch.contested(T + 69) is False  # the host's clock went back


def test_without_an_uptime_nothing_can_be_told() -> None:
    """Firmware without GET_STATS; before the first link; while the link is down."""
    watch = LinkWatch()
    assert watch.contested(T) is None
    assert not watch.started(None, None)
    assert watch.contested(T) is None
    watch = closed()
    assert watch.contested(T) is None  # the link is down
    watch = LinkWatch()
    watch.ended(None, None, by_radio=True)  # the radio closed a link without statistics
    assert not watch.started(3600, T)
    assert watch.contested(T) is False
