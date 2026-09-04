"""Per-process localhost write authorization and arming challenges."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path
from typing import Mapping


class LocalSecurity:
    def __init__(self, port: int, vite_origins: tuple[str, ...] = ()) -> None:
        self.token = secrets.token_urlsafe(32)
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {
            f"http://127.0.0.1:{port}", f"http://localhost:{port}",
            "http://127.0.0.1:5173", "http://localhost:5173", *vite_origins,
        }
        self._challenges: dict[str, float] = {}
        self._lock = threading.Lock()

    def valid_host(self, headers: Mapping[str, str]) -> bool:
        return str(headers.get("Host") or "").lower() in self.allowed_hosts

    def valid_token(self, headers: Mapping[str, str]) -> bool:
        supplied = str(headers.get("X-Terminal-Token") or "")
        return self.valid_host(headers) and bool(supplied) and secrets.compare_digest(supplied, self.token)

    def validate_write(self, headers: Mapping[str, str]) -> tuple[int, str] | None:
        host = str(headers.get("Host") or "").lower()
        origin = str(headers.get("Origin") or "").lower()
        content_type = str(headers.get("Content-Type") or "").lower().split(";", 1)[0].strip()
        supplied = str(headers.get("X-Terminal-Token") or "")
        if host not in self.allowed_hosts:
            return 403, "invalid Host"
        if origin not in self.allowed_origins:
            return 403, "invalid Origin"
        if content_type != "application/json":
            return 415, "Content-Type must be application/json"
        if not supplied or not secrets.compare_digest(supplied, self.token):
            return 403, "invalid terminal token; reload the terminal"
        return None

    def issue_arm_challenge(self, ttl_seconds: float = 60.0) -> str:
        nonce = secrets.token_urlsafe(18)
        signature = hmac.new(self.token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        challenge = f"{nonce}.{signature}"
        now = time.monotonic()
        with self._lock:
            self._challenges = {value: expiry for value, expiry in self._challenges.items() if expiry > now}
            self._challenges[challenge] = now + ttl_seconds
        return challenge

    def consume_arm_challenge(self, challenge: str) -> bool:
        now = time.monotonic()
        with self._lock:
            expiry = self._challenges.pop(challenge, None)
        if expiry is None or expiry < now:
            return False
        try:
            nonce, signature = challenge.rsplit(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self.token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def safe_static_path(root: Path, request_path: str) -> Path | None:
    target = (root / request_path.lstrip("/")).resolve()
    resolved_root = root.resolve()
    return target if target.is_relative_to(resolved_root) and target.is_file() else None
