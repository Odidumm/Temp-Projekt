"""Hysterese, wenn der Pi selbst entscheidet: Alarm ab threshold_c bis unter clear_c.
Warnung nur, wenn der Server warn_c mitschickt."""

from __future__ import annotations

import pytest

from temp_monitor.controller import classify
from temp_monitor.state import Thresholds

from .conftest import LIMITS, LIMITS_WITH_WARN  # Alarm 30 (Ende < 28), Warnung 27 (Ende < 25)


@pytest.mark.parametrize("temp", [-40.0, 20.0, 30.0, 60.0, 125.0])
@pytest.mark.parametrize("current", ["ok", "warn", "alarm"])
def test_without_any_limits_leds_stay_off(temp, current):
    assert classify(temp, current, None) == "ok"


@pytest.mark.parametrize(
    "temp, current, expected",
    [
        (25.0, "ok", "ok"),
        (29.99, "ok", "ok"),  # Warngrenze unbekannt -> keine Warnung
        (30.0, "ok", "alarm"),  # Beispiel aus dem Vertrag: 30.0 -> on
        (28.0, "alarm", "alarm"),  # 28.0 -> bleibt on
        (27.9, "alarm", "ok"),  # 27.9 -> off
        (29.0, "alarm", "alarm"),
        (29.0, "ok", "ok"),
        (29.0, "warn", "ok"),  # gelb vom Server, jetzt ohne Server: aus
        (30.0, "warn", "alarm"),
    ],
)
def test_contract_limits_only_know_alarm(temp, current, expected):
    assert classify(temp, current, LIMITS) == expected


def test_warn_clear_uses_same_gap_as_alarm():
    assert LIMITS_WITH_WARN.warn_clear_c == 25.0
    assert LIMITS.warn_clear_c is None
    # Rundung wie am Server, sonst käme 29.199999999999996 heraus
    assert Thresholds(32.2, 30.2, 31.2).warn_clear_c == 29.2


@pytest.mark.parametrize(
    "temp, current, expected",
    [
        # von ok aus
        (24.0, "ok", "ok"),
        (26.99, "ok", "ok"),
        (27.0, "ok", "warn"),  # genau auf der Warngrenze
        (29.9, "ok", "warn"),
        (30.0, "ok", "alarm"),  # genau auf der Alarmgrenze
        # von warn aus
        (25.0, "warn", "warn"),  # genau auf dem Warn-Ende: bleibt
        (24.99, "warn", "ok"),
        (29.9, "warn", "warn"),
        (30.0, "warn", "alarm"),
        # von alarm aus
        (29.0, "alarm", "alarm"),
        (28.0, "alarm", "alarm"),  # genau auf dem Alarm-Ende: bleibt
        (27.99, "alarm", "warn"),  # aus dem Alarm heraus -> Warnung
        (25.5, "alarm", "warn"),  # unter warn_c, aber über Warn-Ende
        (24.9, "alarm", "ok"),
    ],
)
def test_classify_with_warn_c(temp, current, expected):
    assert classify(temp, current, LIMITS_WITH_WARN) == expected


def test_sequence_does_not_flicker():
    temps = [24, 26, 27, 26, 29.9, 30, 29, 28, 27.9, 27, 25, 24.9, 26.9, 27]
    expected = [
        "ok", "ok", "warn", "warn", "warn", "alarm", "alarm", "alarm",
        "warn", "warn", "warn", "ok", "ok", "warn",
    ]  # fmt: skip
    level, seen = "ok", []
    for t in temps:
        level = classify(t, level, LIMITS_WITH_WARN)
        seen.append(level)
    assert seen == expected


def test_warn_equal_to_alarm_disables_warning():
    limits = Thresholds(threshold_c=30.0, clear_c=28.0, warn_c=30.0)
    assert classify(29.9, "ok", limits) == "ok"
    assert classify(30.0, "ok", limits) == "alarm"
    assert classify(27.9, "alarm", limits) == "ok"  # Warn-Ende = 28


def test_without_gap_behaves_like_simple_thresholds():
    limits = Thresholds(threshold_c=30.0, clear_c=30.0, warn_c=27.0)
    assert classify(29.99, "alarm", limits) == "warn"
    assert classify(26.99, "warn", limits) == "ok"
