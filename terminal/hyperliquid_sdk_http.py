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
    def __init__(self, base_url, *, timeout, max_bytes):
        self.base_url, self.timeout, self.max_bytes = base_url, timeout, max_bytes
        self._local = threading.local()

    def post(self, path, payload):
        # A terminal info client is shared by concurrent handlers. Never share a
        # mutable requests Session between those threads.
        api = getattr(self._local, "api", None)
        if api is None:
            api = API(self.base_url, timeout=self.timeout)
            api.session.headers["User-Agent"] = "AgentTerminal/1"
            api.session.hooks["response"].append(partial(bounded_response, max_bytes=self.max_bytes))
            self._local.api = api
        return api.post(path, payload)

    def close(self):
        api = getattr(self._local, "api", None)
        if api is not None:
            api.session.close()
            del self._local.api
