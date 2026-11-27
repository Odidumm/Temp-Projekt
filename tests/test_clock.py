"""Uhrzeit: erst senden, wenn die Uhr per NTP gestellt ist."""

from __future__ import annotations

import subprocess

import pytest

from temp_monitor import timeutil
from temp_monitor.state import SavedState, StateStore

from .conftest import FIXED_TS, LIMITS


def test_nothing_is_sent_or_buffered_before_clock_is_set(server, make_controller, clock):
    clock.synced = False
    controller = make_controller(server.url)
    assert controller.run_once() is False
    controller.run_once()
    assert server.requests == []
    assert len(controller.buffer) == 0  # ts wäre falsch
    assert controller.health()["last_error"] == "Uhr noch nicht per NTP gestellt"


def test_sends_as_soon_as_clock_is_set(server, make_controller, clock):
    clock.synced = False
    controller = make_controller(server.url)
    controller.run_once()
    clock.synced = True
    clock.advance(3600)  # die Uhr springt beim Stellen nach vorne
    assert controller.run_once() is True
    assert [r["body"]["ts"] for r in server.requests] == ["2026-11-22T14:05:54Z"]
    assert controller.health()["last_error"] is None


def test_clock_is_not_checked_again_after_first_sync(server, make_controller, clock):
    """Fällt NTP später aus, sollen die Messwerte weiter ankommen."""
    controller = make_controller(server.url)
    controller.run_once()
    clock.synced = False
    assert controller.run_once() is True
    assert len(server.requests) == 2


def test_pi_decides_until_clock_is_set(server, make_controller, clock, leds, sensor, state_path):
    StateStore(state_path).save(SavedState(LIMITS))
    clock.synced = False
    controller = make_controller(server.url)
    sensor.set(31.0)
    controller.run_once()
    assert leds() == ("on", "off")  # letzte bekannte Grenzen
    assert controller.temperature()["temp_c"] == 31.0


def test_timestamp_is_taken_after_the_clock_check(server, make_controller, clock):
    """Wird die Uhr genau während der Prüfung gestellt, muss ts schon die richtige Zeit sein."""
    controller = make_controller(server.url)

    def check_and_step():
        clock.advance(7200)
        return True

    controller._clock_synced = check_and_step
    controller.run_once()
    assert FIXED_TS.hour == 13
    assert server.requests[0]["body"]["ts"] == "2026-11-22T15:05:54Z"


def test_waiting_for_clock_is_logged_once(server, make_controller, clock, caplog):
    clock.synced = False
    controller = make_controller(server.url)
    controller.run_once()
    controller.run_once()
    assert sum("noch nicht per NTP" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("output, expected", [("yes\n", True), ("no\n", False), ("", False), ("n/a\n", False)])
def test_clock_synchronized_reads_timedatectl(monkeypatch, output, expected):
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr(timeutil.subprocess, "run", fake_run)
    assert timeutil.clock_synchronized() is expected
    assert calls == [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]]


@pytest.mark.parametrize("error", [FileNotFoundError("timedatectl"), subprocess.TimeoutExpired("timedatectl", 5)])
def test_clock_synchronized_survives_missing_or_hanging_timedatectl(monkeypatch, error):
    def fake_run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(timeutil.subprocess, "run", fake_run)
    assert timeutil.clock_synchronized() is False
