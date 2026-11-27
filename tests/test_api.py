"""Lokale REST-API."""

from __future__ import annotations

import pytest

from temp_monitor.api import create_app
from temp_monitor.sensor import SensorError

LOCAL_TOKEN = "local-secret"
AUTH = {"Authorization": f"Bearer {LOCAL_TOKEN}"}
BOTH_AUTO = {"red": "auto", "yellow": "auto"}


@pytest.fixture
def app_and_controller(server, make_controller):
    controller = make_controller(server.url)
    app = create_app(controller, LOCAL_TOKEN)
    return app.test_client(), controller


def test_health_before_first_measurement(app_and_controller):
    client, _ = app_and_controller
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "sensor_id": "rack-a",
        "server_reachable": False,
        "last_push": None,
        "last_error": None,
        "buffered": 0,
        "sensor_ok": True,
        "level": "ok",
        "leds": {"red": "off", "yellow": "off"},
        "led_mode": BOTH_AUTO,
    }


def test_health_after_push(app_and_controller, server):
    client, controller = app_and_controller
    server.set_reply("warn")
    controller.run_once()
    data = client.get("/health").get_json()
    assert data["server_reachable"] is True
    assert data["last_push"] == "2026-11-22T13:05:54Z"
    assert data["level"] == "warn"
    assert data["leds"] == {"red": "off", "yellow": "on"}


def test_temperature(app_and_controller, sensor):
    client, controller = app_and_controller
    assert client.get("/temperature").status_code == 503
    sensor.set(24.5625)
    controller.run_once()
    resp = client.get("/temperature")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "sensor_id": "rack-a",
        "temp_c": 24.56,
        "ts": "2026-11-22T13:05:54Z",
        "sensor_ok": True,
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer falsch"},
        {"Authorization": f"Basic {LOCAL_TOKEN}"},
        {"Authorization": LOCAL_TOKEN},
        {"Authorization": "Bearer "},
    ],
)
def test_led_requires_local_token(app_and_controller, leds, headers):
    client, _ = app_and_controller
    resp = client.post("/led", json={"state": "on"}, headers=headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert leds() == ("off", "off")


@pytest.mark.parametrize(
    "body",
    [
        {"state": "blink"},
        {"state": None},
        {},
        ["on"],
        "on",
        {"state": "on", "led": "green"},
        {"state": "on", "led": ""},
    ],
)
def test_led_rejects_invalid_body(app_and_controller, body):
    client, _ = app_and_controller
    assert client.post("/led", json=body, headers=AUTH).status_code == 400


def test_led_invalid_json(app_and_controller):
    client, _ = app_and_controller
    resp = client.post("/led", data="{kaputt", headers={**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 400


def test_manual_both_and_auto(app_and_controller, server, leds):
    client, controller = app_and_controller
    server.set_reply("ok")

    resp = client.post("/led", json={"state": "on"}, headers=AUTH)  # ohne "led": beide
    assert resp.status_code == 200
    assert resp.get_json() == {
        "leds": {"red": "on", "yellow": "on"},
        "led_mode": {"red": "manual", "yellow": "manual"},
    }
    assert leds() == ("on", "on")

    controller.run_once()  # Server sagt "ok", manuell hat Vorrang
    assert leds() == ("on", "on")
    assert len(server.requests) == 1  # gemessen und gesendet wird trotzdem

    resp = client.post("/led", json={"state": "auto"}, headers=AUTH)
    assert resp.get_json() == {"leds": {"red": "off", "yellow": "off"}, "led_mode": BOTH_AUTO}
    assert leds() == ("off", "off")  # sofort zurück auf die letzte Server-Entscheidung


def test_manual_single_led(app_and_controller, server, leds):
    client, controller = app_and_controller
    server.set_reply("warn")
    controller.run_once()
    assert leds() == ("off", "on")

    resp = client.post("/led", json={"led": "red", "state": "on"}, headers=AUTH)
    assert resp.get_json()["led_mode"] == {"red": "manual", "yellow": "auto"}
    assert leds() == ("on", "on")  # gelb bleibt automatisch (Warnung)

    client.post("/led", json={"led": "yellow", "state": "off"}, headers=AUTH)
    assert leds() == ("on", "off")

    client.post("/led", json={"led": "red", "state": "auto"}, headers=AUTH)
    assert leds() == ("off", "off")  # gelb noch manuell aus
    client.post("/led", json={"state": "auto"}, headers=AUTH)
    assert leds() == ("off", "on")


def test_manual_off_overrides_sensor_blink(app_and_controller, leds, sensor):
    client, controller = app_and_controller
    sensor.set(SensorError("weg"))
    for _ in range(3):
        controller.run_once()
    assert leds() == ("off", "blink")
    client.post("/led", json={"led": "yellow", "state": "off"}, headers=AUTH)
    assert leds() == ("off", "off")
    client.post("/led", json={"state": "auto"}, headers=AUTH)
    assert leds() == ("off", "blink")


def test_unknown_route_and_method_return_json(app_and_controller):
    client, _ = app_and_controller
    resp = client.get("/nope")
    assert resp.status_code == 404 and resp.get_json() == {"error": "Not Found"}
    assert client.get("/led").status_code == 405


def test_empty_local_token_rejected(make_controller, server):
    with pytest.raises(ValueError):
        create_app(make_controller(server.url), "")
