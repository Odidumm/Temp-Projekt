"""Ansteuerung einer LED (rot = Alarm, gelb = Warnung).

Drei Zustände: ``on``, ``off`` und ``blink`` (nur gelb: Sensor ausgefallen).
"""

from __future__ import annotations

from typing import Protocol

LED_STATES = ("on", "off", "blink")


class Led(Protocol):
    state: str

    def set(self, state: str) -> None: ...

    def close(self) -> None: ...


class GpioLed:
    """LED an einem GPIO-Pin (BCM-Nummer) über gpiozero.

    Ohne Hardware kann man mit ``GPIOZERO_PIN_FACTORY=mock`` testen.
    """

    def __init__(self, pin: int, blink_on_s: float = 0.5, blink_off_s: float = 0.5) -> None:
        from gpiozero import LED  # erst hier importieren, damit Tests ohne GPIO laufen
        from gpiozero.pins.mock import MockFactory

        self._led = LED(pin)
        # True bei GPIOZERO_PIN_FACTORY=mock: dann schaltet keine echte LED
        self.simulated = isinstance(self._led.pin_factory, MockFactory)
        self._blink = (blink_on_s, blink_off_s)
        self.state = "off"
        self._led.off()

    def set(self, state: str) -> None:
        if state not in LED_STATES:
            raise ValueError(f"Unbekannter LED-Zustand: {state!r}")
        if state == self.state:
            return  # blink() nicht neu starten, sonst ruckelt das Blinken
        if state == "on":
            self._led.on()
        elif state == "off":
            self._led.off()
        else:
            on_s, off_s = self._blink
            self._led.blink(on_time=on_s, off_time=off_s, background=True)
        self.state = state

    def close(self) -> None:
        self._led.off()
        self._led.close()


class FakeLed:
    """LED-Ersatz für Tests: merkt sich den Zustand und den Verlauf."""

    simulated = True

    def __init__(self) -> None:
        self.state = "off"
        self.history: list[str] = []
        self.closed = False

    def set(self, state: str) -> None:
        if state not in LED_STATES:
            raise ValueError(f"Unbekannter LED-Zustand: {state!r}")
        if state != self.state:
            self.history.append(state)
        self.state = state

    def close(self) -> None:
        self.closed = True
