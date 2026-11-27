"""Konfiguration aus .env."""

from __future__ import annotations

from pathlib import Path

import pytest

from temp_monitor.config import ConfigError, load_config

BASE = {
    "SENSOR_ID": "rack-a",
    "BASE_URL": "http://192.0.2.10:50001",
    "API_TOKEN": "abc",
    "LOCAL_TOKEN": "0123456789abcdef",
}


def test_defaults():
    cfg = load_config(None, BASE)
    assert cfg.interval_s == 30.0
    assert cfg.request_timeout_s == 5.0
    assert (cfg.led_red_pin, cfg.led_yellow_pin) == (17, 27)
    assert cfg.local_port == 8080
    assert cfg.sensor_backend == "ds18b20"
    assert cfg.state_file == Path("state.json")


def test_env_file_and_environment_override(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "SENSOR_ID=rack-b\nBASE_URL=http://srv\nAPI_TOKEN='x y'\nLOCAL_TOKEN=zzzzzzzzzzzzzzzz\n"
        "LED_RED_PIN=22\nINTERVAL_S=10\n# Kommentar\n"
    )
    cfg = load_config(env, {"INTERVAL_S": "5"})
    assert cfg.sensor_id == "rack-b"
    assert cfg.api_token == "x y"
    assert cfg.interval_s == 5.0  # Umgebungsvariable gewinnt
    assert cfg.led_red_pin == 22


def test_state_directory_from_systemd():
    cfg = load_config(None, {**BASE, "STATE_DIRECTORY": "/var/lib/temp-monitor"})
    assert cfg.state_file == Path("/var/lib/temp-monitor/state.json")


def test_missing_env_file(tmp_path):
    with pytest.raises(ConfigError, match="nicht gefunden"):
        load_config(tmp_path / "fehlt.env", BASE)


@pytest.mark.parametrize("missing", ["SENSOR_ID", "BASE_URL", "API_TOKEN", "LOCAL_TOKEN"])
def test_required_values(missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(None, env)


@pytest.mark.parametrize(
    "key, value",
    [
        ("LED_RED_PIN", "elf"),
        ("LED_YELLOW_PIN", "40"),
        ("LED_YELLOW_PIN", "17"),  # gleicher Pin wie rot
        ("LED_PIN", "17"),  # alter Name -> klare Fehlermeldung
        ("INTERVAL_S", "0"),
        ("INTERVAL_S", "nan"),
        ("API_URL", "http://srv:50001/api/readings"),  # alter Name -> klare Fehlermeldung
        ("BASE_URL", "ftp://srv"),
        ("BASE_URL", "http://srv:50001/api/readings"),  # mit Pfad
        ("BASE_URL", "http://srv:50001/?x=1"),
        ("BASE_URL", "http://srv:50001/#x"),
        ("BASE_URL", "http://name:passwort@srv:50001"),  # würde den Bearer-Header ersetzen
        ("BASE_URL", "http://<NAS-IP>:50001"),  # Platzhalter
        ("BASE_URL", "http://:50001"),  # Rechner fehlt
        ("BASE_URL", "http://srv:500011"),  # Port zu groß
        ("BASE_URL", "http://[::1"),
        ("BASE_URL", "192.0.2.10:50001"),  # http:// fehlt
        ("BASE_URL", "api.odidumm.com"),
        ("SENSOR_ID", "Rack-A"),
        ("SENSOR_ID", "rack a"),
        ("SENSOR_ID", "-rack"),
        ("SENSOR_ID", "a" * 65),
        ("SENSOR_BACKEND", "dht22"),
        ("LOCAL_PORT", "70000"),
        ("LOCAL_TOKEN", "   "),
        ("LOCAL_TOKEN", "bitte-aendern"),  # Platzhalter aus .env.example
        ("LOCAL_TOKEN", "1234"),  # zu kurz
        ("LOCAL_TOKEN", "a" * 15),
        ("API_TOKEN", "bitte-aendern"),
    ],
)
def test_invalid_values(key, value):
    with pytest.raises(ConfigError):
        load_config(None, {**BASE, key: value})


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://192.168.178.101:50001", "http://192.168.178.101:50001"),
        ("http://192.168.178.101:50001/", "http://192.168.178.101:50001"),
        ("https://api.odidumm.com", "https://api.odidumm.com"),
        ("https://api.odidumm.com/", "https://api.odidumm.com"),
        ("HTTPS://api.odidumm.com", "https://api.odidumm.com"),
        ("http://localhost:9000", "http://localhost:9000"),
        ("http://nas:50001", "http://nas:50001"),
        ("http://nas.local:50001", "http://nas.local:50001"),
        ("http://nas.fritz.box:50001", "http://nas.fritz.box:50001"),
        ("http://[fd00::10]:50001", "http://[fd00::10]:50001"),
        ("https://203.0.113.5", "https://203.0.113.5"),
    ],
)
def test_base_url_is_accepted(raw, expected):
    assert load_config(None, {**BASE, "BASE_URL": raw}).base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://api.odidumm.com",  # Cloudflare würde es annehmen, das Token wäre mitlesbar
        "http://api.odidumm.com:80/",
        "http://8.8.8.8:50001",
        "http://local.example.com",  # nur die Endung zählt
        "http://nas.local.example.com:50001",
    ],
)
def test_http_is_only_allowed_in_the_lan(raw):
    with pytest.raises(ConfigError, match="nur https://"):
        load_config(None, {**BASE, "BASE_URL": raw})


def test_base_url_error_does_not_reveal_password():
    with pytest.raises(ConfigError) as info:
        load_config(None, {**BASE, "BASE_URL": "https://name:geheim@api.odidumm.com"})
    assert "geheim" not in str(info.value)


def test_old_api_url_name_gives_helpful_error():
    with pytest.raises(ConfigError, match="BASE_URL"):
        load_config(None, {**BASE, "API_URL": "http://srv/api/readings"})


def test_local_token_with_16_chars_is_accepted():
    assert load_config(None, {**BASE, "LOCAL_TOKEN": "a" * 16}).local_token == "a" * 16


def test_env_example_contains_only_placeholders(tmp_path):
    """Wer .env.example unverändert übernimmt, bekommt einen klaren Konfigurationsfehler."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    with pytest.raises(ConfigError, match="Platzhalter"):
        load_config(example, {})


@pytest.mark.parametrize("sensor_id", ["rack-a", "pi_2", "0", "a" * 64])
def test_valid_sensor_ids(sensor_id):
    assert load_config(None, {**BASE, "SENSOR_ID": sensor_id}).sensor_id == sensor_id


def test_limits_are_not_configurable():
    """Die Grenzen kommen aus dem Dashboard; alte FALLBACK_*-Werte haben keine Wirkung."""
    cfg = load_config(None, {**BASE, "FALLBACK_ALARM_C": "20", "FALLBACK_WARN_C": "10"})
    assert cfg == load_config(None, BASE)


def test_old_led_pin_name_gives_helpful_error():
    with pytest.raises(ConfigError, match="LED_RED_PIN"):
        load_config(None, {**BASE, "LED_PIN": "17"})
