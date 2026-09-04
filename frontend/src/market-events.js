const candleListeners = new Set();

export function publishBinanceCandle(candle) {
  for (const listener of candleListeners) {
    try { listener(candle); }
    catch (error) { console.warn("Vela candle listener failed", error); }
  }
}

export function subscribeBinanceCandles(listener) {
  candleListeners.add(listener);
  return () => candleListeners.delete(listener);
}
