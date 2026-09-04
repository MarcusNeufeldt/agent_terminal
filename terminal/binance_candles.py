"""Binance USDT-M futures klines for chart display.

Read-only market data: candle HISTORY for the chart UI. Trading, tickers,
orderbook, positions and fills stay on Kraken. Falls back to None for any
symbol Binance doesn't list, so server.get_candles can use the Kraken path.
"""

from __future__ import annotations

import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BINANCE_BASE = "https://fapi.binance.com/fapi/v1/klines"

_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
    "4h": "4h", "12h": "12h", "1d": "1d", "1w": "1w",
}

# freshness window per resolution: the live edge is streamed from Kraken trades,
# this cache only serves history on load/switch
_TTL = {"1m": 20, "5m": 30, "15m": 60, "30m": 90, "1h": 120, "4h": 300, "12h": 600, "1d": 900, "1w": 1800}

_cache: dict[str, tuple[float, list[list[float]]]] = {}
_cache_lock = threading.Lock()
_missing: dict[str, float] = {}  # binance symbol -> epoch until which we skip it
MISSING_TTL = 600.0


def to_binance_symbol(kraken_symbol: str) -> str:
    """PF_ENAUSD -> ENAUSDT, PF_XBTUSD -> BTCUSDT, XBTUSD -> BTCUSDT."""
    sym = str(kraken_symbol or "").strip().upper()
    if sym.startswith("PF_"):
        sym = sym[3:]
    elif sym.startswith("FI_"):  # Kraken fixings — no Binance equivalent
        return ""
    if sym.endswith("USD"):
        sym = sym[:-3]
    if sym == "XBT":
        sym = "BTC"
    if not sym or not sym.isalnum():
        return ""
    return sym + "USDT"


def get_klines(kraken_symbol: str, resolution: str, limit: int = 1500) -> list[list[float]] | None:
    """[[timeSec, open, high, low, close, volume], ...] or None if unavailable."""
    interval = _INTERVALS.get(resolution)
    if not interval:
        return None
    bsym = to_binance_symbol(kraken_symbol)
    if not bsym:
        return None

    now = time.time()
    key = f"{bsym}:{interval}:{limit}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        if now < _missing.get(bsym, 0):
            return None

    url = f"{BINANCE_BASE}?symbol={quote(bsym)}&interval={interval}&limit={min(max(int(limit), 1), 1500)}"
    try:
        req = Request(url, headers={"User-Agent": "kraken-terminal/1.0"})
        with urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
    except HTTPError as exc:
        if exc.code in (400, 404, 418):  # unknown symbol / blocked — stop retrying for a while
            with _cache_lock:
                _missing[bsym] = time.time() + MISSING_TTL
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    candles = [
        [int(k[0] // 1000), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
        for k in raw
        if isinstance(k, (list, tuple)) and len(k) >= 6
    ]
    if candles:
        with _cache_lock:
            _cache[key] = (time.time() + _TTL.get(resolution, 30), candles)
    return candles


_atr_cache: dict[str, tuple[float, float]] = {}


def atr14d(kraken_symbol: str) -> tuple[float, float] | None:
    """(ATR14, lastPrice) on 1d klines, or None. Cached 5 min."""
    bsym = to_binance_symbol(kraken_symbol)
    if not bsym:
        return None
    now = time.time()
    with _cache_lock:
        hit = _atr_cache.get(bsym)
        if hit and now - hit[0] < 300:
            return hit[1], hit[2]
    try:
        req = Request(f"{BINANCE_BASE}?symbol={quote(bsym)}&interval=1d&limit=16", headers={"User-Agent": "kraken-terminal/1.0"})
        with urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
        k = [[float(x) for x in row[:6]] for row in raw]  # [t, o, h, l, c, v]
    except Exception:
        return None
    if len(k) < 15:
        return None
    trs = []
    for i in range(1, len(k)):
        h, l, pc = k[i][2], k[i][3], k[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-14:]) / 14
    price = k[-1][4]
    with _cache_lock:
        _atr_cache[bsym] = (now, atr, price)
    return atr, price
