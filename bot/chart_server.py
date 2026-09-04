from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bot.chart_encode import encode_tick
from bot.hub import Hub

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CHART_DIR = Path(__file__).resolve().parent.parent / "chart"
BROADCAST_INTERVAL_S = 0.2

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_loopback(host: str) -> str:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f"refusing to bind non-loopback host: {host!r}")
    return host


def _ws_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _encode_ws_frame(opcode: int, body: bytes) -> bytes:
    length = len(body)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN=1
    if length < 126:
        header.append(length)
    elif length < 0x10000:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + body


def _encode_ws_text_frame(payload: str) -> bytes:
    return _encode_ws_frame(0x1, payload.encode("utf-8"))


def _read_client_frame(conn: socket.socket) -> tuple[int, bytes] | None:
    """Read one client (masked) WS frame. Returns (opcode, payload) or None on close/error."""
    header = conn.recv(2)
    if len(header) < 2:
        return None
    b0, b1 = header[0], header[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = conn.recv(2)
        if len(ext) < 2:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = conn.recv(8)
        if len(ext) < 8:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask_key = b""
    if masked:
        mask_key = conn.recv(4)
        if len(mask_key) < 4:
            return None
    payload = b""
    remaining = length
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return None
        payload += chunk
        remaining -= len(chunk)
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        pass  # silence default stderr logging

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._serve_index()
        elif path == "/kline":
            self._serve_kline()
        elif path == "/ws":
            self._serve_ws()
        else:
            self.send_error(404, "not found")

    def _serve_index(self) -> None:
        try:
            body = (CHART_DIR / "index.html").read_bytes()
        except OSError:
            self.send_error(404, "index not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_kline(self) -> None:
        server: ChartServer = self.server.chart
        end = int(time.time() * 1000)
        start = end - 20 * 15 * 60 * 1000
        data = server.client.kline(
            server.symbol,
            interval="Min15",
            start=start,
            end=end,
        )
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_ws(self) -> None:
        client_key = self.headers.get("Sec-WebSocket-Key")
        if not client_key:
            self.send_error(400, "missing Sec-WebSocket-Key")
            return
        accept = _ws_accept_key(client_key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        self.connection.sendall(response.encode("utf-8"))

        server: ChartServer = self.server.chart
        conn = self.connection
        server.add_client(conn)
        try:
            conn.settimeout(1.0)
            while not server.stopped:
                try:
                    frame = _read_client_frame(conn)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # close
                    try:
                        conn.sendall(_encode_ws_frame(0x8, payload[:2]))
                    except OSError:
                        pass
                    break
                if opcode == 0x9:  # ping -> pong
                    try:
                        conn.sendall(_encode_ws_frame(0xA, payload))
                    except OSError:
                        break
                # data frames (0x0/0x1/0x2) and pong (0xA) are ignored: this
                # is a one-way tick stream, we don't accept client input.
        finally:
            server.remove_client(conn)


class ChartServer:
    def __init__(self, *, hub: Hub, client: Any, host: str = "127.0.0.1", port: int = 8765, symbol: str | None = None):
        require_loopback(host)
        self.hub = hub
        self.client = client
        self.host = host
        self.requested_port = port
        self.symbol = symbol or (hub.symbol or "BTC_USDT")
        self._httpd: ThreadingHTTPServer | None = None
        self.port: int | None = None
        self.clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self.stopped = False
        self._broadcast_thread: threading.Thread | None = None

    def start(self) -> None:
        require_loopback(self.host)
        try:
            httpd = ThreadingHTTPServer((self.host, self.requested_port), _Handler)
        except OSError as exc:
            raise OSError(
                f"chart server could not bind {self.host}:{self.requested_port}: {exc}"
            ) from exc
        httpd.chart = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self.stopped = False

        self._broadcast_thread = threading.Thread(target=self._broadcast_loop, daemon=True)
        self._broadcast_thread.start()

    def serve_forever(self) -> None:
        if self._httpd is None:
            raise RuntimeError("call start() before serve_forever()")
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self.stopped = True
        with self._clients_lock:
            clients = list(self.clients)
            self.clients.clear()
        for conn in clients:
            try:
                conn.close()
            except OSError:
                pass
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def add_client(self, conn: socket.socket) -> None:
        with self._clients_lock:
            self.clients.append(conn)

    def remove_client(self, conn: socket.socket) -> None:
        with self._clients_lock:
            if conn in self.clients:
                self.clients.remove(conn)

    def _broadcast_loop(self) -> None:
        while not self.stopped:
            time.sleep(BROADCAST_INTERVAL_S)
            if self.stopped:
                return
            payload = json.dumps(encode_tick(self.hub))
            frame = _encode_ws_text_frame(payload)
            with self._clients_lock:
                targets = list(self.clients)
            dead = []
            for conn in targets:
                try:
                    conn.sendall(frame)
                except OSError:
                    dead.append(conn)
            if dead:
                with self._clients_lock:
                    for conn in dead:
                        if conn in self.clients:
                            self.clients.remove(conn)
