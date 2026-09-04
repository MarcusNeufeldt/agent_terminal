import { api, RES_SECONDS } from "./api.js";
import { subscribeBinanceCandles } from "./market-events.js";

const TO_TERMINAL = {
  "1": "1m",
  "5": "5m",
  "15": "15m",
  "30": "30m",
  "60": "1h",
  "240": "4h",
  "720": "12h",
  D: "1d",
  W: "1w",
};

const TO_VELA = Object.fromEntries(Object.entries(TO_TERMINAL).map(([vela, terminal]) => [terminal, vela]));

export const VELA_TIMEFRAMES = Object.keys(TO_TERMINAL);

export function toTerminalTimeframe(timeframe) {
  return TO_TERMINAL[String(timeframe)] || "1m";
}

export function toVelaTimeframe(resolution) {
  return TO_VELA[String(resolution)] || "1";
}

export function toVelaBar(candle) {
  return {
    time: Number(candle[0]) * 1000,
    open: Number(candle[1]),
    high: Number(candle[2]),
    low: Number(candle[3]),
    close: Number(candle[4]),
    volume: Number(candle[5] || 0),
  };
}

export function accumulateLiveCandle(previous, candle, timeframe, accumulator = null) {
  const seconds = RES_SECONDS[toTerminalTimeframe(timeframe)] || 60;
  const time = Math.floor(Number(candle.t) / seconds) * seconds * 1000;
  if (previous && time < previous.time) return null;
  const currentVolume = Number(candle.v || 0);
  const fresh = !accumulator || accumulator.time !== time;
  const existed = previous?.time === time;
  const state = fresh ? {
    time,
    open: existed ? previous.open : Number(candle.o),
    high: Math.max(existed ? previous.high : Number(candle.h), Number(candle.h)),
    low: Math.min(existed ? previous.low : Number(candle.l), Number(candle.l)),
    close: Number(candle.c),
    volumeBase: existed ? Math.max(0, Number(previous.volume || 0) - currentVolume) : 0,
    minuteVolumes: { [candle.t]: currentVolume },
  } : {
    ...accumulator,
    high: Math.max(accumulator.high, Number(candle.h)),
    low: Math.min(accumulator.low, Number(candle.l)),
    close: Number(candle.c),
    minuteVolumes: { ...accumulator.minuteVolumes, [candle.t]: currentVolume },
  };
  state.volume = state.volumeBase + Object.values(state.minuteVolumes).reduce((sum, value) => sum + value, 0);
  return {
    bar: { time: state.time, open: state.open, high: state.high, low: state.low, close: state.close, volume: state.volume },
    accumulator: state,
  };
}

export function mergeLiveCandle(previous, candle, timeframe) {
  return accumulateLiveCandle(previous, candle, timeframe)?.bar || null;
}

export class TerminalVelaProvider {
  constructor() {
    this.lastBars = new Map();
    this.instrumentsPromise = null;
  }

  info() {
    return {
      name: "terminal",
      displayName: "Kraken Futures / Binance charts",
      supportedTimeframes: VELA_TIMEFRAMES,
      capabilities: { enumerate: true, stream: true, symbolInfo: true },
    };
  }

  async instruments() {
    this.instrumentsPromise ||= api("/api/instruments")
      .then(data => data.instruments || [])
      .catch(error => { this.instrumentsPromise = null; throw error; });
    return this.instrumentsPromise;
  }

  async listSymbols() {
    return (await this.instruments())
      .filter(instrument => instrument.tradeable !== false && String(instrument.symbol || "").startsWith("PF_"))
      .map(instrument => ({
        ticker: String(instrument.symbol),
        description: String(instrument.pair || instrument.underlying || instrument.symbol),
        type: "futures",
      }));
  }

  async getSymbolInfo(ticker) {
    const instrument = (await this.instruments()).find(item => item.symbol === ticker) || {};
    return {
      ticker,
      mintick: Number(instrument.tickSize) || undefined,
      pointvalue: Number(instrument.contractSize) || undefined,
    };
  }

  async getBars(ticker, timeframe, range = {}) {
    const resolution = toTerminalTimeframe(timeframe);
    const data = await api(`/api/candles?symbol=${encodeURIComponent(ticker)}&res=${encodeURIComponent(resolution)}`);
    let bars = (data.candles || []).map(toVelaBar);
    if (Number.isFinite(range.from)) bars = bars.filter(bar => bar.time >= range.from);
    if (Number.isFinite(range.to)) bars = bars.filter(bar => bar.time <= range.to);
    if (Number.isFinite(range.limit) && bars.length > range.limit) bars = bars.slice(-range.limit);
    if (bars.length) this.lastBars.set(`${ticker}|${timeframe}`, bars[bars.length - 1]);
    return bars;
  }

  subscribe(ticker, timeframe, onBar) {
    const key = `${ticker}|${timeframe}`;
    let accumulator = null;
    return subscribeBinanceCandles(candle => {
      if (candle.symbol !== ticker) return;
      const result = accumulateLiveCandle(this.lastBars.get(key), candle, timeframe, accumulator);
      if (!result) return;
      accumulator = result.accumulator;
      this.lastBars.set(key, result.bar);
      onBar(result.bar);
    });
  }
}
