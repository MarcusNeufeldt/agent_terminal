"""Official SDK HTTP client with bounded responses, no redirects and no retries."""
import threading
from functools import partial

from hyperliquid.api import API
from hyperliquid.utils.error import ClientError, ServerError
from requests import RequestException


def bounded_response(response, *args, max_bytes, **kwargs):
    try:
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            content.extend(chunk)
            if len(content) > max_bytes:
                raise RequestException("Hyperliquid response exceeds the read limit")
        # Cache the bounded bytes for SDK API.post's Response.json/text access.
        # Response hooks run before requests performs its default unbounded read.
        response._content = bytes(content)
        response._content_consumed = True
        if 300 <= response.status_code < 400:
            raise RequestException("Hyperliquid redirects are not allowed")
        if 400 <= response.status_code < 500:
            # SDK 0.24 assumes some JSON 4xx bodies contain code/msg. Gate on HTTP
            # status even when an intermediary returns a different body shape.
            raise ClientError(response.status_code, None, "Hyperliquid HTTP rejection", response.headers)
        if response.status_code >= 500:
            raise ServerError(response.status_code, "Hyperliquid upstream failure")
        return response
    finally:
        response.close()


class SDKTransport:
    # Idle SDK sessions kept warm between requests. Each request checks one out, so
    # concurrent handlers never share a mutable requests Session, while the next
    # request reuses the open connection instead of paying a new TCP+TLS handshake.
    MAX_IDLE = 8

    def __init__(self, base_url, *, timeout, max_bytes):
        self.base_url, self.timeout, self.max_bytes = base_url, timeout, max_bytes
        self._lock = threading.Lock()
        self._idle = []

    def _new_api(self):
        api = API(self.base_url, timeout=self.timeout)
        api.session.headers["User-Agent"] = "AgentTerminal/1"
        api.session.hooks["response"].append(partial(bounded_response, max_bytes=self.max_bytes))
        return api

    def post(self, path, payload):
        with self._lock:
            api = self._idle.pop() if self._idle else None
        api = api or self._new_api()
        try:
            result = api.post(path, payload)
        except BaseException:
            # A failed exchange can leave the connection in an unknown state: drop it.
            api.session.close()
            raise
        with self._lock:
            if len(self._idle) < self.MAX_IDLE:
                self._idle.append(api)
                return result
        api.session.close()
        return result

    def close(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for api in idle:
            api.session.close()
