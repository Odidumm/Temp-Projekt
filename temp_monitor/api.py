"""Lokale REST-API (Flask)."""

from __future__ import annotations

import hmac

from flask import Flask, request
from werkzeug.exceptions import HTTPException

from .controller import LED_NAMES, Controller


def _authorized(header: str | None, token: str) -> bool:
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    # compare_digest: Vergleich in konstanter Zeit, verrät nichts über das Token.
    return hmac.compare_digest(value.strip().encode(), token.encode())


def create_app(controller: Controller, local_token: str) -> Flask:
    if not local_token:
        raise ValueError("LOCAL_TOKEN darf nicht leer sein")

    app = Flask(__name__)
    app.json.sort_keys = False

    @app.get("/health")
    def health():
        return controller.health()

    @app.get("/temperature")
    def temperature():
        data = controller.temperature()
        if data is None:
            return {"error": "Noch kein gültiger Messwert"}, 503
        return data

    @app.post("/led")
    def led():
        if not _authorized(request.headers.get("Authorization"), local_token):
            return {"error": "Nicht autorisiert"}, 401, {"WWW-Authenticate": "Bearer"}
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        state, led = body.get("state"), body.get("led")
        if state not in ("on", "off", "auto") or led not in (None, *LED_NAMES):
            return {
                "error": 'Erwartet JSON {"state": "on" | "off" | "auto", "led": "red" | "yellow"}'
                " (ohne led: beide LEDs)"
            }, 400
        return controller.set_led_mode(state, led)

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        return {"error": exc.name}, exc.code

    return app
