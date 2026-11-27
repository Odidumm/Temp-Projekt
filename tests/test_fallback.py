"""Keine gültige Antwort vom Server -> der Pi entscheidet selbst. Dazu Sensorausfall."""

from __future__ import annotations

import threading

import pytest

from temp_monitor.sensor import SensorError
from temp_monitor.state import SavedState, StateStore

from .conftest import LIMITS, LIMITS_WITH_WARN


def _run(controller, sensor, temp):
    sensor.set(temp)
    controller.run_once()


@pytest.mark.parametrize("temp", [20.0, 30.0, 60.0])
def test_leds_stay_off_before_first_reply(unreachable_url, make_controller, leds, sensor, temp):
    """Es gibt keine fest eingebauten Grenzen: ohne Antwort vom Server kein Alarm."""
    controller = make_controller(unreachable_url)
    _run(controller, sensor, temp)
    assert controller.thresholds is None
    assert controller.server_reachable is False
    assert leds() == ("off", "off")


def test_uses_last_known_server_thresholds(server, make_controller, leds, sensor):
    controller = make_controller(server.url)
    server.set_reply("ok")  # Alarm 30, Ende unter 28
    controller.run_once()

    server.set_error(503)
    steps = [(29.9, "off"), (30.0, "on"), (29.0, "on"), (28.0, "on"), (27.9, "off"), (29.0, "off")]
    for temp, expected in steps:
        _run(controller, sensor, temp)
        assert leds() == (expected, "off"), f"bei {temp} °C"
    assert controller.server_reachable is False


def test_changed_limits_from_dashboard_are_used_offline(server, make_controller, leds, sensor):
    controller = make_controller(server.url)
    controller.run_once()
    server.set_reply("ok", threshold_c=24.0, clear_c=22.0)  # im Dashboard geändert
    controller.run_once()
    server.set_error(500)
    _run(controller, sensor, 24.0)
    assert leds() == ("on", "off")


def test_thresholds_from_before_restart_are_used(make_controller, unreachable_url, state_path, leds, sensor):
    StateStore(state_path).save(SavedState(LIMITS))
    controller = make_controller(unreachable_url)
    _run(controller, sensor, 31.0)
    assert leds() == ("on", "off")


def test_alarm_from_server_continues_until_clear_c(server, make_controller, leds, sensor):
    controller = make_controller(server.url)
    server.set_reply("alarm")
    _run(controller, sensor, 31.0)
    assert leds() == ("on", "off")

    server.stop()  # Server weg
    _run(controller, sensor, 29.0)
    assert leds() == ("on", "off")
    _run(controller, sensor, 27.9)
    assert leds() == ("off", "off")
    server.start()  # damit das Fixture sauber aufräumen kann


def test_yellow_is_off_without_server(server, make_controller, leds, sensor):
    """Der Server schickt keine Warngrenze, also kann der Pi Gelb nicht selbst entscheiden."""
    controller = make_controller(server.url)
    server.set_reply("warn")
    _run(controller, sensor, 28.0)
    assert leds() == ("off", "on")
    server.set_error(500)
    _run(controller, sensor, 28.0)
    assert leds() == ("off", "off")
    server.set_reply("warn")  # Server wieder da
    _run(controller, sensor, 28.0)
    assert leds() == ("off", "on")


def test_yellow_works_without_server_if_warn_c_is_known(server, make_controller, leds, sensor):
    controller = make_controller(server.url)
    server.set_reply("ok", warn_c=27.0)
    controller.run_once()
    server.set_error(500)
    steps = [(27.0, ("off", "on")), (30.0, ("on", "off")), (27.9, ("off", "on")), (24.9, ("off", "off"))]
    for temp, expected in steps:
        _run(controller, sensor, temp)
        assert leds() == expected, f"bei {temp} °C"


def test_server_takes_over_again_after_outage(server, make_controller, leds, sensor):
    controller = make_controller(server.url)
    controller.run_once()
    server.set_error(500)
    _run(controller, sensor, 31.0)
    assert leds() == ("on", "off")  # Pi hat Alarm gegeben

    server.set_reply("ok")  # Server wieder da und sagt "off"
    controller.run_once()
    assert controller.server_reachable is True
    assert leds() == ("off", "off")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 530])
def test_pi_decides_on_every_error(server, make_controller, leds, sensor, status):
    """Auch bei 400 und 401 entscheidet der Pi selbst, der Alarm fällt nicht aus."""
    controller = make_controller(server.url)
    controller.run_once()
    last_push = controller.last_push
    server.set_error(status)
    _run(controller, sensor, 31.0)
    assert controller.server_reachable is False
    assert controller.last_push == last_push
    assert leds() == ("on", "off")


@pytest.mark.parametrize("bad_reply", ["kein json", {"status": "ok"}])
def test_pi_decides_on_invalid_reply(server, make_controller, leds, sensor, bad_reply):
    controller = make_controller(server.url)
    controller.run_once()
    server.reply = bad_reply
    _run(controller, sensor, 31.0)
    assert controller.server_reachable is False
    assert leds() == ("on", "off")


def test_timeout_does_not_crash(server, make_controller, leds, sensor):
    controller = make_controller(server.url, timeout_s=0.2)
    controller.run_once()
    server.delay_s = 0.6
    _run(controller, sensor, 31.0)
    assert controller.server_reachable is False
    assert leds() == ("on", "off")


def test_invalid_reply_does_not_overwrite_thresholds(server, make_controller, state_path):
    controller = make_controller(server.url)
    server.set_reply("ok")
    controller.run_once()
    server.set_reply("ok", threshold_c=10.0, clear_c=50.0)
    controller.run_once()
    assert controller.thresholds == LIMITS
    assert StateStore(state_path).load().thresholds == LIMITS


@pytest.mark.parametrize(
    "limits, level, temp, expected",
    [
        (LIMITS, "alarm", 29.0, ("on", "off")),
        (LIMITS_WITH_WARN, "warn", 26.0, ("off", "on")),
    ],
)
def test_level_survives_restart_inside_hysteresis_band(
    make_controller, unreachable_url, state_path, red, yellow, leds, sensor, limits, level, temp, expected
):
    """Alarm/Warnung, Server weg, Pi startet neu im Hysteresebereich: LED bleibt an."""
    StateStore(state_path).save(SavedState(limits, level))
    red.set("off")
    yellow.set("off")  # nach dem Neustart ist der GPIO erst mal aus
    restarted = make_controller(unreachable_url)
    assert leds() == expected  # gleich beim Start wiederhergestellt
    _run(restarted, sensor, temp)
    assert leds() == expected
    _run(restarted, sensor, 24.0)
    assert leds() == ("off", "off")


def test_same_rounded_value_is_sent_and_evaluated(server, make_controller, leds, sensor):
    """29,996 °C geht als 30,0 an den Server. Dann muss auch der Pi 30,0 bewerten."""
    controller = make_controller(server.url)
    controller.run_once()
    server.set_error(500)
    _run(controller, sensor, 29.996)
    assert server.sent_temps[-1] == 30.0
    assert leds() == ("on", "off")


def test_change_of_control_is_logged_once(server, make_controller, caplog):
    controller = make_controller(server.url)
    controller.run_once()
    server.set_error(500)
    controller.run_once()
    controller.run_once()
    assert sum("Pi entscheidet selbst" in r.getMessage() for r in caplog.records) == 1


# --------------------------------------------------------------- Sensorausfall


def test_sensor_failure_blinks_yellow_and_keeps_red(server, make_controller, leds, sensor):
    controller = make_controller(server.url, fail_limit=3)
    server.set_reply("alarm")
    controller.run_once()
    assert leds() == ("on", "off")

    sensor.set(SensorError("CRC-Fehler"))
    controller.run_once()
    controller.run_once()
    assert leds() == ("on", "off")  # 2 Fehler: noch kein Ausfall
    controller.run_once()
    assert leds() == ("on", "blink")  # Alarm bleibt, gelb blinkt
    assert controller.health()["sensor_ok"] is False
    assert len(server.requests) == 1  # in der Zeit nichts gesendet

    sensor.set(25.0)
    server.set_reply("ok")
    controller.run_once()
    assert leds() == ("off", "off")
    assert controller.health()["sensor_ok"] is True


def test_sensor_failure_during_warning(server, make_controller, leds, sensor):
    controller = make_controller(server.url, fail_limit=1)
    server.set_reply("warn")
    controller.run_once()
    sensor.set(SensorError("weg"))
    controller.run_once()
    assert leds() == ("off", "blink")


def test_unexpected_sensor_exception_does_not_crash(unreachable_url, make_controller, leds, sensor):
    controller = make_controller(unreachable_url, fail_limit=1)
    sensor.set(RuntimeError("Treiber kaputt"))
    controller.run_once()
    assert leds() == ("off", "blink")


def test_unexpected_push_exception_does_not_crash(server, make_controller, leds, sensor):
    controller = make_controller(server.url)
    controller.run_once()

    def broken(*_args):
        raise RuntimeError("unerwartet")

    controller.client.push = broken
    _run(controller, sensor, 31.0)
    assert leds() == ("on", "off")
    assert len(controller.buffer) == 0


def test_led_error_does_not_crash(server, make_controller, red, yellow, leds):
    def broken(_state):
        raise OSError("GPIO weg")

    red.set = broken
    controller = make_controller(server.url)
    server.set_reply("warn")
    controller.run_once()  # rote LED wirft, gelbe wird trotzdem geschaltet
    assert yellow.state == "on"


def test_run_forever_survives_errors_and_stops(make_controller, unreachable_url):
    controller = make_controller(unreachable_url)
    stop = threading.Event()
    calls = []

    def boom():
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
        raise RuntimeError("unerwartet")

    controller.run_once = boom
    controller.run_forever(stop, interval_s=0.01)
    assert len(calls) == 3
