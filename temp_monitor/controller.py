"""Messschleife, Puffer und LED-Logik.

Zwei LEDs: **rot** = Alarm, **gelb** = Warnung. Grundlage ist die Stufe
``ok`` / ``warn`` / ``alarm``:

- ok    -> beide aus
- warn  -> gelb an
- alarm -> rot an

Wer bestimmt die LEDs? (höchste Priorität zuerst)

1. manuell über ``POST /led`` (je LED ``on``/``off``), bis ``auto`` kommt
2. Sensor ausgefallen (``sensor_fail_limit`` Messungen in Folge ungültig):
   gelb blinkt, rot behält die letzte Stufe (ein Alarm endet nicht durch
   einen Sensorausfall)
3. Server-Antwort: rot folgt genau ``led``, gelb leuchtet bei ``status`` warn
4. keine gültige Antwort (nicht erreichbar, 400, 401, 403, Uhr nicht gestellt ...):
   der Pi entscheidet selbst mit den zuletzt empfangenen Grenzen. Vor der
   allerersten Antwort bleiben die LEDs aus.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .client import AuthError, PushError, ServerClient, ServerReply, ServerUnavailable
from .led import Led
from .sensor import Sensor, SensorError
from .state import SavedState, StateStore, Thresholds
from .timeutil import clock_synchronized, iso_utc, utc_now

log = logging.getLogger(__name__)

LED_NAMES = ("red", "yellow")

# Puffer im Arbeitsspeicher: reicht bei 1 s Takt für gut 16 Stunden (voll unter 10 MB).
BUFFER_SIZE = 60_000


def classify(temp_c: float, current: str, limits: Thresholds | None) -> str:
    """Stufe ohne Server bestimmen, mit Hysterese.

    - Noch nie Grenzen vom Server bekommen: ``ok``, die LEDs bleiben aus.
    - Alarm ab ``threshold_c``, endet erst unter ``clear_c``.
    - Warnung nur, wenn der Server ``warn_c`` mitschickt: ab ``warn_c``, endet
      erst unter ``warn_c - (threshold_c - clear_c)``.

    Dazwischen bleibt der bisherige Zustand, damit die LEDs bei Werten um eine
    Grenze herum nicht flackern.
    """
    if limits is None:
        return "ok"
    if temp_c >= limits.threshold_c:
        return "alarm"
    if current == "alarm" and temp_c >= limits.clear_c:
        return "alarm"
    if limits.warn_c is not None:
        if temp_c >= limits.warn_c:
            return "warn"
        if current in ("warn", "alarm") and temp_c >= limits.warn_clear_c:
            return "warn"
    return "ok"


@dataclass(frozen=True, slots=True)
class Measurement:
    temp_c: float
    ts: datetime


class Controller:
    def __init__(
        self,
        sensor: Sensor,
        red: Led,
        yellow: Led,
        client: ServerClient,
        store: StateStore,
        sensor_id: str,
        sensor_fail_limit: int = 3,
        buffer_size: int = BUFFER_SIZE,
        now: Callable[[], datetime] = utc_now,
        clock_synced: Callable[[], bool] = clock_synchronized,
    ) -> None:
        self.sensor = sensor
        self.leds: dict[str, Led] = {"red": red, "yellow": yellow}
        self.client = client
        self.store = store
        self.sensor_id = sensor_id
        self.sensor_fail_limit = max(1, sensor_fail_limit)
        self._now = now
        self._clock_synced = clock_synced
        self._lock = threading.RLock()

        saved = store.load()
        self._persisted = saved  # Stand der Datei; weicht er ab, wird gespeichert
        self.thresholds: Thresholds | None = saved.thresholds
        self.level = saved.level  # aktuelle Stufe der LEDs
        if self.thresholds:
            log.info(
                "Gespeicherte Grenzwerte geladen: %s, letzte Stufe %s",
                _describe(self.thresholds),
                self.level,
            )
        self.manual: dict[str, str | None] = {name: None for name in LED_NAMES}
        self.sensor_failures = 0
        self.last_measurement: Measurement | None = None
        self.last_push: datetime | None = None
        self.last_error: str | None = None
        self.clock_ok: bool | None = None  # None = noch nicht geprüft
        # Nicht gesendete Werte; ist der Puffer voll, fallen die ältesten heraus.
        self.buffer: deque[Measurement] = deque(maxlen=buffer_size)
        self._decided_by: str | None = None  # "server" oder "pi"
        self._apply_leds()

    # ------------------------------------------------------------------ Status

    @property
    def sensor_ok(self) -> bool:
        return self.sensor_failures < self.sensor_fail_limit

    @property
    def server_reachable(self) -> bool:
        """True, wenn der Server die letzte Messung angenommen hat."""
        return self._decided_by == "server"

    def led_states(self) -> dict[str, str]:
        """Was die LEDs gerade zeigen sollen: on / off / blink."""
        auto = {
            "red": "on" if self.level == "alarm" else "off",
            "yellow": "on" if self.level == "warn" else "off",
        }
        if not self.sensor_ok:
            auto["yellow"] = "blink"
        return {name: self.manual[name] or auto[name] for name in LED_NAMES}

    def led_modes(self) -> dict[str, str]:
        return {name: "auto" if self.manual[name] is None else "manual" for name in LED_NAMES}

    def health(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "sensor_id": self.sensor_id,
                "server_reachable": self.server_reachable,
                "last_push": iso_utc(self.last_push),
                "last_error": self.last_error,
                "buffered": len(self.buffer),
                "sensor_ok": self.sensor_ok,
                "level": self.level,
                "leds": self.led_states(),
                "led_mode": self.led_modes(),
            }

    def temperature(self) -> dict | None:
        with self._lock:
            m = self.last_measurement
            if m is None:
                return None
            return {
                "sensor_id": self.sensor_id,
                "temp_c": m.temp_c,
                "ts": iso_utc(m.ts),
                "sensor_ok": self.sensor_ok,
            }

    # --------------------------------------------------------------- Steuerung

    def set_led_mode(self, state: str, led: str | None = None) -> dict:
        """``on``/``off`` schaltet manuell, ``auto`` gibt die Steuerung zurück.

        ``led`` ist ``red`` oder ``yellow``; ohne Angabe gilt es für beide.
        """
        if state not in ("on", "off", "auto"):
            raise ValueError(f"Ungültiger LED-Modus: {state!r}")
        if led is not None and led not in LED_NAMES:
            raise ValueError(f"Unbekannte LED: {led!r}")
        with self._lock:
            for name in (led,) if led else LED_NAMES:
                self.manual[name] = None if state == "auto" else state
            log.info("LED %s manuell gesetzt: %s", led or "rot+gelb", state)
            self._apply_leds()
            return {"leds": self.led_states(), "led_mode": self.led_modes()}

    def run_once(self) -> bool:
        """Ein Messzyklus: messen, senden, LEDs schalten.

        Liefert True, wenn der Server die Messung angenommen hat.
        """
        # Erst die Uhr prüfen, dann den Zeitstempel nehmen: Wird die Uhr genau
        # dazwischen gestellt, ginge sonst ein falscher ts an den Server.
        clock_ok = self._check_clock()
        ts = self._now()
        try:
            # Gerundet wie gesendet, damit Pi und Server denselben Wert bewerten.
            measurement = Measurement(round(self.sensor.read_celsius(), 2), ts)
        except Exception as exc:  # noqa: BLE001 - der Dienst darf nicht abstürzen
            self._on_sensor_error(exc)
            return False

        # Sensor und Netzwerk ohne Lock, damit die lokale API nicht blockiert.
        reply: ServerReply | None = None
        error: Exception | None = None
        if clock_ok:
            try:
                reply = self.client.push(self.sensor_id, measurement.temp_c, ts)
            except Exception as exc:  # noqa: BLE001
                error = exc

        with self._lock:
            if not self.sensor_ok:
                log.warning("Sensor liefert wieder gültige Werte (%.2f °C)", measurement.temp_c)
            self.sensor_failures = 0
            self.last_measurement = measurement

            if reply is not None:
                self.last_push = ts
                self.last_error = None
                self._update_thresholds(reply.thresholds)
                self._set_decided_by("server")
                new_level = reply.level
            else:
                self._on_push_error(error, measurement)
                self._set_decided_by("pi")
                new_level = classify(measurement.temp_c, self.level, self.thresholds)

            if new_level != self.level:
                log.warning(
                    "Stufe %s -> %s bei %.2f °C (%s)",
                    self.level,
                    new_level,
                    measurement.temp_c,
                    "Server" if reply is not None else "Pi",
                )
            self.level = new_level
            self._apply_leds()
            self._persist()
        return reply is not None

    def flush_buffer(self, stop: threading.Event | None = None, budget_s: float = 15.0) -> None:
        """Gepufferte Werte mit ihrem ursprünglichen Zeitstempel nachliefern, älteste zuerst.

        Nur aufrufen, nachdem der Server den aktuellen Wert angenommen hat. Die
        Antworten gehören zu alten Werten und ändern weder LEDs noch Grenzen.
        Nach ``budget_s`` ist Schluss, der Rest folgt im nächsten Zyklus.
        """
        if not self.buffer:
            return
        deadline = time.monotonic() + budget_s
        sent = 0
        while self.buffer and time.monotonic() < deadline and not (stop and stop.is_set()):
            measurement = self.buffer[0]
            try:
                self.client.push(self.sensor_id, measurement.temp_c, measurement.ts)
                sent += 1
            except (ServerUnavailable, AuthError) as exc:
                log.warning("Nachliefern unterbrochen: %s", exc)
                break  # Wert bleibt im Puffer
            except PushError as exc:
                log.error("Gepufferter Wert von %s verworfen: %s", iso_utc(measurement.ts), exc)
            except Exception:  # noqa: BLE001 - ein Wert darf den Puffer nicht blockieren
                log.exception("Gepufferter Wert von %s verworfen", iso_utc(measurement.ts))
            self.buffer.popleft()
        log.info("%d gepufferte Werte nachgeliefert, noch %d im Puffer", sent, len(self.buffer))

    def run_forever(self, stop: threading.Event, interval_s: float) -> None:
        """Misst alle ``interval_s`` Sekunden, bis ``stop`` gesetzt wird."""
        next_run = time.monotonic()
        while not stop.is_set():
            next_run += interval_s
            try:
                if self.run_once():
                    # Die halbe restliche Zeit nachliefern, damit die nächste Messung
                    # pünktlich kommt. Mindestens ein Zehntel des Takts, sonst würde
                    # der Puffer bei kurzem Takt nie leer (eine Messung dauert 0,8 s).
                    rest = next_run - time.monotonic()
                    self.flush_buffer(stop, budget_s=max(rest / 2, interval_s / 10))
            except Exception:  # noqa: BLE001 - Sicherheitsnetz, Dienst läuft weiter
                log.exception("Unerwarteter Fehler im Messzyklus")
            delay = next_run - time.monotonic()
            if delay < 0:  # Zyklus hat länger gedauert: nicht nachholen
                next_run = time.monotonic()
                delay = 0
            stop.wait(delay)

    # ----------------------------------------------------------------- intern

    def _check_clock(self) -> bool:
        """Erst senden, wenn die Uhr einmal per NTP gestellt war.

        Danach wird nicht mehr geprüft: Fällt NTP später aus, geht die Uhr
        trotzdem weiter, und die Messwerte sollen weiter ankommen.
        """
        if self.clock_ok:
            return True
        synced = self._clock_synced()
        if synced:
            log.info("Uhr ist per NTP gestellt, Messwerte werden gesendet")
        elif self.clock_ok is None:
            log.warning("Uhr noch nicht per NTP gestellt: es wird gemessen, aber noch nicht gesendet")
        self.clock_ok = synced
        return synced

    def _on_push_error(self, error: Exception | None, measurement: Measurement) -> None:
        if error is None:  # gar nicht gesendet; ohne richtige Uhrzeit auch nicht puffern
            self.last_error = "Uhr noch nicht per NTP gestellt"
            return
        self.last_error = str(error)
        if isinstance(error, ServerUnavailable):
            self.buffer.append(measurement)
            log.warning(
                "Push fehlgeschlagen: %s – Wert gepuffert (%d von höchstens %d)",
                error,
                len(self.buffer),
                self.buffer.maxlen,
            )
        elif isinstance(error, AuthError):
            log.error(
                "Server verweigert den Zugang (%s). API_TOKEN oder SENSOR_ID in der .env ist falsch. "
                "Der Wert wird nicht gepuffert.",
                error,
            )
        elif isinstance(error, PushError):
            log.error("Messwert %.2f °C verworfen: %s", measurement.temp_c, error)
        else:
            log.error("Unerwarteter Fehler beim Senden, Wert verworfen", exc_info=error)

    def _on_sensor_error(self, exc: Exception) -> None:
        with self._lock:
            self.sensor_failures += 1
            if isinstance(exc, SensorError):
                log.warning("Messwert verworfen (%d. Fehler in Folge): %s", self.sensor_failures, exc)
            else:
                log.error("Unerwarteter Sensorfehler", exc_info=exc)
            if self.sensor_failures == self.sensor_fail_limit:
                log.error(
                    "Sensor ausgefallen (%d ungültige Messungen in Folge) – gelbe LED blinkt",
                    self.sensor_failures,
                )
            self._apply_leds()

    def _set_decided_by(self, who: str) -> None:
        if who == self._decided_by:
            return
        self._decided_by = who
        if who == "server":
            log.info("Server hat die Messung angenommen, er steuert die LEDs")
        elif self.thresholds:
            log.warning("Pi entscheidet selbst: %s", _describe(self.thresholds))
        else:
            log.warning("Noch nie Grenzwerte vom Server bekommen, LEDs bleiben aus")

    def _update_thresholds(self, new: Thresholds) -> None:
        if new == self.thresholds:
            return
        log.info("Neue Grenzwerte vom Server: %s", _describe(new))
        self.thresholds = new

    def _persist(self) -> None:
        """Schreibt nur, wenn sich etwas geändert hat (schont die SD-Karte).
        Schlägt das Schreiben fehl, wird es im nächsten Zyklus wiederholt."""
        current = SavedState(thresholds=self.thresholds, level=self.level)
        if current != self._persisted and self.store.save(current):
            self._persisted = current

    def _apply_leds(self) -> None:
        for name, state in self.led_states().items():
            try:
                self.leds[name].set(state)
            except Exception:  # noqa: BLE001
                log.exception("LED %s konnte nicht auf %s geschaltet werden", name, state)


def _describe(t: Thresholds) -> str:
    text = f"Alarm ab {t.threshold_c:.2f} °C (Ende unter {t.clear_c:.2f})"
    if t.warn_c is None:
        return f"{text}, Warngrenze unbekannt"
    return f"{text}, Warnung ab {t.warn_c:.2f} °C (Ende unter {t.warn_clear_c:.2f})"
