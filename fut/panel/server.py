"""Loopback-only, GET-only HTTP server for the futures paper panel."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from bot.chart_server import _origin_is_loopback, require_loopback
from fut.panel.cache import PanelCache
from fut.panel.reader import PanelDbBusy, PanelDbMissing, PanelReader
from fut.panel.state import build_events, build_state

_LOOPBACK_HOST_PREFIXES = ("127.0.0.1", "localhost", "[::1]")


def _host_is_loopback(host: str | None) -> bool:
    host = (host or "").strip().lower()
    return any(host == p or host.startswith(p + ":") for p in _LOOPBACK_HOST_PREFIXES)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json", extra)

    def _refuse_method(self) -> None:
        self._json(405, {"error": "only GET"}, {"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse_method

    def do_GET(self) -> None:
        panel: PanelServer = self.server.panel
        parts = urlsplit(self.path)
        if parts.path == "/":
            try:
                self._send(200, panel.index_path.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._json(404, {"error": "index not found"})
            return
        if parts.path not in ("/api/state", "/api/events"):
            self._json(404, {"error": "not found"})
            return
        # A page on another site (or a DNS-rebound name) must not read the local ledger.
        origin = self.headers.get("Origin")
        if (origin is not None and not _origin_is_loopback(origin)) or not _host_is_loopback(self.headers.get("Host")):
            self._json(403, {"error": "forbidden origin"})
            return
        try:
            if parts.path == "/api/state":
                self._send(200, panel.state_bytes, "application/json")
                return
            raw = parse_qs(parts.query).get("after", [None])[0]
            after = None
            if raw is not None:
                if not raw.isdigit():
                    self._json(400, {"error": "after must be a non-negative integer"})
                    return
                after = int(raw)
            self._json(200, build_events(panel.reader, after))
        except PanelDbMissing:
            empty = {"estado": "sem_banco"}
            self._json(200, empty if parts.path == "/api/state" else {**empty, "events": [], "last_id": 0})
        except PanelDbBusy:
            self._json(503, {"error": "database busy"}, {"Retry-After": "1"})


class PanelServer:
    def __init__(self, *, reader: PanelReader, index_path: Path, host: str = "127.0.0.1", port: int = 8766,
                 clock_ms: Callable[[], int] | None = None, max_hold_s: float = 300.0):
        self.host = require_loopback(host)
        self.reader = reader
        self.cache = PanelCache(reader)
        self.index_path = Path(index_path)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_hold_s = max_hold_s
        self.requested_port = port
        self.port: int | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._refresh_stop = threading.Event()
        self._state_lock = threading.Lock()
        self.state_bytes = json.dumps({"estado": "carregando"}, ensure_ascii=False).encode("utf-8")

    def refresh_now(self) -> None:
        try:
            self.cache.refresh(now_ms=self.clock_ms())
            state = build_state(self.cache, now_ms=self.clock_ms(), max_hold_s=self.max_hold_s)
            body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        except PanelDbMissing:
            body = json.dumps({"estado": "sem_banco"}, ensure_ascii=False).encode("utf-8")
        except PanelDbBusy:
            return
        with self._state_lock:
            self.state_bytes = body

    def _refresh_loop(self) -> None:
        while not self._refresh_stop.is_set():
            self.refresh_now()
            self._refresh_stop.wait(1.0)

    def _start_refresher(self) -> None:
        if self._refresh_thread is None:
            self._refresh_stop.clear()
            self._refresh_thread = threading.Thread(target=self._refresh_loop, name="fut-panel-refresh", daemon=True)
            self._refresh_thread.start()

    def _bind(self) -> ThreadingHTTPServer:
        if self._httpd is None:
            self._httpd = ThreadingHTTPServer((self.host, self.requested_port), _Handler)
            self._httpd.daemon_threads = True
            self._httpd.panel = self
            self.port = self._httpd.server_address[1]
        return self._httpd

    def start(self) -> None:
        httpd = self._bind()
        self._start_refresher()
        self._thread = threading.Thread(target=httpd.serve_forever, name="fut-panel", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._start_refresher()
        self._bind().serve_forever()

    def shutdown(self) -> None:
        self._refresh_stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._refresh_thread is not None and self._refresh_thread is not threading.current_thread():
            self._refresh_thread.join(timeout=2)
        self._refresh_thread = None
