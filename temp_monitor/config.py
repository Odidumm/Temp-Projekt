"""Konfiguration aus der .env-Datei (bzw. aus Umgebungsvariablen) laden und prüfen."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

SENSOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # gleiche Regel wie am Server

# Endungen von Namen im eigenen Netz. Nur dort ist http:// erlaubt.
LAN_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".fritz.box")

SENSOR_BACKENDS = ("ds18b20", "fake")

PLACEHOLDER_TOKEN = "bitte-aendern"  # Wert in .env.example
LOCAL_TOKEN_MIN_LENGTH = 16


class ConfigError(Exception):
    """Die Konfiguration ist unvollständig oder ungültig."""


@dataclass(frozen=True)
class Config:
    sensor_id: str
    base_url: str
    api_token: str
    local_token: str
    led_red_pin: int = 17
    led_yellow_pin: int = 27
    interval_s: float = 30.0
    request_timeout_s: float = 5.0
    local_host: str = "0.0.0.0"
    local_port: int = 8080
    w1_device_id: str | None = None
    sensor_backend: str = "ds18b20"
    fake_temp_c: float = 22.0
    fake_temp_file: Path | None = None
    state_file: Path = Path("state.json")
    sensor_fail_limit: int = 3
    log_level: str = "INFO"


def _is_lan_host(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:  # keine IP-Adresse, sondern ein Name
        return "." not in host or host.endswith(LAN_SUFFIXES)


def check_base_url(raw: str) -> str:
    """Prüft BASE_URL und liefert sie ohne Schrägstrich am Ende."""
    if "<" in raw:
        raise ConfigError("BASE_URL enthält noch den Platzhalter <NAS-IP> aus .env.example")
    try:
        url = urlsplit(raw)
        valid = url.scheme in ("http", "https") and bool(url.hostname) and url.port != 0
    except ValueError:  # z. B. Port über 65535
        valid = False
    if not valid:
        raise ConfigError(f"BASE_URL ist keine gültige http-Adresse: {raw!r}")
    if url.path.rstrip("/") or url.query or url.fragment or "@" in url.netloc:
        raise ConfigError("BASE_URL besteht nur aus Adresse und Port, ohne Pfad (also ohne /api/readings)")
    # Cloudflare leitet http:// nicht um, es würde also funktionieren und niemand merkt es.
    if url.scheme == "http" and not _is_lan_host(url.hostname):
        raise ConfigError(
            f"BASE_URL {raw!r}: außerhalb des eigenen Netzes nur https://, "
            "sonst geht das Token unverschlüsselt durchs Internet"
        )
    return f"{url.scheme}://{url.netloc}"


def load_config(
    env_file: str | os.PathLike | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Liest die Konfiguration aus ``env_file`` (falls angegeben) und ``environ``.

    Werte aus echten Umgebungsvariablen haben Vorrang vor der .env-Datei,
    damit man einzelne Werte beim Testen überschreiben kann.
    """
    if environ is None:
        environ = os.environ

    values: dict[str, str] = {}
    if env_file is not None:
        path = Path(env_file)
        if not path.is_file():
            raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    values.update(environ)

    def get(name: str, default: str | None = None) -> str | None:
        value = values.get(name)
        if value is None or value.strip() == "":
            return default
        return value.strip()

    def require(name: str) -> str:
        value = get(name)
        if value is None:
            raise ConfigError(f"{name} fehlt in der Konfiguration")
        return value

    def number(name: str, default: float, minimum: float | None = None) -> float:
        raw = get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"{name} muss eine Zahl sein, nicht {raw!r}") from None
        if not math.isfinite(value):
            raise ConfigError(f"{name} muss eine endliche Zahl sein")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{name} muss mindestens {minimum} sein")
        return value

    def integer(name: str, default: int | None, minimum: int, maximum: int) -> int:
        raw = get(name)
        if raw is None:
            if default is None:
                raise ConfigError(f"{name} fehlt in der Konfiguration")
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{name} muss eine ganze Zahl sein, nicht {raw!r}") from None
        if not minimum <= value <= maximum:
            raise ConfigError(f"{name} muss zwischen {minimum} und {maximum} liegen")
        return value

    sensor_id = require("SENSOR_ID")
    if not SENSOR_ID_RE.fullmatch(sensor_id):
        raise ConfigError(
            f"SENSOR_ID {sensor_id!r} ist ungültig: nur a-z, 0-9, - und _, höchstens 64 Zeichen, "
            "vorne ein Buchstabe oder eine Ziffer"
        )

    if get("API_URL") is not None:
        raise ConfigError(
            "API_URL gibt es nicht mehr: jetzt BASE_URL, ohne Pfad, z. B. BASE_URL=http://<NAS-IP>:50001"
        )
    base_url = check_base_url(require("BASE_URL"))

    sensor_backend = (get("SENSOR_BACKEND", "ds18b20") or "").lower()
    if sensor_backend not in SENSOR_BACKENDS:
        raise ConfigError(f"SENSOR_BACKEND muss einer von {SENSOR_BACKENDS} sein")

    fake_temp_file = get("FAKE_TEMP_FILE")

    # systemd setzt STATE_DIRECTORY, wenn in der Unit StateDirectory= steht.
    state_file = get("STATE_FILE")
    if state_file is None:
        state_dir = get("STATE_DIRECTORY")
        state_file = str(Path(state_dir.split(":")[0]) / "state.json") if state_dir else "state.json"

    log_level = (get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("LOG_LEVEL muss DEBUG, INFO, WARNING oder ERROR sein")

    api_token = require("API_TOKEN")
    if api_token == PLACEHOLDER_TOKEN:
        raise ConfigError("API_TOKEN ist noch der Platzhalter aus .env.example")
    # LOCAL_TOKEN schützt POST /led: Wer es kennt, kann die LEDs abschalten.
    local_token = require("LOCAL_TOKEN")
    if local_token == PLACEHOLDER_TOKEN or len(local_token) < LOCAL_TOKEN_MIN_LENGTH:
        raise ConfigError(
            f"LOCAL_TOKEN ist der Platzhalter oder zu kurz (mind. {LOCAL_TOKEN_MIN_LENGTH} Zeichen). "
            'Erzeugen mit: python3 -c "import secrets; print(secrets.token_hex(24))"'
        )

    if get("LED_PIN") is not None:
        raise ConfigError(
            "LED_PIN gibt es nicht mehr: es gibt jetzt zwei LEDs, "
            "LED_RED_PIN (Alarm, Standard 17) und LED_YELLOW_PIN (Warnung, Standard 27)"
        )
    # BCM-Nummerierung; 0-27 sind die GPIOs der 40-poligen Leiste.
    led_red_pin = integer("LED_RED_PIN", 17, 0, 27)
    led_yellow_pin = integer("LED_YELLOW_PIN", 27, 0, 27)
    if led_red_pin == led_yellow_pin:
        raise ConfigError("LED_RED_PIN und LED_YELLOW_PIN müssen verschiedene GPIOs sein")

    return Config(
        sensor_id=sensor_id,
        base_url=base_url,
        api_token=api_token,
        local_token=local_token,
        led_red_pin=led_red_pin,
        led_yellow_pin=led_yellow_pin,
        interval_s=number("INTERVAL_S", 30.0, minimum=1.0),
        local_host=get("LOCAL_HOST", "0.0.0.0") or "0.0.0.0",
        local_port=integer("LOCAL_PORT", 8080, 1, 65535),
        w1_device_id=get("W1_DEVICE_ID"),
        sensor_backend=sensor_backend,
        fake_temp_c=number("FAKE_TEMP_C", 22.0),
        fake_temp_file=Path(fake_temp_file) if fake_temp_file else None,
        state_file=Path(state_file),
        log_level=log_level,
    )
