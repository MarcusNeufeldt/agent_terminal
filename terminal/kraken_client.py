"""Kraken Futures REST client.

Vendored from F:\\explore\\kraken-futures-cli (kraken_futures_cli/client.py),
kept verbatim in behavior so the CLI and this terminal share the same signing
logic. Only the User-Agent differs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib import error, parse, request

DEFAULT_LIVE_BASE_URL = "https://futures.kraken.com"
DEFAULT_DEMO_BASE_URL = "https://demo-futures.kraken.com"
TRADING_PATH_PREFIX = "/derivatives/api/v3"
AUTH_PATH_PREFIX = "/api/v3"
HISTORY_PATH_PREFIX = "/api/history/v3"
CHARTS_PATH_PREFIX = "/api/charts/v1"
USER_AGENT = "kraken-futures-terminal/0.1.0"


class KrakenFuturesError(Exception):
    """Base exception for client failures."""


class KrakenConfigError(KrakenFuturesError):
    """Raised when local configuration is missing or invalid."""


class KrakenHTTPError(KrakenFuturesError):
    """Raised when Kraken returns a non-2xx HTTP response."""

    def __init__(self, status: int, reason: str, payload: Any | None = None) -> None:
        self.status = status
        self.reason = reason
        self.payload = payload
        super().__init__(f"HTTP {status}: {reason}")


def load_env_file(path: str | os.PathLike[str]) -> bool:
    """Load KEY=VALUE pairs from a .env file without overriding the process env."""
    env_path = Path(path)
    if not env_path.exists():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def build_query(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ""

    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            pairs.extend((key, str(item)) for item in value)
        else:
            pairs.append((key, str(value)))

    return parse.urlencode(pairs, doseq=True, quote_via=parse.quote)


def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        raise KrakenConfigError("endpoint cannot be empty")
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    if endpoint.startswith(TRADING_PATH_PREFIX):
        endpoint = endpoint[len(TRADING_PATH_PREFIX) :] or "/"
    elif endpoint.startswith(AUTH_PATH_PREFIX):
        endpoint = endpoint[len(AUTH_PATH_PREFIX) :] or "/"
    return endpoint


def sign_authent(api_secret: str, post_data: str, nonce: str, endpoint_path: str) -> str:
    message = (post_data + nonce + endpoint_path).encode("utf-8")
    hashed = hashlib.sha256(message).digest()

    secret_text = api_secret.strip()
    padded_secret = secret_text + ("=" * (-len(secret_text) % 4))
    try:
        decoded_secret = base64.b64decode(padded_secret, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KrakenConfigError("KRAKEN_FUTURES_API_SECRET is not valid base64") from exc

    digest = hmac.new(decoded_secret, hashed, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("ascii")


@dataclass
class KrakenFuturesClient:
    api_key: str | None = None
    api_secret: str | None = None
    base_url: str = DEFAULT_LIVE_BASE_URL
    timeout: float = 10.0
    _last_nonce: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_env(
        cls,
        *,
        demo: bool = False,
        base_url: str | None = None,
        timeout: float = 10.0,
    ) -> "KrakenFuturesClient":
        env_name = os.getenv("KRAKEN_FUTURES_ENV", "live").strip().lower()
        use_demo = demo or env_name in {"demo", "sandbox", "test"}
        resolved_base_url = base_url or (
            DEFAULT_DEMO_BASE_URL if use_demo else DEFAULT_LIVE_BASE_URL
        )
        return cls(
            api_key=os.getenv("KRAKEN_FUTURES_API_KEY"),
            api_secret=os.getenv("KRAKEN_FUTURES_API_SECRET"),
            base_url=resolved_base_url,
            timeout=timeout,
        )

    @property
    def is_demo(self) -> bool:
        return "demo" in self.base_url

    def get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        normalized_endpoint = normalize_endpoint(endpoint)
        query = build_query(params)
        url = f"{self.base_url.rstrip('/')}{TRADING_PATH_PREFIX}{normalized_endpoint}"
        if query:
            url = f"{url}?{query}"

        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if private:
            headers.update(self._auth_headers(normalized_endpoint, query))

        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                return _decode_response(raw, content_type)
        except error.HTTPError as exc:
            payload = _decode_response(exc.read(), exc.headers.get("Content-Type", ""))
            raise KrakenHTTPError(exc.code, exc.reason, payload) from exc
        except error.URLError as exc:
            raise KrakenFuturesError(f"request failed: {exc.reason}") from exc

    def get_public_charts(
        self,
        symbol: str,
        resolution: str,
        *,
        tick_type: str = "trade",
        start: int | None = None,
        end: int | None = None,
    ) -> Any:
        """Public candle history: /api/charts/v1/{tick_type}/{symbol}/{resolution}."""
        resolution = resolution.strip().lower()
        path = f"{CHARTS_PATH_PREFIX}/{tick_type}/{parse.quote(symbol)}/{parse.quote(resolution)}"
        query = build_query({k: v for k, v in {"from": start, "to": end}.items() if v is not None})
        url = f"{self.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                return _decode_response(raw, response.headers.get("Content-Type", ""))
        except error.HTTPError as exc:
            raise KrakenHTTPError(exc.code, exc.reason) from exc
        except error.URLError as exc:
            raise KrakenFuturesError(f"charts request failed: {exc.reason}") from exc

    def post(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        private: bool = True,
    ) -> Any:
        normalized_endpoint = normalize_endpoint(endpoint)
        body = build_query(params)
        url = f"{self.base_url.rstrip('/')}{TRADING_PATH_PREFIX}{normalized_endpoint}"
        body_bytes = body.encode("utf-8")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        if private:
            headers.update(self._auth_headers(normalized_endpoint, body))

        req = request.Request(url, data=body_bytes, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                return _decode_response(raw, content_type)
        except error.HTTPError as exc:
            payload = _decode_response(exc.read(), exc.headers.get("Content-Type", ""))
            raise KrakenHTTPError(exc.code, exc.reason, payload) from exc
        except error.URLError as exc:
            raise KrakenFuturesError(f"request failed: {exc.reason}") from exc

    def _auth_headers(self, normalized_endpoint: str, query: str) -> dict[str, str]:
        return self._auth_headers_for_path(f"{AUTH_PATH_PREFIX}{normalized_endpoint}", query)

    def _auth_headers_for_path(self, endpoint_path: str, query: str) -> dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise KrakenConfigError(
                "missing KRAKEN_FUTURES_API_KEY or KRAKEN_FUTURES_API_SECRET"
            )

        nonce_value = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = nonce_value
        nonce = str(nonce_value)
        authent = sign_authent(self.api_secret, query, nonce, endpoint_path)
        return {
            "APIKey": self.api_key,
            "Nonce": nonce,
            "Authent": authent,
        }


def _decode_response(raw: bytes, content_type: str) -> Any:
    if not raw:
        return None

    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower():
        return json.loads(text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
