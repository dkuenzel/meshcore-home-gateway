"""The gateway's own health (health.py, ARC-012): caught bugs counted and their tracebacks thinned
out (CODE-008), the event loop's lateness (CODE-001)."""

from __future__ import annotations

import importlib
import logging

import pytest

from meshcore_home_gateway import health
from meshcore_home_gateway.health import Failures, LoopLag, MinuteCounts


def test_minute_counts_keep_the_last_hour() -> None:
    counts = MinuteCounts()
    for at in (0.0, 30.0, 59.9, 60.0, 3000.0):
        counts.add(at)
    assert counts.total(3000.0) == 5
    assert counts.total(3600.0) == 2  # the first minute is over an hour ago
    assert counts.total(3660.0) == 1
    assert counts.total(7200.0) == 0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def bug() -> None:
    raise RuntimeError("a bug")


def failing(logger: logging.Logger, message: str = "listener %s failed", arg: str = "x") -> None:
    try:
        bug()
    except RuntimeError:
        logger.exception(message, arg)


def test_a_bug_is_counted_and_its_traceback_logged_once_a_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per logger and message: the first goes through, those within a minute are held back and
    counted, and the next one says how many there were. Others are not held by it."""
    clock, logger = Clock(), logging.getLogger("meshcore_home_gateway.test_health")
    failures = Failures(clock)
    logger.addFilter(failures)
    caplog.set_level(logging.DEBUG, logger.name)
    try:
        failing(logger)
        clock.now += 30
        failing(logger, arg="y")  # the same place: held back
        failing(logger, "another %s failed")  # another place
        clock.now += 30
        failing(logger, arg="z")  # a minute after the first
        logger.warning("not a bug")
        logger.error("no traceback")
    finally:
        logger.removeFilter(failures)
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        "listener x failed",
        "another x failed",
        "listener z failed (1 more since)",
        "not a bug",
        "no traceback",
    ]
    assert all(r.exc_info for r in caplog.records[:3])
    assert failures.last_hour() == 4
    clock.now += 3600
    assert failures.last_hour() == 0


def test_the_service_loggers_report_to_the_one_count() -> None:
    """The parts that catch their own bugs; a count of their own: count()."""
    for name in (
        "bridge",
        "observer",
        "telemetry_poller",
        "contact_manager",
        "radio.session",
        "history",
    ):
        module = importlib.import_module(f"meshcore_home_gateway.{name}")
        assert health.FAILURES in module.log.filters
    health.FAILURES.count()
    assert health.FAILURES.last_hour() == 1


def test_loop_lag_keeps_the_largest_of_the_hour_and_warns_once_a_minute() -> None:
    lag = LoopLag()
    assert lag.worst() is None
    assert not lag.record(0.002, 0.0)
    assert lag.record(health.LAG_WARN_S, 10.0)  # from the threshold on
    assert not lag.record(5.0, 20.0)  # within a minute of the warning
    assert lag.worst() == 5.0
    assert lag.record(2.0, 80.0)
    assert not lag.record(0.01, 3700.0)  # the minutes of the first measurements are over
    assert lag.worst() == 0.01
