"""Sensorzugriff hinter einer kleinen Schnittstelle.

Jeder Sensor hat eine Methode ``read_celsius()``. Sie liefert einen
plausiblen Messwert in °C oder wirft ``SensorError``. Ungültige Werte
(CRC-Fehler, 85-°C-Power-on-Wert usw.) kommen gar nicht erst heraus.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Iterable, Protocol, Union

W1_DEVICES = Path("/sys/bus/w1/devices")

# Der DS18B20 meldet nach dem Einschalten bzw. nach einer unterbrochenen
# Wandlung 85,000 °C. Ein echter Messwert von genau 85 °C ist im Serverraum
# nicht realistisch, deshalb wird er immer verworfen.
POWER_ON_RAW = 85000

# Bereich, den der Server annimmt (pi-api.txt). Der DS18B20 selbst kann bis -55 °C,
# so kalt ist es im Serverraum aber nur bei einem Defekt.
MIN_C = -40.0
MAX_C = 125.0


class SensorError(Exception):
    """Es gibt keinen gültigen Messwert."""


class Sensor(Protocol):
    def read_celsius(self) -> float:
        """Gültigen Messwert in °C liefern oder SensorError werfen."""
        ...


def parse_w1_slave(text: str) -> float:
    """Wertet den Inhalt von ``/sys/bus/w1/devices/28-*/w1_slave`` aus.

    Beispiel für eine gültige Ausgabe::

        72 01 4b 46 7f ff 0e 10 57 : crc=57 YES
        72 01 4b 46 7f ff 0e 10 57 t=23125
    """
    lines = text.strip().splitlines()
    if len(lines) < 2:
        raise SensorError("Unvollständige Sensordaten")

    crc_line, data_line = lines[0].strip(), lines[1].strip()
    if not crc_line.endswith("YES"):
        raise SensorError(f"CRC-Fehler: {crc_line!r}")

    # Liest der Kernel nur Nullen (z. B. Datenleitung auf GND gezogen), ist
    # auch die CRC 00 und der Kernel meldet trotzdem YES. Das ist kein Messwert.
    scratchpad = crc_line.split(":", 1)[0].split()
    if scratchpad and all(byte == "00" for byte in scratchpad):
        raise SensorError("Sensor liefert nur Nullen (Verkabelung prüfen)")

    pos = data_line.find("t=")
    if pos == -1:
        raise SensorError(f"Kein Temperaturwert in {data_line!r}")
    try:
        raw = int(data_line[pos + 2 :].strip())
    except ValueError:
        raise SensorError(f"Temperaturwert nicht lesbar: {data_line!r}") from None

    if raw == POWER_ON_RAW:
        raise SensorError("85 °C Power-on-Wert verworfen")

    celsius = raw / 1000.0
    if not MIN_C <= celsius <= MAX_C:
        raise SensorError(f"Messwert {celsius} °C außerhalb des Messbereichs")
    return celsius


class DS18B20Sensor:
    """DS18B20 über den 1-Wire-Treiber des Kernels (sysfs)."""

    def __init__(
        self,
        device_id: str | None = None,
        base_dir: Path = W1_DEVICES,
        attempts: int = 2,
        retry_delay_s: float = 0.2,
    ) -> None:
        self.device_id = device_id
        self.base_dir = Path(base_dir)
        self.attempts = max(1, attempts)
        self.retry_delay_s = retry_delay_s

    def device_file(self) -> Path:
        """Pfad zur w1_slave-Datei. Wird bei jeder Messung neu gesucht,
        damit ein später angesteckter Sensor ohne Neustart gefunden wird."""
        if self.device_id:
            return self.base_dir / self.device_id / "w1_slave"
        candidates = sorted(self.base_dir.glob("28-*/w1_slave"))
        if not candidates:
            raise SensorError(f"Kein DS18B20 (28-*) unter {self.base_dir} gefunden")
        return candidates[0]

    def read_celsius(self) -> float:
        error = SensorError("Keine Messung durchgeführt")
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(self.retry_delay_s)
            try:
                text = self.device_file().read_text(encoding="ascii", errors="replace")
                return parse_w1_slave(text)
            except SensorError as exc:
                error = exc
            except OSError as exc:
                error = SensorError(f"Sensor nicht lesbar: {exc}")
        raise error


Reading = Union[float, BaseException]


class FakeSensor:
    """Sensor-Ersatz für Tests und für den Betrieb ohne Hardware.

    ``values`` werden der Reihe nach geliefert; ist ein Eintrag eine
    Exception, wird sie geworfen. Danach bleibt der zuletzt gesetzte Wert
    (bzw. ``default``) stehen. Mit ``set()`` kann man den Wert jederzeit ändern.
    """

    def __init__(self, default: Reading = 22.0, values: Iterable[Reading] = ()) -> None:
        self.current: Reading = default
        self.queue: deque[Reading] = deque(values)
        self.reads = 0

    def set(self, value: Reading) -> None:
        self.queue.clear()
        self.current = value

    def read_celsius(self) -> float:
        self.reads += 1
        if self.queue:
            self.current = self.queue.popleft()
        if isinstance(self.current, BaseException):
            raise self.current
        return float(self.current)


class FileFakeSensor:
    """Fake-Sensor, der den Wert bei jeder Messung aus einer Textdatei liest.

    So lässt sich die Temperatur im laufenden Betrieb ändern::

        echo 31.5 > /tmp/fake_temp
        rm /tmp/fake_temp     # simuliert einen Sensorausfall
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read_celsius(self) -> float:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SensorError(f"Fake-Sensor-Datei nicht lesbar: {exc}") from None
        try:
            celsius = float(text)
        except ValueError:
            raise SensorError(f"Kein Zahlenwert in {self.path}: {text!r}") from None
        if not MIN_C <= celsius <= MAX_C:
            raise SensorError(f"Messwert {celsius} °C außerhalb des Messbereichs")
        return celsius
