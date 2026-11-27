"""Einstiegspunkt: ``python -m temp_monitor [--env-file PFAD]``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from .api import create_app
from .client import ServerClient
from .config import Config, ConfigError, load_config
from .controller import Controller
from .sensor import DS18B20Sensor, FakeSensor, FileFakeSensor, Sensor
from .server import create_server
from .state import StateStore

log = logging.getLogger("temp_monitor")

EXIT_CONFIG_ERROR = 2  # systemd startet bei diesem Code nicht neu (RestartPreventExitStatus)

# Syslog-Prioritäten. journald liest sie aus einem Präfix wie "<4>" am
# Zeilenanfang; ohne Präfix landet alles als "info" im Journal und
# "journalctl -p warning" fände keine einzige Warnung.
_SYSLOG_PRIORITY = {
    logging.DEBUG: 7,
    logging.INFO: 6,
    logging.WARNING: 4,
    logging.ERROR: 3,
    logging.CRITICAL: 2,
}


class JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"<{_SYSLOG_PRIORITY.get(record.levelno, 6)}>{super().format(record)}"


def stdout_is_journal() -> bool:
    """True, wenn stdout direkt ins Journal geht (Start durch systemd)."""
    stream = os.environ.get("JOURNAL_STREAM", "")
    try:
        dev, ino = (int(part) for part in stream.split(":"))
        st = os.fstat(sys.stdout.fileno())
    except (ValueError, OSError):
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if stdout_is_journal():
        # journald setzt Zeitstempel selbst
        handler.setFormatter(JournalFormatter("%(levelname)s %(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    if level != "DEBUG":
        # Nicht jeden einzelnen HTTP-Request der lokalen API loggen.
        logging.getLogger("werkzeug").setLevel(logging.WARNING)


def build_sensor(cfg: Config) -> Sensor:
    if cfg.sensor_backend == "fake":
        if cfg.fake_temp_file:
            log.warning("Fake-Sensor aktiv: Werte aus %s", cfg.fake_temp_file)
            return FileFakeSensor(cfg.fake_temp_file)
        log.warning("Fake-Sensor aktiv: konstant %.2f °C", cfg.fake_temp_c)
        return FakeSensor(cfg.fake_temp_c)
    return DS18B20Sensor(device_id=cfg.w1_device_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="temp_monitor", description=__doc__)
    parser.add_argument(
        "--env-file",
        help="Pfad zur .env-Datei (Standard: .env im aktuellen Verzeichnis, falls vorhanden)",
    )
    args = parser.parse_args(argv)

    env_file = args.env_file or (".env" if Path(".env").is_file() else None)
    try:
        if env_file is not None:
            if not Path(env_file).is_file():
                raise ConfigError(f"Konfigurationsdatei nicht gefunden: {env_file}")
            # In die Umgebung übernehmen (ohne bestehende Variablen zu überschreiben),
            # damit z. B. auch GPIOZERO_PIN_FACTORY=mock aus der .env wirkt.
            load_dotenv(env_file, override=False)
        cfg = load_config(None, os.environ)
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Konfigurationsfehler: %s", exc)
        return EXIT_CONFIG_ERROR

    setup_logging(cfg.log_level)

    sensor = build_sensor(cfg)
    try:
        from .led import GpioLed
    except Exception as exc:  # noqa: BLE001
        log.error("gpiozero nicht verfügbar: %s", exc)
        return 1
    leds: list = []
    for name, pin in (("rot", cfg.led_red_pin), ("gelb", cfg.led_yellow_pin)):
        try:
            leds.append(GpioLed(pin))
        except Exception as exc:  # noqa: BLE001
            log.error("LED %s an GPIO %s nicht verfügbar: %s", name, pin, exc)
            for led in leds:
                led.close()
            return 1
    red, yellow = leds
    if red.simulated or yellow.simulated:
        log.warning(
            "LEDs werden nur simuliert (GPIOZERO_PIN_FACTORY=mock) – die echten LEDs an GPIO %s/%s schalten nicht",
            cfg.led_red_pin,
            cfg.led_yellow_pin,
        )

    client = ServerClient(cfg.base_url, cfg.api_token, timeout_s=cfg.request_timeout_s)
    controller = Controller(
        sensor=sensor,
        red=red,
        yellow=yellow,
        client=client,
        store=StateStore(cfg.state_file),
        sensor_id=cfg.sensor_id,
        sensor_fail_limit=cfg.sensor_fail_limit,
    )

    try:
        server = create_server(create_app(controller, cfg.local_token), cfg.local_host, cfg.local_port)
    except OSError as exc:
        log.error("Lokale API kann nicht auf %s:%s starten: %s", cfg.local_host, cfg.local_port, exc)
        client.close()
        for led in leds:
            led.close()
        return 1
    api_thread = threading.Thread(target=server.serve_forever, name="local-api", daemon=True)
    api_thread.start()

    stop = threading.Event()

    def request_stop(signum, _frame):
        log.info("Signal %s empfangen, beende ...", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log.info(
        "Gestartet: sensor_id=%s, Intervall %ss, Server %s, lokale API auf %s:%s, "
        "LED rot an GPIO %s, gelb an GPIO %s, Zustand in %s",
        cfg.sensor_id,
        cfg.interval_s,
        cfg.base_url,
        cfg.local_host,
        cfg.local_port,
        cfg.led_red_pin,
        cfg.led_yellow_pin,
        cfg.state_file,
    )
    try:
        controller.run_forever(stop, cfg.interval_s)
    finally:
        server.shutdown()
        server.server_close()
        client.close()
        for led in leds:
            led.close()
        log.info("Beendet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
