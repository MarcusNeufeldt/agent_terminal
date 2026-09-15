"""Read-only Binance relative strength for tradable Kraken PF_ altcoins."""
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from binance_candles import BINANCE_BASE, to_binance_symbol

TICKERS_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
CACHE_SECONDS = 60
MAX_AGE_MS = 180_000
WINDOW_TOLERANCE_MS = 120_000
WINDOWS = {"1h": 3_600_000, "6h": 21_600_000, "24h": 86_400_000}
CANDLE_MS = 300_000
_cache = None
_lock = threading.Lock()
_short_cache = None
_short_lock = threading.Lock()
_blocked_until = 0


def _quote(row, now_ms, window="24h"):
    values = [float(row[k]) for k in ("openPrice", "lastPrice", "quoteVolume", "openTime", "closeTime")]
    opened, last, volume, start, end = values
    max_age = MAX_AGE_MS if window == "24h" else CANDLE_MS + WINDOW_TOLERANCE_MS
    tolerance = WINDOW_TOLERANCE_MS if window == "24h" else 1
    if (not all(math.isfinite(v) for v in values) or min(opened, last, volume) <= 0
            or not -WINDOW_TOLERANCE_MS <= now_ms - end <= max_age
            or abs(end - start - WINDOWS[window]) > tolerance):
        raise ValueError(f"missing, stale, or incomplete {window} quote")
    return opened, last, volume, start, end


def build_snapshot(instruments, tickers, now_ms=None, window="24h"):
    if window not in WINDOWS:
        raise ValueError("Window must be 1h, 6h, or 24h")
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if not isinstance(instruments, list) or not instruments or not isinstance(tickers, list):
        raise ValueError("Kraken instruments or Binance tickers unavailable")
    quotes = {row["symbol"]: row for row in tickers if isinstance(row, dict) and row.get("symbol")}
    try:
        btc = _quote(quotes["BTCUSDT"], now_ms, window)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Fresh Binance BTCUSDT benchmark unavailable") from exc
    btc_return = btc[1] / btc[0]
    if not math.isfinite(btc_return) or btc_return <= 0:
        raise ValueError("Invalid Binance BTCUSDT benchmark return")
    rows, eligible = [], _eligible_symbols(instruments)
    for symbol, binance_symbol in eligible.items():
        try:
            opened, last, volume, start, end = _quote(quotes[binance_symbol], now_ms, window)
            tolerance = WINDOW_TOLERANCE_MS if window == "24h" else 0
            if max(abs(start - btc[3]), abs(end - btc[4])) > tolerance:
                continue
            alt_return = last / opened
            relative = (alt_return / btc_return - 1) * 100
            if not all(math.isfinite(v) for v in (alt_return, relative, last / btc[1])):
                continue
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            continue
        rows.append({"symbol": symbol, "asset": symbol[3:-3], "binanceSymbol": binance_symbol,
                     "changeVsBtcPct": relative, "changeUsdtPct": (alt_return - 1) * 100,
                     "priceBtc": last / btc[1], "volumeUsdt": volume})
    rows.sort(key=lambda row: (-row["changeVsBtcPct"], row["symbol"]))
    return {"state": "current", "source": "Binance USDT-M", "window": window,
            "granularity": "ticker" if window == "24h" else "5m",
            "asOfEpochMs": int(btc[4]), "windowStartEpochMs": int(btc[3]),
            "btcChangePct": (btc_return - 1) * 100,
            "eligibleCount": len(eligible), "excludedCount": len(eligible) - len(rows), "rows": rows}


def _eligible_symbols(instruments):
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("Kraken instruments unavailable")
    eligible = {}
    for instrument in instruments:
        if not isinstance(instrument, dict):
            continue
        symbol = str(instrument.get("symbol") or "")
        if (symbol.startswith("PF_") and symbol.endswith("USD") and instrument.get("tradeable") is True
                and not instrument.get("isExpired") and not instrument.get("tradfi")):
            mapped = to_binance_symbol(symbol)
            if mapped != "BTCUSDT":
                eligible[symbol] = mapped
    return eligible


def _read_json(url):
    global _blocked_until
    if time.monotonic() < _blocked_until:
        raise HTTPError(url, 429, "Binance rate limit cooldown; retry later", {}, None)
    request = Request(url, headers={"User-Agent": "kraken-terminal/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        if exc.code in (418, 429):
            minimum = 600 if exc.code == 418 else 60
            try:
                delay = float((exc.headers or {}).get("Retry-After", minimum))
            except (TypeError, ValueError):
                delay = minimum
            if not math.isfinite(delay) or delay < minimum:
                delay = minimum
            _blocked_until = max(_blocked_until, time.monotonic() + delay)
        raise


def _get_tickers():
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        tickers = _read_json(TICKERS_URL)
        if not isinstance(tickers, list):
            raise ValueError("Binance ticker catalog unavailable")
        _cache = (time.monotonic(), tickers)
        return tickers


def candle_quote(symbol, candles, window, end_ms):
    """A full trailing window ending at one shared, completed five-minute boundary."""
    start_ms = end_ms - WINDOWS[window]
    if not isinstance(candles, list):
        raise ValueError("Missing candle data")
    selected = [row for row in candles if isinstance(row, list) and len(row) >= 8
                and start_ms <= int(row[0]) < end_ms]
    if [int(row[0]) for row in selected] != list(range(start_ms, end_ms, CANDLE_MS)):
        raise ValueError("Incomplete or duplicated candle window")
    if any(int(row[6]) != int(row[0]) + CANDLE_MS - 1 for row in selected):
        raise ValueError("Candle close time mismatch")
    volumes = [float(row[7]) for row in selected]
    if any(not math.isfinite(v) or v < 0 for v in volumes):
        raise ValueError("Invalid candle quote volume")
    return {"symbol": symbol, "openPrice": selected[0][1], "lastPrice": selected[-1][4],
            "quoteVolume": sum(volumes), "openTime": start_ms, "closeTime": end_ms - 1}


def _short_quotes(symbol, end_ms, stop):
    if stop.is_set():
        return {}
    try:
        query = urlencode({"symbol": symbol, "interval": "5m", "startTime": end_ms - WINDOWS["6h"],
                           "endTime": end_ms - 1, "limit": 72})
        candles = _read_json(f"{BINANCE_BASE}?{query}")
    except HTTPError as exc:
        if exc.code in (418, 429):
            stop.set()
            raise
        return {}
    except (OSError, ValueError, TypeError):
        return {}
    quotes = {}
    for window in ("1h", "6h"):
        try:
            quotes[window] = candle_quote(symbol, candles, window, end_ms)
        except (ValueError, TypeError, OverflowError, IndexError):
            pass  # Missing bars are omitted, never forward-filled or replaced with 24h data.
    return quotes


def _get_short_snapshot(instruments, window):
    global _short_cache
    eligible = _eligible_symbols(instruments)
    tickers = _get_tickers()
    benchmark = next((row for row in tickers if isinstance(row, dict) and row.get("symbol") == "BTCUSDT"), {})
    now_ms = time.time() * 1000
    stamp = float(benchmark.get("closeTime") or 0)
    if not math.isfinite(stamp) or not -WINDOW_TOLERANCE_MS <= now_ms - stamp <= MAX_AGE_MS:
        raise ValueError("Fresh Binance clock reference unavailable")
    # Use exchange time too: a fast local clock must not include a still-forming candle.
    end_ms = int(min(now_ms, stamp)) // CANDLE_MS * CANDLE_MS
    key = (end_ms, tuple(sorted(eligible)))
    # Both short windows share the same candles; the 24h bulk endpoint stays independent.
    with _short_lock:
        if not _short_cache or _short_cache[0] != key:
            available = {row.get("symbol") for row in tickers if isinstance(row, dict)}
            stop = threading.Event()
            btc = _short_quotes("BTCUSDT", end_ms, stop)
            for period in ("1h", "6h"):
                if period not in btc:
                    raise ValueError("Aligned Binance BTC candle benchmark unavailable")
                _quote(btc[period], time.time() * 1000, period)
            quotes = {period: [btc[period]] for period in ("1h", "6h")}
            symbols = sorted(set(eligible.values()) & available)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for result in pool.map(lambda symbol: _short_quotes(symbol, end_ms, stop), symbols):
                    for period, row in result.items():
                        quotes[period].append(row)
            _short_cache = (key, quotes)
        return build_snapshot(instruments, _short_cache[1][window], window=window)


def get_snapshot(instruments, window="24h"):
    if window not in WINDOWS:
        raise ValueError("Window must be 1h, 6h, or 24h")
    if window == "24h":
        return build_snapshot(instruments, _get_tickers())
    return _get_short_snapshot(instruments, window)
