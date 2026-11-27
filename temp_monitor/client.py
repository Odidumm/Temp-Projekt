"""Messwerte an den zentralen Server schicken und die Antwort prüfen (Vertrag: pi-api.txt)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from .state import Thresholds
from .timeutil import iso_utc

API_PATH = "/api/readings"
# Vorgabe aus pi-api.txt: Cloudflare blockt z. B. den Standardwert von urllib mit HTTP 403.
USER_AGENT = "tempmon-pi/1.0"

STATUSES = ("ok", "warn", "alarm")
LED_VALUES = ("on", "off")


class PushError(Exception):
    """Der Messwert wurde nicht angenommen oder die Antwort ist unbrauchbar.
    Noch einmal senden hilft nicht (HTTP 400) oder gäbe den Wert doppelt."""


class ServerUnavailable(PushError):
    """Der Messwert ist nicht beim Server angekommen (nicht erreichbar, Timeout, 5xx,
    von Cloudflare geblockt ...): puffern und später nachliefern."""


class AuthError(PushError):
    """HTTP 401: Token oder sensor_id falsch."""


@dataclass(frozen=True)
class ServerReply:
    status: str  # ok | warn | alarm
    led: str  # on | off
    thresholds: Thresholds

    @property
    def level(self) -> str:
        """Stufe für die LEDs: Rot folgt genau ``led``, Gelb leuchtet bei ``status`` warn."""
        if self.led == "on":
            return "alarm"
        return "warn" if self.status == "warn" else "ok"


def _is_number(value: Any) -> bool:
    # bool ist in Python eine Unterklasse von int, soll hier aber nicht durchgehen.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(data: dict, key: str) -> float:
    value = data.get(key)
    if not _is_number(value):
        raise PushError(f"Antwort: {key} ist keine gültige Zahl ({value!r})")
    return float(value)


def parse_reply(data: Any) -> ServerReply:
    """Prüft die Server-Antwort. Alles, was nicht zum Vertrag passt, wirft
    PushError, damit der Pi selbst entscheidet, statt mit Unsinn weiterzuarbeiten."""
    if not isinstance(data, dict):
        raise PushError("Antwort ist kein JSON-Objekt")
    status, led = data.get("status"), data.get("led")
    if status not in STATUSES:
        raise PushError(f"Antwort: unbekannter status {status!r}")
    if led not in LED_VALUES:
        raise PushError(f"Antwort: unbekannter Wert für led {led!r}")
    threshold_c = _number(data, "threshold_c")
    clear_c = _number(data, "clear_c")
    if clear_c > threshold_c:
        raise PushError(f"Antwort: clear_c ({clear_c}) liegt über threshold_c ({threshold_c})")
    # warn_c steht nicht im Vertrag. Fehlt es oder passt es nicht, gilt die Antwort trotzdem.
    warn_c = data.get("warn_c")
    warn_c = float(warn_c) if _is_number(warn_c) and warn_c <= threshold_c else None
    return ServerReply(status=status, led=led, thresholds=Thresholds(threshold_c, clear_c, warn_c))


def _describe(exc: requests.RequestException) -> str:
    """Kurze Fehlerursache fürs Log statt der langen urllib3-Darstellung,
    z. B. ``Failed to establish a new connection: [Errno 111] Connection refused``."""
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    text = str(reason or exc)
    if ">: " in text:  # "<urllib3...NewConnectionError object at 0x...>: eigentliche Meldung"
        text = text.split(">: ", 1)[1]
    return text


def _error_text(response: requests.Response) -> str:
    """Fehlertext des Servers (``{"error": "<Text>"}``) oder von Cloudflare (kurzer
    Klartext wie ``error code: 1010``), sonst der allgemeine HTTP-Text."""
    try:
        data = response.json()
    except ValueError:
        data = None
    text = data.get("error") if isinstance(data, dict) else None
    if isinstance(text, str) and text:
        return text[:200]
    if len(response.content) <= 100:
        text = response.text.strip()
        if text and "<" not in text:
            return text
    return response.reason or "ohne Fehlertext"


class ServerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_s: float = 5.0,
        session: requests.Session | None = None,
    ) -> None:
        self.url = base_url + API_PATH
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})

    def push(self, sensor_id: str, temp_c: float, ts: datetime) -> ServerReply:
        body = {"sensor_id": sensor_id, "temp_c": round(temp_c, 2), "ts": iso_utc(ts)}
        try:
            # Keine Weiterleitungen folgen: ein 30x zählt als Fehler,
            # und das Token geht nicht an eine andere Adresse.
            response = self.session.post(
                self.url, json=body, timeout=self.timeout_s, allow_redirects=False
            )
        except requests.Timeout:
            raise ServerUnavailable(f"Keine Antwort innerhalb von {self.timeout_s:g} s") from None
        except (
            requests.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        ) as exc:
            raise ServerUnavailable(f"Server nicht erreichbar: {_describe(exc)}") from None
        except requests.RequestException as exc:  # z. B. ungültige BASE_URL
            raise PushError(f"Anfrage nicht möglich: {_describe(exc)}") from None

        with response:
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise PushError("Server-Antwort ist kein gültiges JSON") from None
                return parse_reply(data)
            status = response.status_code
            message = f"HTTP {status}: {_error_text(response)}"

        if status == 401:
            raise AuthError(message)
        if status == 400 or 200 <= status < 300:
            raise PushError(message)
        if status == 403:
            message += " (kommt von Cloudflare, nicht vom Server: User-Agent prüfen)"
        # 5xx und alles, was der Server selbst nicht schickt (403, 404, 429, Umleitung ...):
        # Der Wert ist dort nicht gespeichert, später noch einmal senden.
        raise ServerUnavailable(message)

    def close(self) -> None:
        self.session.close()
