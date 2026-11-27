"""Push an den Server: Format, Header, LEDs nach Antwort, Grenzwerte speichern, Fehlerarten."""

from __future__ import annotations

import json

import pytest

from temp_monitor.client import AuthError, PushError, ServerClient, ServerUnavailable, parse_reply
from temp_monitor.state import SavedState, StateStore, Thresholds

from .conftest import API_TOKEN, FIXED_TS, LIMITS, LIMITS_WITH_WARN, VALID_REPLY


@pytest.fixture
def push(server):
    """Schickt einen Wert an den Mock-Server und liefert die Antwort bzw. wirft PushError."""
    clients = []

    def _push(url=None, timeout_s=1.0):
        client = ServerClient(url or server.url, API_TOKEN, timeout_s=timeout_s)
        clients.append(client)
        return client.push("rack-a", 25.0, FIXED_TS)

    yield _push
    for client in clients:
        client.close()


def test_push_sends_body_and_bearer_token(server, make_controller, sensor):
    sensor.set(31.437)
    make_controller(server.url).run_once()

    assert len(server.requests) == 1
    req = server.requests[0]
    assert req["path"] == "/api/readings"
    assert req["headers"]["Authorization"] == f"Bearer {API_TOKEN}"
    assert req["headers"]["Content-Type"] == "application/json"
    assert req["headers"]["User-Agent"] == "tempmon-pi/1.0"  # sonst blockt Cloudflare
    assert req["body"] == {"sensor_id": "rack-a", "temp_c": 31.44, "ts": "2026-11-22T13:05:54Z"}


# ------------------------------------------------------------------ Antwort


def test_contract_reply_without_warn_c_is_accepted(server, make_controller, leds):
    """Genau die vier Felder aus dem Vertrag. Daran ist die alte Version gescheitert."""
    server.reply = {"status": "alarm", "led": "on", "threshold_c": 30.0, "clear_c": 28.0}
    controller = make_controller(server.url)
    assert controller.run_once() is True
    assert controller.server_reachable is True
    assert controller.thresholds == LIMITS
    assert controller.last_error is None
    assert leds() == ("on", "off")


@pytest.mark.parametrize(
    "status, led, expected",
    [
        ("ok", "off", ("off", "off")),
        ("warn", "off", ("off", "on")),
        ("alarm", "on", ("on", "off")),
        # Rot folgt genau "led", auch wenn status nicht dazu passt.
        ("ok", "on", ("on", "off")),
        ("warn", "on", ("on", "off")),
        ("alarm", "off", ("off", "off")),
    ],
)
def test_leds_follow_server_reply(server, make_controller, leds, status, led, expected):
    controller = make_controller(server.url)
    server.set_reply(status, led)
    controller.run_once()
    assert leds() == expected
    assert controller.server_reachable is True
    assert controller.health()["last_push"] == "2026-11-22T13:05:54Z"


def test_server_wins_over_local_hysteresis(server, make_controller, leds, sensor):
    # Der Server darf anders entscheiden als die lokalen Grenzen.
    sensor.set(20.0)
    server.set_reply("alarm")
    make_controller(server.url).run_once()
    assert leds() == ("on", "off")


@pytest.mark.parametrize("missing", ["status", "led", "threshold_c", "clear_c"])
def test_all_four_contract_fields_are_required(push, server, missing):
    server.reply = {k: v for k, v in VALID_REPLY.items() if k != missing}
    with pytest.raises(PushError, match=missing):
        push()


@pytest.mark.parametrize(
    "reply",
    [
        "das ist kein json",
        ["status", "ok"],
        {**VALID_REPLY, "status": "panic"},
        {**VALID_REPLY, "status": None},
        {**VALID_REPLY, "led": "blink"},
        {**VALID_REPLY, "led": True},
        {**VALID_REPLY, "threshold_c": "30"},
        {**VALID_REPLY, "clear_c": True},
        {**VALID_REPLY, "clear_c": 31},  # clear über threshold
    ],
)
def test_invalid_replies_raise_push_error(push, server, reply):
    server.reply = reply
    with pytest.raises(PushError) as info:
        push()
    # Der Server hat mit 200 geantwortet und den Wert vermutlich gespeichert:
    # nicht noch einmal senden, er erkennt keine doppelten Werte.
    assert not isinstance(info.value, ServerUnavailable)


@pytest.mark.parametrize("key", ["threshold_c", "clear_c"])
def test_nan_rejected(key):
    with pytest.raises(PushError):
        parse_reply({**VALID_REPLY, key: float("nan")})


def test_optional_warn_c_is_used():
    assert parse_reply({**VALID_REPLY, "warn_c": 27}).thresholds == LIMITS_WITH_WARN


@pytest.mark.parametrize("warn_c", [None, "27", True, float("nan"), 30.5, {}])
def test_unusable_warn_c_does_not_invalidate_reply(warn_c):
    reply = parse_reply({**VALID_REPLY, "led": "on", "warn_c": warn_c})
    assert reply.thresholds == LIMITS
    assert reply.level == "alarm"


# ------------------------------------------------------------- Fehler vom Server


def test_401_is_auth_error_with_server_text(push, server):
    server.set_error(401, "Token gehört nicht zu diesem Sensor")
    with pytest.raises(AuthError, match="HTTP 401: Token gehört nicht zu diesem Sensor"):
        push()


def test_400_is_final_with_server_text(push, server):
    server.set_error(400, "ts liegt in der Zukunft – Uhrzeit des Pi prüfen (NTP)")
    with pytest.raises(PushError, match="HTTP 400: ts liegt in der Zukunft") as info:
        push()
    assert type(info.value) is PushError


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_counts_as_unavailable(push, server, status):
    server.set_error(status, "Interner Fehler")
    with pytest.raises(ServerUnavailable, match=f"HTTP {status}: Interner Fehler"):
        push()


@pytest.mark.parametrize("status", [201, 204])
def test_other_2xx_is_final(push, server, status):
    """Der Wert ist vermutlich gespeichert, noch einmal senden gäbe ihn doppelt."""
    server.status, server.reply = status, ""
    with pytest.raises(PushError, match=f"HTTP {status}") as info:
        push()
    assert type(info.value) is PushError


def test_403_comes_from_cloudflare(push, server):
    """So antwortet Cloudflare, wenn es den User-Agent blockt: Klartext statt JSON."""
    server.status, server.reply = 403, "error code: 1010\n"
    with pytest.raises(ServerUnavailable) as info:
        push()
    assert str(info.value) == (
        "HTTP 403: error code: 1010 (kommt von Cloudflare, nicht vom Server: User-Agent prüfen)"
    )


@pytest.mark.parametrize("status", [301, 404, 405, 429, 530])
def test_what_the_server_never_stored_can_be_sent_again(push, server, status):
    """404 (Adresse nicht im Tunnel), 429 (zu viele Anfragen), 530 (Tunnel weg) ..."""
    server.status, server.reply = status, f"error code: {status}"
    with pytest.raises(ServerUnavailable, match=f"HTTP {status}: error code: {status}$"):
        push()


@pytest.mark.parametrize(
    "body, expected",
    [
        ("<html>Bad Gateway</html>", "HTTP 502: Bad Gateway"),
        ("x" * 101, "HTTP 502: Bad Gateway"),  # zu lang fürs Log
        ("", "HTTP 502: Bad Gateway"),
        ('{"error": ""}', 'HTTP 502: {"error": ""}'),
        ({"error": "y" * 300}, "HTTP 502: " + "y" * 200),
        ({"error": 5}, 'HTTP 502: {"error": 5}'),
    ],
)
def test_error_text(push, server, body, expected):
    server.status, server.reply = 502, body
    with pytest.raises(ServerUnavailable) as info:
        push()
    assert str(info.value) == expected


def test_redirect_is_not_followed(push, server):
    server.status = 302
    server.headers = {"Location": "http://example.invalid/steal"}
    with pytest.raises(ServerUnavailable, match="HTTP 302"):
        push()
    assert len(server.requests) == 1


def test_connection_refused_is_unavailable_and_short(push, unreachable_url):
    with pytest.raises(ServerUnavailable, match="Connection refused") as info:
        push(unreachable_url)
    assert "object at 0x" not in str(info.value)


def test_timeout_is_unavailable(push, server):
    server.delay_s = 0.5
    with pytest.raises(ServerUnavailable, match="Keine Antwort innerhalb von 0.1 s"):
        push(timeout_s=0.1)


def test_unusable_url_is_not_unavailable(push):
    with pytest.raises(PushError, match="Anfrage nicht möglich") as info:
        push("http://:50001")  # Rechnername fehlt
    assert type(info.value) is PushError


def test_default_timeout_is_five_seconds():
    from temp_monitor.config import Config

    cfg = Config(sensor_id="x", base_url="http://x", api_token="t", local_token="l")
    assert cfg.request_timeout_s == 5.0


@pytest.mark.parametrize(
    "status, level, text",
    [
        (401, "ERROR", "API_TOKEN oder SENSOR_ID in der .env ist falsch"),
        (400, "ERROR", "verworfen: HTTP 400: Fehlertext"),
        (403, "WARNING", "User-Agent prüfen) – Wert gepuffert (1 von höchstens 2000)"),
        (500, "WARNING", "Wert gepuffert (1 von höchstens 2000)"),
    ],
)
def test_errors_are_logged_clearly(server, make_controller, caplog, status, level, text):
    server.set_error(status)
    controller = make_controller(server.url)
    controller.run_once()
    assert [r.levelname for r in caplog.records if text in r.getMessage()] == [level]
    assert controller.health()["last_error"].startswith(f"HTTP {status}: Fehlertext")


def test_last_error_is_cleared_after_success(server, make_controller):
    controller = make_controller(server.url)
    server.set_error(500)
    controller.run_once()
    server.set_reply("ok")
    controller.run_once()
    assert controller.health()["last_error"] is None


# ------------------------------------------------------------ Zustand speichern


def test_thresholds_are_persisted_and_survive_restart(server, make_controller, state_path):
    server.set_reply("warn", threshold_c=36.5, clear_c=34.0)
    make_controller(server.url).run_once()

    assert json.loads(state_path.read_text()) == {"threshold_c": 36.5, "clear_c": 34.0, "level": "warn"}
    # "Neustart": neuer Controller liest die Datei
    restarted = make_controller(server.url)
    assert restarted.thresholds == Thresholds(36.5, 34.0)
    assert restarted.level == "warn"


def test_optional_warn_c_is_persisted(server, make_controller, state_path):
    server.set_reply("ok", warn_c=27.0)
    make_controller(server.url).run_once()
    assert StateStore(state_path).load() == SavedState(LIMITS_WITH_WARN, "ok")


def test_state_only_written_when_changed(server, make_controller):
    controller = make_controller(server.url)
    saved = []
    original_save = controller.store.save
    controller.store.save = lambda s: saved.append(s) or original_save(s)

    controller.run_once()
    controller.run_once()  # gleiche Grenzen, gleiche Stufe -> kein erneutes Schreiben (SD-Karte)
    assert saved == [SavedState(LIMITS, "ok")]

    server.set_reply("ok", threshold_c=36.0, clear_c=34.0)
    controller.run_once()
    server.set_reply("warn", threshold_c=36.0, clear_c=34.0)
    controller.run_once()
    new = Thresholds(36.0, 34.0)
    assert saved[1:] == [SavedState(new, "ok"), SavedState(new, "warn")]


def test_state_write_failure_does_not_crash_and_is_retried(server, make_controller, tmp_path, leds):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # eine Datei, wo das Verzeichnis hin soll -> Schreiben schlägt fehl
    controller = make_controller(server.url, store_path=blocker / "state.json")
    server.set_reply("alarm")
    controller.run_once()  # Speichern schlägt fehl, wird nur geloggt
    assert leds() == ("on", "off")
    assert controller.thresholds == LIMITS

    blocker.unlink()  # Problem behoben, Server schickt dasselbe
    controller.run_once()
    assert StateStore(blocker / "state.json").load() == SavedState(LIMITS, "alarm")


def test_invalid_reading_is_not_sent(server, make_controller, sensor):
    from temp_monitor.sensor import SensorError

    sensor.set(SensorError("CRC-Fehler"))
    controller = make_controller(server.url)
    assert controller.run_once() is False
    assert server.requests == []
    assert len(controller.buffer) == 0


@pytest.mark.parametrize(
    "text, expected",
    [
        ("{kaputt", SavedState()),
        ("[1, 2]", SavedState()),
        ('{"threshold_c": 20, "clear_c": 25}', SavedState()),  # clear über threshold
        ('{"threshold_c": 30}', SavedState()),  # clear_c fehlt
        ('{"threshold_c": 30, "clear_c": 28, "warn_c": 31}', SavedState()),  # warn über threshold
        ('{"threshold_c": 30, "clear_c": 28}', SavedState(LIMITS, "ok")),
        ('{"threshold_c": 30, "clear_c": 28, "warn_c": 27, "level": "warn"}', SavedState(LIMITS_WITH_WARN, "warn")),
        ('{"threshold_c": 30, "clear_c": 28, "level": "panic"}', SavedState(LIMITS, "ok")),
        # Ohne Grenzen gibt es keine Stufe: vor der ersten Antwort bleiben die LEDs aus.
        ('{"level": "alarm"}', SavedState()),
    ],
)
def test_store_load(tmp_path, text, expected):
    path = tmp_path / "state.json"
    path.write_text(text)
    assert StateStore(path).load() == expected
