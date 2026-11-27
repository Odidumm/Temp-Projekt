"""Zustand, der Neustarts überstehen muss: die zuletzt vom Server erhaltenen
Grenzwerte und die letzte automatische Stufe ok/warn/alarm (für die Hysterese)."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


LEVELS = ("ok", "warn", "alarm")


@dataclass(frozen=True)
class Thresholds:
    threshold_c: float  # Alarm (rote LED) ab dieser Temperatur
    clear_c: float  # Alarm endet unter dieser Temperatur
    # Warnung (gelbe LED) ab dieser Temperatur. Steht nicht im API-Vertrag und
    # ist None, solange der Server sie nicht mitschickt.
    warn_c: float | None = None

    @property
    def warn_clear_c(self) -> float | None:
        """Warnung endet unter dieser Temperatur (gleicher Abstand wie beim Alarm)."""
        if self.warn_c is None:
            return None
        return round(self.warn_c - (self.threshold_c - self.clear_c), 4)  # gerundet wie am Server

    def is_valid(self) -> bool:
        return (
            math.isfinite(self.threshold_c)
            and math.isfinite(self.clear_c)
            and self.clear_c <= self.threshold_c
            and (self.warn_c is None or (math.isfinite(self.warn_c) and self.warn_c <= self.threshold_c))
        )


@dataclass(frozen=True)
class SavedState:
    thresholds: Thresholds | None = None
    # Letzte Stufe der LEDs. Wird gespeichert, damit ein Alarm nach einem
    # Neustart nicht erlischt, solange die Temperatur noch im Hysteresebereich
    # liegt. Gilt nur zusammen mit Grenzwerten.
    level: str = "ok"


class StateStore:
    """Speichert den Zustand als JSON-Datei.

    Geschrieben wird zuerst in eine temporäre Datei im selben Verzeichnis,
    die dann per ``os.replace`` umbenannt wird. Ein Stromausfall mitten im
    Schreiben hinterlässt so entweder die alte oder die neue Datei, nie eine
    halbe.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> SavedState:
        """Liest den Zustand. Fehlt die Datei oder ist sie kaputt, gibt es
        Standardwerte (keine Grenzen, Stufe ok)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SavedState()
        except (OSError, ValueError) as exc:
            log.warning("Gespeicherter Zustand in %s unbrauchbar (%s), ignoriere ihn", self.path, exc)
            return SavedState()
        if not isinstance(data, dict):
            log.warning("Gespeicherter Zustand in %s unbrauchbar, ignoriere ihn", self.path)
            return SavedState()

        if "threshold_c" not in data and "clear_c" not in data:
            return SavedState()
        try:
            warn_c = data.get("warn_c")
            thresholds = Thresholds(
                float(data["threshold_c"]),
                float(data["clear_c"]),
                None if warn_c is None else float(warn_c),
            )
        except (KeyError, TypeError, ValueError):
            thresholds = None
        if thresholds is None or not thresholds.is_valid():
            log.warning("Gespeicherte Grenzwerte in %s ungültig, ignoriere sie", self.path)
            return SavedState()

        level = data.get("level", "ok")
        if level not in LEVELS:
            level = "ok"
        return SavedState(thresholds=thresholds, level=level)

    def save(self, state: SavedState) -> bool:
        """Speichert den Zustand. Gibt False zurück, wenn das nicht klappt
        (wird geloggt, der Dienst läuft weiter)."""
        data: dict = {}
        if state.thresholds is not None:
            data["threshold_c"] = state.thresholds.threshold_c
            data["clear_c"] = state.thresholds.clear_c
            if state.thresholds.warn_c is not None:
                data["warn_c"] = state.thresholds.warn_c
        data["level"] = state.level

        tmp_name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
            tmp_name = None
            _fsync_dir(self.path.parent)
            return True
        except OSError as exc:
            log.error("Zustand konnte nicht nach %s gespeichert werden: %s", self.path, exc)
            return False
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
