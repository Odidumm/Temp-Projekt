"""Puffer: nicht gesendete Werte merken und nach einem Ausfall nachliefern."""

from __future__ import annotations

import threading
import time

import pytest

from temp_monitor.client import ServerClient
from temp_monitor.controller import BUFFER_SIZE, Controller
from temp_monitor.led import FakeLed
from temp_monitor.sensor import FakeSensor, SensorError
from temp_monitor.state import SavedState, StateStore

from .conftest import LIMITS


@pytest.fixture
def measure(sensor, clock):
    """Ein Messzyklus mit ``temp``; die Uhr läuft danach 30 s weiter."""

    def _measure(controller, temp):
        sensor.set(temp)
        ok = controller.run_once()
        clock.advance(30)
        return ok

    return _measure


def test_buffer_lasts_16_hours_at_one_value_per_second():
    assert BUFFER_SIZE >= 16 * 3600


def test_buffer_is_limited_by_default(unreachable_url, state_path):
    controller = Controller(
        sensor=FakeSensor(),
        red=FakeLed(),
        yellow=FakeLed(),
        client=ServerClient(unreachable_url, "t"),
        store=StateStore(state_path),
        sensor_id="rack-a",
    )
    assert controller.buffer.maxlen == BUFFER_SIZE


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503, 530])
def test_values_that_did_not_reach_the_server_are_buffered(server, make_controller, measure, status):
    controller = make_controller(server.url)
    server.set_error(status)
    measure(controller, 21.0)
    measure(controller, 22.0)
    assert [m.temp_c for m in controller.buffer] == [21.0, 22.0]
    assert controller.health()["buffered"] == 2


def test_unreachable_is_buffered(unreachable_url, make_controller, measure):
    controller = make_controller(unreachable_url)
    measure(controller, 21.0)
    assert len(controller.buffer) == 1


def test_timeout_is_buffered(server, make_controller, measure):
    server.delay_s = 0.5
    controller = make_controller(server.url, timeout_s=0.1)
    measure(controller, 21.0)
    assert len(controller.buffer) == 1


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_values_are_not_buffered(server, make_controller, measure, status):
    server.set_error(status)
    controller = make_controller(server.url)
    measure(controller, 21.0)
    assert len(controller.buffer) == 0


def test_invalid_reply_is_not_buffered(server, make_controller, measure):
    """HTTP 200: Der Wert ist vermutlich gespeichert, noch einmal senden gäbe ihn doppelt."""
    server.reply = {"status": "ok"}
    controller = make_controller(server.url)
    measure(controller, 21.0)
    assert len(controller.buffer) == 0


def test_failed_reading_is_not_buffered(unreachable_url, make_controller, measure):
    controller = make_controller(unreachable_url)
    measure(controller, SensorError("CRC-Fehler"))
    assert len(controller.buffer) == 0


def test_oldest_values_fall_out(unreachable_url, make_controller, measure):
    controller = make_controller(unreachable_url, buffer_size=3)
    for temp in (21.0, 22.0, 23.0, 24.0, 25.0):
        measure(controller, temp)
    assert [m.temp_c for m in controller.buffer] == [23.0, 24.0, 25.0]


def test_current_value_first_then_buffer_with_original_timestamps(server, make_controller, measure):
    controller = make_controller(server.url)
    server.set_error(500)
    for temp in (21.0, 22.0, 23.0):
        measure(controller, temp)
    server.requests.clear()

    server.set_reply("ok")
    assert measure(controller, 24.0) is True
    controller.flush_buffer()

    assert [(r["body"]["temp_c"], r["body"]["ts"]) for r in server.requests] == [
        (24.0, "2026-11-22T13:07:24Z"),  # aktueller Wert zuerst
        (21.0, "2026-11-22T13:05:54Z"),
        (22.0, "2026-11-22T13:06:24Z"),
        (23.0, "2026-11-22T13:06:54Z"),
    ]
    assert len(controller.buffer) == 0


def test_every_value_arrives_exactly_once(server, make_controller, measure):
    controller = make_controller(server.url)
    temps = [20.0 + i / 10 for i in range(12)]
    for i, temp in enumerate(temps):
        down = 3 <= i < 9
        server.set_error(503) if down else server.set_reply("ok")
        accepted = len(server.requests)
        if measure(controller, temp):
            controller.flush_buffer()
        if down:
            del server.requests[accepted:]  # vom Server abgelehnt, zählt nicht als angekommen
    assert sorted(server.sent_temps) == temps


def test_replies_to_buffered_values_change_neither_leds_nor_limits(server, make_controller, measure, leds):
    controller = make_controller(server.url)
    server.set_error(500)
    measure(controller, 35.0)

    def reply(body):
        if body["temp_c"] == 35.0:  # Antwort auf den alten Wert
            server.set_reply("alarm", threshold_c=99.0, clear_c=98.0)
        else:
            server.set_reply("ok")

    server.on_request = reply
    measure(controller, 22.0)
    controller.flush_buffer()
    assert server.sent_temps[-1] == 35.0
    assert leds() == ("off", "off")
    assert controller.level == "ok"
    assert controller.thresholds == LIMITS
    assert controller.health()["last_push"] == "2026-11-22T13:06:24Z"  # aktueller Wert, nicht der alte


@pytest.mark.parametrize("status", [403, 429, 503])
def test_flush_stops_when_server_fails_again_and_continues_later(server, make_controller, measure, status):
    """Auch wenn Cloudflare mitten im Nachliefern bremst (429), geht kein Wert verloren."""
    controller = make_controller(server.url)
    server.set_error(500)
    for temp in (21.0, 22.0, 23.0):
        measure(controller, temp)

    server.on_request = lambda body: server.set_error(status) if body["temp_c"] == 22.0 else server.set_reply("ok")
    measure(controller, 24.0)
    controller.flush_buffer()
    assert [m.temp_c for m in controller.buffer] == [22.0, 23.0]  # nichts verloren

    server.on_request = None
    server.set_reply("ok")
    measure(controller, 25.0)
    controller.flush_buffer()
    assert len(controller.buffer) == 0


def test_flush_drops_value_rejected_with_400(server, make_controller, measure):
    controller = make_controller(server.url)
    server.set_error(500)
    for temp in (21.0, 22.0, 23.0):
        measure(controller, temp)
    server.on_request = lambda body: server.set_error(400) if body["temp_c"] == 22.0 else server.set_reply("ok")
    controller.flush_buffer()
    assert server.sent_temps[-3:] == [21.0, 22.0, 23.0]  # ein abgelehnter Wert hält den Rest nicht auf
    assert len(controller.buffer) == 0


def test_flush_keeps_values_on_401(server, make_controller, measure):
    controller = make_controller(server.url)
    server.set_error(500)
    measure(controller, 21.0)
    server.set_error(401)
    controller.flush_buffer()
    assert len(controller.buffer) == 1


def test_flush_unexpected_error_does_not_block_buffer(server, make_controller, measure):
    controller = make_controller(server.url)
    server.set_error(500)
    measure(controller, 21.0)

    def broken(*_args):
        raise RuntimeError("unerwartet")

    controller.client.push = broken
    controller.flush_buffer()
    assert len(controller.buffer) == 0


def test_flush_respects_time_budget(server, make_controller, measure):
    controller = make_controller(server.url)
    server.set_error(500)
    for i in range(20):
        measure(controller, 20.0 + i / 10)
    server.set_reply("ok")
    server.delay_s = 0.05
    start = time.monotonic()
    controller.flush_buffer(budget_s=0.2)
    assert time.monotonic() - start < 0.6
    assert 0 < len(controller.buffer) < 20  # der Rest folgt im nächsten Zyklus


def test_flush_stops_on_shutdown(server, make_controller, measure):
    controller = make_controller(server.url)
    server.set_error(500)
    measure(controller, 21.0)
    server.set_reply("ok")
    stop = threading.Event()
    stop.set()
    controller.flush_buffer(stop)
    assert len(controller.buffer) == 1


def test_flush_without_buffer_sends_nothing(server, make_controller):
    make_controller(server.url).flush_buffer()
    assert server.requests == []


@pytest.mark.parametrize("accepted", [True, False])
def test_run_forever_flushes_only_after_success(make_controller, unreachable_url, accepted):
    controller = make_controller(unreachable_url)
    stop = threading.Event()
    flushed = []

    def once():
        stop.set()
        return accepted

    controller.run_once = once
    controller.flush_buffer = lambda stop_event, budget_s: flushed.append(budget_s)
    controller.run_forever(stop, interval_s=30)
    assert flushed == ([pytest.approx(15.0, abs=0.1)] if accepted else [])


@pytest.mark.parametrize(
    "duration, expected",
    [
        (0.0, 0.5),  # sofort fertig: die Hälfte der restlichen Sekunde
        (0.8, 0.1),  # DS18B20: es bleiben 0,2 s, davon die Hälfte
        (1.2, 0.1),  # Takt überschritten: trotzdem ein kleines Stück, sonst wird der Puffer nie leer
    ],
)
def test_flush_leaves_time_for_the_next_measurement(make_controller, unreachable_url, duration, expected):
    controller = make_controller(unreachable_url)
    stop = threading.Event()
    flushed = []

    def once():
        time.sleep(duration)
        stop.set()
        return True

    controller.run_once = once
    controller.flush_buffer = lambda stop_event, budget_s: flushed.append(budget_s)
    controller.run_forever(stop, interval_s=1.0)
    assert flushed == [pytest.approx(expected, abs=0.02)]


def test_measurement_interval_is_kept(make_controller, unreachable_url):
    """Der Takt zählt von Messbeginn zu Messbeginn, nicht Messdauer plus Pause."""
    controller = make_controller(unreachable_url)
    stop = threading.Event()
    starts = []

    def once():
        starts.append(time.monotonic())
        time.sleep(0.08)  # wie der Sensor: fast der ganze Takt
        if len(starts) == 8:
            stop.set()
        return True

    controller.run_once = once
    controller.run_forever(stop, interval_s=0.1)
    assert starts[-1] - starts[0] == pytest.approx(0.7, abs=0.05)


def test_missed_cycles_are_not_made_up(make_controller, unreachable_url):
    """Nach einem langen Timeout geht es im normalen Takt weiter, ohne Nachhol-Serie."""
    controller = make_controller(unreachable_url)
    stop = threading.Event()
    starts = []

    def once():
        starts.append(time.monotonic())
        if len(starts) == 1:
            time.sleep(0.35)  # wie ein Timeout über mehrere Takte
        if len(starts) == 5:
            stop.set()
        return False

    controller.run_once = once
    controller.run_forever(stop, interval_s=0.1)
    gaps = [later - earlier for earlier, later in zip(starts[1:], starts[2:])]
    assert min(gaps) > 0.08


def test_alarm_reacts_before_buffer_is_flushed(server, make_controller, measure, leds):
    """Nach dem Ausfall entscheidet der aktuelle Wert sofort, nicht erst nach dem Nachliefern."""
    controller = make_controller(server.url)
    server.set_error(500)
    measure(controller, 21.0)
    server.set_reply("alarm")
    measure(controller, 31.0)
    assert leds() == ("on", "off")
    assert len(controller.buffer) == 1


def test_outage_with_known_limits_end_to_end(server, make_controller, measure, leds, state_path):
    controller = make_controller(server.url)
    measure(controller, 23.0)
    server.stop()
    measure(controller, 31.0)  # Server aus: Pi gibt selbst Alarm und puffert
    assert leds() == ("on", "off")
    measure(controller, 27.0)
    assert leds() == ("off", "off")
    assert [m.temp_c for m in controller.buffer] == [31.0, 27.0]
    server.start()
    measure(controller, 24.0)
    controller.flush_buffer()
    assert server.sent_temps == [23.0, 24.0, 31.0, 27.0]
    assert StateStore(state_path).load() == SavedState(LIMITS, "ok")
