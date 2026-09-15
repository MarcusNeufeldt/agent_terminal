"""Venue selection only. Never owns an exchange client, account, or database."""

from contextlib import contextmanager
from urllib.parse import parse_qs

EXCHANGES = ("kraken", "hyperliquid")


class ExchangeRoutingError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def requested_exchange(query_string):
    values = parse_qs(query_string, keep_blank_values=True).get("exchange", ["kraken"])
    if len(values) != 1 or values[0] not in EXCHANGES:
        raise ExchangeRoutingError("exchange must be kraken or hyperliquid", 400)
    return values[0]


class ExchangeRouting:
    def __init__(self, lock):
        self.lock = lock
        self.active = "kraken"
        self.epoch = 0
        self.pending = 0

    def snapshot(self):
        with self.lock:
            return {"exchange": self.active, "exchangeEpoch": self.epoch, "exchangeRouting": 1}

    def check(self, exchange, epoch=None, *, write=False):
        with self.lock:
            if exchange not in EXCHANGES:
                raise ExchangeRoutingError("Unknown exchange", 400)
            if exchange != self.active:
                raise ExchangeRoutingError("Exchange changed in another tab. Reload before continuing.")
            # Legacy Kraken clients work only until the first switch. An epoch
            # rejects old requests even after Kraken -> Hyperliquid -> Kraken.
            expected = str(self.epoch)
            if write and not (epoch == expected or epoch is None and self.epoch == 0 and exchange == "kraken"):
                raise ExchangeRoutingError("Exchange session expired. Reload before submitting again.")

    @contextmanager
    def request(self, exchange, epoch):
        with self.lock:
            self.check(exchange, epoch, write=True)
            self.pending += 1
        try:
            yield
        finally:
            with self.lock:
                self.pending -= 1

    def switch(self, exchange, epoch, target, *, active_chases, disarm):
        with self.lock:
            self.check(exchange, epoch, write=True)
            if target not in EXCHANGES:
                raise ExchangeRoutingError("Unknown exchange", 400)
            if target == self.active:
                return self.snapshot()
            if self.pending:
                raise ExchangeRoutingError("A terminal request is still running. Wait for its result before switching.")
            try:
                workers = active_chases()
            except Exception as exc:
                raise ExchangeRoutingError("Cannot inspect Chase workers. Exchange switch blocked.", 503) from exc
            if not isinstance(workers, list):
                raise ExchangeRoutingError("Chase state unavailable. Exchange switch blocked.", 503)
            if workers:
                raise ExchangeRoutingError("Finish or reconcile active Chase workers before switching exchanges.")
            disarm()
            self.active = target
            self.epoch += 1
            return self.snapshot()
