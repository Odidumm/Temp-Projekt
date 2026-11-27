"""HTTP-Server für die lokale API (werkzeug), abgesichert gegen hängende
Verbindungen: jede Verbindung hat ein Timeout, und es gibt nur eine begrenzte
Zahl gleichzeitiger Verbindungen. Sonst könnte jemand im LAN mit vielen offenen,
stillen Verbindungen alle Dateideskriptoren belegen und damit auch das Messen
und Senden lahmlegen."""

from __future__ import annotations

import socket
import threading

from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler


class _RequestHandler(WSGIRequestHandler):
    timeout = 10.0  # Sekunden ohne Daten -> Verbindung wird geschlossen


class LimitedWSGIServer(ThreadedWSGIServer):
    def __init__(self, *args, max_connections: int = 32, **kwargs) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # zu viele Verbindungen: sofort schließen
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def create_server(
    app,
    host: str,
    port: int,
    max_connections: int = 32,
    timeout_s: float = 10.0,
) -> LimitedWSGIServer:
    """Erzeugt den Server. Ist der Port belegt, kommt ein OSError
    (werkzeug selbst würde sonst sys.exit(1) aufrufen)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.create_server((host, port), family=family)
    handler = type("RequestHandler", (_RequestHandler,), {"timeout": timeout_s})
    try:
        # werkzeug übernimmt ein Duplikat des Sockets (socket.fromfd)
        return LimitedWSGIServer(
            host, port, app, handler=handler, fd=sock.fileno(), max_connections=max_connections
        )
    finally:
        sock.close()
