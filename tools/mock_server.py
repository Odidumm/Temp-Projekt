"""Mini-Ersatz für den zentralen Server, zum Ausprobieren ohne NAS.

Verhält sich wie der Ingest-Port des echten Servers (Vertrag: pi-api.txt):
POST /api/readings und GET /healthz, gleiche Antworten und Fehlermeldungen.
Aufruf:

    .venv/bin/python tools/mock_server.py --port 9000 --token test --warn 27 --alarm 30 --clear 28

Server-Ausfall simulieren: einfach mit Strg+C beenden, oder ``--http-status 500``.
``--http-status 403`` antwortet wie Cloudflare, wenn es den User-Agent blockt.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Rejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_ts(value: object) -> float:
    try:
        dt = datetime.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise Rejected(400, "ts muss ein ISO-8601-Zeitstempel sein") from None
    if dt.tzinfo is None:
        raise Rejected(400, "ts braucht eine Zeitzone, z. B. 2026-11-20T10:30:00Z")
    return dt.timestamp()


class Sensor:
    """Zustand eines Sensors mit Hysterese für Alarm und Warnung, wie am Server."""

    def __init__(self) -> None:
        self.in_warn = self.in_alarm = False
        self.last_ts: float | None = None

    def update(self, temp: float, ts: float, warn: float, alarm: float, clear: float) -> bool:
        """False bei einem nachgereichten älteren Wert: Er ändert den Zustand nicht."""
        if self.last_ts is not None and ts < self.last_ts:
            return False
        warn_clear = round(warn - (alarm - clear), 4)
        self.in_alarm = temp >= (clear if self.in_alarm else alarm)
        self.in_warn = temp >= (warn_clear if self.in_warn else warn)
        self.last_ts = ts
        return True

    @property
    def status(self) -> str:
        return "alarm" if self.in_alarm else "warn" if self.in_warn else "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--sensor", default="rack-a", help="sensor_id, zu der das Token gehört")
    parser.add_argument("--token", default="test", help="erwartetes API_TOKEN")
    parser.add_argument("--warn", type=float, default=27.0, help="Warngrenze")
    parser.add_argument("--alarm", type=float, default=30.0, help="Alarmgrenze (threshold_c)")
    parser.add_argument("--clear", type=float, default=28.0, help="Alarm-Ende (clear_c)")
    parser.add_argument("--send-warn-c", action="store_true", help="warn_c mitschicken (nicht im Vertrag)")
    parser.add_argument("--http-status", type=int, default=200, help="z. B. 500 oder 403, um Fehler zu simulieren")
    args = parser.parse_args()

    sensor = Sensor()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict | str) -> None:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain" if isinstance(payload, str) else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _reading(self, raw: bytes) -> dict:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise Rejected(401, "Authorization: Bearer <Token> fehlt")
            if auth != f"Bearer {args.token}":
                raise Rejected(401, "Ungültiger Token")
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise Rejected(400, "Body muss ein JSON-Objekt sein")
            temp = body.get("temp_c")
            if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not math.isfinite(temp):
                raise Rejected(400, "temp_c muss eine Zahl sein")
            if not -40 <= temp <= 125:
                raise Rejected(400, "temp_c außerhalb von -40 bis 125 °C")
            ts = parse_ts(body.get("ts"))
            if ts > time.time() + 300:
                raise Rejected(400, "ts liegt in der Zukunft – Uhrzeit des Pi prüfen (NTP)")
            if body.get("sensor_id") != args.sensor:
                raise Rejected(401, "Token gehört nicht zu diesem Sensor")
            if args.http_status != 200:
                raise Rejected(args.http_status, "simulierter Fehler")

            current = sensor.update(temp, ts, args.warn, args.alarm, args.clear)
            note = "" if current else "  (nachgeliefert, ändert nichts)"
            print(f"{body['ts']}  {args.sensor}: {temp:.2f} °C -> status={sensor.status}{note}", flush=True)
            reply = {
                "status": sensor.status,
                "led": "on" if sensor.in_alarm else "off",
                "threshold_c": args.alarm,
                "clear_c": args.clear,
            }
            return {**reply, "warn_c": args.warn} if args.send_warn_c else reply

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path != "/api/readings":
                self._send(404, {"error": "Nicht gefunden"})
                return
            try:
                self._send(200, self._reading(raw))
            except Rejected as exc:
                print(f"HTTP {exc.status}: {exc}", flush=True)
                self._send(exc.status, "error code: 1010\n" if exc.status == 403 else {"error": str(exc)})

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._send(200, {"status": "ok", "role": "ingest"})
            else:
                self._send(404, {"error": "Nicht gefunden"})

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"Mock-Server auf http://{args.host}:{args.port} "
        f"(Sensor {args.sensor!r}, Token {args.token!r}, Strg+C beendet)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
