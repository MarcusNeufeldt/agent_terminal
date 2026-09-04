"""Volatility scanner + EMA signal, ported from kraken-futures-cli.

- scan_volatility: ranks perpetuals by realized volatility from closed 1m
  mark candles (sqrt of summed log returns), filtered by quote volume and
  spread. Heavy (one chart fetch per candidate) — cached.
- ema_signal: the ema_volatility_bot's core signal — EMA fast vs slow with
  price-position confirmation on closed 1m mark candles.
"""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from kraken_client import KrakenFuturesError

VOL_CACHE_TTL = 60.0
SIGNAL_CACHE_TTL = 60.0
_vol_cache: dict[tuple[int, int, float, float], tuple[float, dict[str, Any]]] = {}
_signal_cache: dict[str, tuple[float, Any]] = {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ticker_spread_percent(ticker: dict[str, Any]) -> float | None:
    mark = _as_float(ticker.get("markPrice"))
    bid = _as_float(ticker.get("bid"))
    ask = _as_float(ticker.get("ask"))
    return (ask - bid) / mark * 100 if bid is not None and ask is not None and mark else None


def _measure(ticker: dict[str, Any], candles: list[Any], window_minutes: int) -> dict[str, Any] | None:
    current_minute_ms = int(time.time() // 60) * 60_000
    closed = [
        c for c in candles
        if isinstance(c, dict) and (ts := _as_float(c.get("time"))) is not None and ts < current_minute_ms
    ]
    selected = closed[-(window_minutes + 1):]
    if len(selected) < window_minutes + 1:
        return None
    closes = [_as_float(c.get("close")) for c in selected]
    highs = [_as_float(c.get("high")) for c in selected[1:]]
    lows = [_as_float(c.get("low")) for c in selected[1:]]
    if any(v is None or v <= 0 for v in [*closes, *highs, *lows]):
        return None
    prices = [float(v) for v in closes]
    log_returns = [math.log(cur / prev) for prev, cur in zip(prices, prices[1:])]
    return {
        "symbol": str(ticker.get("symbol") or ""),
        "markPrice": _as_float(ticker.get("markPrice")),
        "realizedVolatilityPercent": math.sqrt(sum(v * v for v in log_returns)) * 100,
        "rangePercent": (max(float(v) for v in highs) / min(float(v) for v in lows) - 1) * 100,
        "movePercent": (prices[-1] / prices[0] - 1) * 100,
        "spreadPercent": _ticker_spread_percent(ticker),
        "volumeQuote": _as_float(ticker.get("volumeQuote")),
        "bars": window_minutes,
    }


def _fetch_candles(client, symbol: str, start: int, end: int) -> list[Any]:
    payload = client.get_public_charts(symbol, "1m", tick_type="mark", start=start, end=end)
    candles = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(candles, list):
        raise KrakenFuturesError(f"no candles for {symbol}")
    return candles


def scan_volatility(
    client,
    *,
    window_minutes: int = 5,
    limit: int = 15,
    min_volume_quote: float = 1_000_000,
    max_spread_percent: float = 0.5,
) -> dict[str, Any]:
    now = time.monotonic()
    for key, (ts, _) in list(_vol_cache.items()):
        if now - ts >= VOL_CACHE_TTL:
            del _vol_cache[key]
    cache_key = (window_minutes, limit, min_volume_quote, max_spread_percent)
    hit = _vol_cache.get(cache_key)
    if hit and now - hit[0] < VOL_CACHE_TTL:
        return {**hit[1], "cached": True}

    payload = client.get("/tickers")
    tickers = payload.get("tickers", []) if isinstance(payload, dict) else []
    candidates = [
        t for t in tickers
        if isinstance(t, dict)
        and str(t.get("symbol") or "").startswith("PF_")
        and t.get("tag") == "perpetual"
        and not t.get("suspended")
        and (_as_float(t.get("volumeQuote")) or 0) >= min_volume_quote
        and (sp := _ticker_spread_percent(t)) is not None
        and sp <= max_spread_percent
    ]
    candidates.sort(key=lambda t: _as_float(t.get("volumeQuote")) or 0, reverse=True)

    end = int(time.time())
    start = end - (window_minutes + 10) * 60
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(12, len(candidates)) or 1) as pool:
        futures = {pool.submit(_fetch_candles, client, str(t.get("symbol")), start, end): t for t in candidates}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                row = _measure(ticker, future.result(), window_minutes)
                if row:
                    rows.append(row)
            except KrakenFuturesError:
                continue

    rows.sort(key=lambda r: (r["realizedVolatilityPercent"], r["volumeQuote"] or 0), reverse=True)
    data = {
        "metric": "realized volatility from closed 1m mark-price log returns",
        "windowMinutes": window_minutes,
        "marketsScanned": len(candidates),
        "rows": rows[:limit],
    }
    _vol_cache[cache_key] = (time.monotonic(), data)
    return {**data, "cached": False}


def _ema(values: list[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        raise ValueError(f"EMA {period} needs at least {period} closes")
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for price in values[period:]:
        value += alpha * (price - value)
    return value


def ema_signal(client, symbol: str, fast: int = 400, slow: int = 800) -> dict[str, Any]:
    key = f"{symbol}:{fast}:{slow}"
    now = time.monotonic()
    hit = _signal_cache.get(key)
    if hit and now - hit[0] < SIGNAL_CACHE_TTL:
        return hit[1]

    end = int(time.time())
    start = end - (min(1990, slow * 2 + 10)) * 60
    candles = _fetch_candles(client, symbol, start, end)
    current_minute_ms = int(time.time() // 60) * 60_000
    closes = [
        float(_as_float(c.get("close")))
        for c in candles
        if isinstance(c, dict) and (ts := _as_float(c.get("time"))) is not None and ts < current_minute_ms
    ]
    if len(closes) < slow:
        return {"symbol": symbol, "side": None, "error": f"need {slow} closed candles, got {len(closes)}"}

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    price = closes[-1]
    side = None
    if ema_fast > ema_slow and price > ema_fast:
        side = "long"
    elif ema_fast < ema_slow and price < ema_slow:
        side = "short"
    result = {
        "symbol": symbol,
        "side": side,
        "price": price,
        "emaFast": round(ema_fast, 8),
        "emaSlow": round(ema_slow, 8),
        "fast": fast,
        "slow": slow,
    }
    _signal_cache[key] = (time.monotonic(), result)
    return result
