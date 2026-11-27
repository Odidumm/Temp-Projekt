"""Betrieb als Dienst: API-Server, Logging fürs Journal, simulierte LED."""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest
from flask import Flask

from temp_monitor.__main__ import JournalFormatter
from temp_monitor.server import create_server


@pytest.fixture
def running_server():
    servers = []

    def start(**kwargs):
        app = Flask(__name__)
        app.get("/ping")(lambda: {"pong": True})
        srv = create_server(app, "127.0.0.1", 0, **kwargs)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        servers.append(srv)
        return srv, srv.socket.getsockname()[1]

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _get(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    return data


def test_idle_connections_cannot_block_the_api(running_server):
    _, port = running_server(max_connections=2, timeout_s=0.3)
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(5)]  # senden nie etwas
    try:
        time.sleep(0.6)  # Timeout der stillen Verbindungen abwarten
        assert b'"pong"' in _get(port)
    finally:
        for sock in idle:
            sock.close()


def test_connections_over_limit_are_closed_immediately(running_server):
    _, port = running_server(max_connections=1, timeout_s=5)
    first = socket.create_connection(("127.0.0.1", port))  # belegt den einzigen Platz
    try:
        time.sleep(0.1)
        second = socket.create_connection(("127.0.0.1", port), timeout=2)
        assert second.recv(1) == b""  # Server hat sofort geschlossen
        second.close()
    finally:
        first.close()
    time.sleep(0.1)
    assert b'"pong"' in _get(port)  # Platz wieder frei


def test_port_in_use_raises_oserror_instead_of_exiting(running_server):
    _, port = running_server()
    with pytest.raises(OSError):
        create_server(Flask(__name__), "127.0.0.1", port)


def test_journal_formatter_adds_syslog_priority():
    fmt = JournalFormatter("%(levelname)s %(message)s")

    def line(level):
        return fmt.format(logging.LogRecord("x", level, __file__, 1, "Text", None, None))

    assert line(logging.INFO) == "<6>INFO Text"
    assert line(logging.WARNING) == "<4>WARNING Text"
    assert line(logging.ERROR) == "<3>ERROR Text"


def test_gpio_led_with_mock_factory_reports_simulated(monkeypatch):
    from gpiozero import Device
    from gpiozero.pins.mock import MockFactory

    from temp_monitor.led import GpioLed

    monkeypatch.setattr(Device, "pin_factory", MockFactory())
    led = GpioLed(17, blink_on_s=0.01, blink_off_s=0.01)
    try:
        assert led.simulated is True
        led.set("on")
        assert led._led.is_lit
        led.set("blink")
        led.set("blink")  # zweimal blink startet das Blinken nicht neu
        assert led.state == "blink"
        led.set("off")
        assert not led._led.is_lit
    finally:
        led.close()
        Device.pin_factory.close()


# ------------------------------------------------------------- Start (main)

START_ENV = {
    "SENSOR_ID": "pi-rack-a",
    "BASE_URL": "http://127.0.0.1:9",
    "API_TOKEN": "test",
    "LOCAL_TOKEN": "0123456789abcdef",
    "SENSOR_BACKEND": "fake",
}


@pytest.fixture
def start_env(monkeypatch, tmp_path):
    """Leeres Arbeitsverzeichnis (keine .env) und eine kontrollierte Umgebung."""
    monkeypatch.chdir(tmp_path)
    for key in ("API_URL", "LED_PIN", "LED_RED_PIN", "LED_YELLOW_PIN", "STATE_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in START_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    return monkeypatch


def test_main_config_error_exits_with_2(start_env):
    from temp_monitor.__main__ import EXIT_CONFIG_ERROR, main

    start_env.setenv("LED_PIN", "17")  # alter Variablenname
    assert main([]) == EXIT_CONFIG_ERROR


def test_main_missing_env_file_exits_with_2(start_env, tmp_path):
    from temp_monitor.__main__ import EXIT_CONFIG_ERROR, main

    assert main(["--env-file", str(tmp_path / "fehlt.env")]) == EXIT_CONFIG_ERROR


def test_main_releases_red_led_when_yellow_fails(start_env, capsys):
    import temp_monitor.led as led_module
    from temp_monitor.__main__ import main

    created = []

    class FlakyGpioLed(led_module.FakeLed):
        def __init__(self, pin):
            if pin == 27:
                raise RuntimeError("GPIO busy")
            super().__init__()
            created.append(self)

    start_env.setattr(led_module, "GpioLed", FlakyGpioLed)
    assert main([]) == 1
    assert len(created) == 1 and created[0].closed  # rote LED wieder freigegeben
    assert "LED gelb an GPIO 27 nicht verfügbar" in capsys.readouterr().out


def test_main_warns_when_leds_are_simulated(start_env, capsys):
    # main() loggt nach stdout (journald) und richtet das Logging neu ein,
    # deshalb wird stdout geprüft statt caplog.
    import temp_monitor.__main__ as main_module

    start_env.setenv("GPIOZERO_PIN_FACTORY", "mock")
    start_env.setenv("LOCAL_PORT", "1")  # Port 1 darf pi nicht öffnen -> Start bricht danach ab
    assert main_module.main([]) == 1
    out = capsys.readouterr().out
    assert "LEDs werden nur simuliert" in out
    assert "Lokale API kann nicht" in out
