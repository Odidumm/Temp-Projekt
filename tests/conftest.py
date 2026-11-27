"""Gemeinsame Fixtures: gemockter Server, Fake-Sensor, Fake-LED, Controller."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from temp_monitor.client import ServerClient
from temp_monitor.controller import Controller
from temp_monitor.led import FakeLed
from temp_monitor.sensor import FakeSensor
from temp_monitor.state import StateStore, Thresholds

API_TOKEN = "server-secret"
FIXED_TS = datetime(2026, 11, 22, 13, 5, 54, 123456, tzinfo=timezone.utc)
# Was der Server laut Vertrag schickt: Alarm ab 30, Alarm-Ende unter 28, keine Warngrenze.
LIMITS = Thresholds(threshold_c=30.0, clear_c=28.0)
# Mit der optionalen Warngrenze: Warnung ab 27, Ende unter 25.
LIMITS_WITH_WARN = Thresholds(threshold_c=30.0, clear_c=28.0, warn_c=27.0)
VALID_REPLY = {"status": "ok", "led": "off", "threshold_c": 30.0, "clear_c": 28.0}


@dataclass
class MockServer:
    """Kleiner HTTP-Server im Hintergrund, der den zentralen Server spielt.

    ``reply`` ist das JSON (dict) oder ein roher String, ``status`` der
    HTTP-Code, ``delay_s`` eine künstliche Verzögerung (für Timeouts).
    Alle Requests landen in ``requests``. ``on_request`` darf ``status`` und
    ``reply`` je nach Body ändern, bevor geantwortet wird.
    """

    status: int = 200
    reply: object = field(default_factory=lambda: dict(VALID_REPLY))
    delay_s: float = 0.0
    headers: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    on_request: object = None
    url: str = ""

    def start(self) -> None:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                mock.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw or b"null")}
                )
                if mock.on_request:
                    mock.on_request(mock.requests[-1]["body"])
                if mock.delay_s:
                    time.sleep(mock.delay_s)
                payload = mock.reply if isinstance(mock.reply, str) else json.dumps(mock.reply)
                data = payload.encode()
                try:
                    self.send_response(mock.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for key, value in mock.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Client hat wegen Timeout schon aufgegeben

            def log_message(self, *args):
                pass

        # Nach einem stop() wieder auf demselben Port, damit der Client ihn wiederfindet.
        port = int(self.url.rsplit(":", 1)[1]) if self.url else 0
        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def sent_temps(self) -> list[float]:
        return [request["body"]["temp_c"] for request in self.requests]

    def set_reply(self, status: str, led: str | None = None, **extra):
        """Wie der echte Server: led ist genau dann on, wenn status alarm ist."""
        self.status = 200
        self.reply = {**VALID_REPLY, "status": status, "led": led or ("on" if status == "alarm" else "off"), **extra}

    def set_error(self, status: int, text: str = "Fehlertext"):
        self.status = status
        self.reply = {"error": text}


@pytest.fixture
def server():
    mock = MockServer()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def unreachable_url():
    """BASE_URL, unter der garantiert niemand lauscht (Port gebunden, aber nicht im listen)."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    sock.close()


@pytest.fixture
def sensor():
    return FakeSensor(25.0)


@pytest.fixture
def red():
    return FakeLed()


@pytest.fixture
def yellow():
    return FakeLed()


@pytest.fixture
def leds(red, yellow):
    """Aktueller Zustand beider LEDs als Tupel (rot, gelb)."""
    return lambda: (red.state, yellow.state)


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state" / "state.json"


class Clock:
    """Systemuhr für Tests: steht auf FIXED_TS, bis ein Test sie weiterstellt."""

    def __init__(self) -> None:
        self.now = FIXED_TS
        self.synced = True

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float = 30) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_controller(sensor, red, yellow, state_path, clock):
    """Baut einen Controller; ``url`` (BASE_URL) bestimmt, wohin gepusht wird."""
    clients = []

    def factory(url, *, timeout_s=1.0, fail_limit=3, store_path=None, buffer_size=2000):
        client = ServerClient(url, API_TOKEN, timeout_s=timeout_s)
        clients.append(client)
        return Controller(
            sensor=sensor,
            red=red,
            yellow=yellow,
            client=client,
            store=StateStore(store_path or state_path),
            sensor_id="rack-a",
            sensor_fail_limit=fail_limit,
            buffer_size=buffer_size,
            now=clock,
            clock_synced=lambda: clock.synced,
        )

    yield factory
    for client in clients:
        client.close()
