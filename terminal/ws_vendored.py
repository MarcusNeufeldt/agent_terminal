"""Minimal Kraken Futures websocket client.

Vendored from F:\\explore\\kraken-futures-cli (kraken_futures_cli/websocket.py):
handshake, frame codec, and JSON send/recv only. Public feeds need no auth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketEndpoint:
    def __init__(self, host: str, port: int, use_tls: bool, path: str):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.path = path


def websocket_endpoint_from_base_url(base_url: str) -> WebSocketEndpoint:
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"invalid base URL: {base_url}")
    use_tls = parsed.scheme in {"https", "wss"}
    port = parsed.port or (443 if use_tls else 80)
    return WebSocketEndpoint(host=host, port=port, use_tls=use_tls, path="/ws/v1")


def open_websocket(endpoint: WebSocketEndpoint, *, timeout: float) -> "WebSocketConnection":
    raw_socket = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
    raw_socket.settimeout(timeout)
    try:
        if endpoint.use_tls:
            wrapped = ssl.create_default_context().wrap_socket(
                raw_socket, server_hostname=endpoint.host
            )
        else:
            wrapped = raw_socket
    except OSError:
        raw_socket.close()
        raise

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {endpoint.path} HTTP/1.1\r\n"
        f"Host: {endpoint.host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: kraken-futures-terminal/0.1.0\r\n"
        "\r\n"
    )
    wrapped.sendall(request.encode("ascii"))
    headers, buffered = _read_http_headers(wrapped)
    _validate_handshake(headers, key)
    return WebSocketConnection(wrapped, buffered)


def _read_http_headers(sock: socket.socket) -> tuple[bytes, bytes]:
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake: connection closed")
        response += chunk
    headers, _, buffered = response.partition(b"\r\n\r\n")
    return headers, buffered


def _validate_handshake(headers: bytes, key: str) -> None:
    lines = headers.decode("iso-8859-1", errors="replace").split("\r\n")
    status = lines[0] if lines else ""
    if " 101 " not in status:
        raise ConnectionError(f"websocket handshake failed: {status or 'missing status'}")
    parsed = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            parsed[name.strip().lower()] = value.strip()
    expected = base64.b64encode(
        hashlib.sha1(f"{key}{WEBSOCKET_GUID}".encode("ascii")).digest()
    ).decode("ascii")
    if parsed.get("sec-websocket-accept") != expected:
        raise ConnectionError("websocket handshake: invalid accept header")


class WebSocketConnection:
    def __init__(self, sock: socket.socket, buffered: bytes = b""):
        self.sock = sock
        self.buffered = bytearray(buffered)

    def send_json(self, payload: dict) -> None:
        self.send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"), opcode=1)

    def recv_json(self) -> dict:
        raw = self.recv_message()
        return json.loads(raw)

    def recv_message(self) -> str:
        parts: list[bytes] = []
        message_opcode: int | None = None
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == 8:
                raise ConnectionError("websocket connection closed")
            if opcode == 9:
                self.send_frame(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode in {1, 2}:
                message_opcode = opcode
                parts = [payload]
            elif opcode == 0 and message_opcode is not None:
                parts.append(payload)
            else:
                raise ConnectionError(f"unsupported websocket frame opcode: {opcode}")
            if fin:
                return b"".join(parts).decode("utf-8")

    def recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self.recv_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.recv_exact(8))[0]
        mask = self.recv_exact(4) if masked else b""
        payload = self.recv_exact(length) if length else b""
        if masked:
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        return fin, opcode, payload

    def recv_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        if self.buffered:
            chunk = bytes(self.buffered[:size])
            del self.buffered[:size]
            chunks.append(chunk)
            size -= len(chunk)
        while size:
            chunk = self.sock.recv(size)
            if not chunk:
                raise ConnectionError("websocket connection closed unexpectedly")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def send_frame(self, payload: bytes, *, opcode: int) -> None:
        first = 0x80 | opcode
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length < 65536:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def close(self) -> None:
        try:
            self.send_frame(b"", opcode=8)
        except OSError:
            pass
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
