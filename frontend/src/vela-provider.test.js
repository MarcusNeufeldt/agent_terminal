import test from "node:test";
import assert from "node:assert/strict";
import { accumulateLiveCandle, mergeLiveCandle, toTerminalTimeframe, toVelaBar, toVelaTimeframe } from "./vela-provider.js";
import { publishBinanceCandle, subscribeBinanceCandles } from "./market-events.js";

test("maps terminal and Vela timeframe names", () => {
  assert.equal(toVelaTimeframe("1m"), "1");
  assert.equal(toVelaTimeframe("12h"), "720");
  assert.equal(toTerminalTimeframe("60"), "1h");
  assert.equal(toTerminalTimeframe("W"), "1w");
});

test("converts terminal candle seconds to Vela milliseconds", () => {
  assert.deepEqual(toVelaBar([10, 1, 3, 0.5, 2, 7]), {
    time: 10_000, open: 1, high: 3, low: 0.5, close: 2, volume: 7,
  });
});

test("one failed live subscriber does not starve the other charts", () => {
  let received = null;
  const warn = console.warn;
  console.warn = () => {};
  const offBad = subscribeBinanceCandles(() => { throw new Error("broken cell"); });
  const offGood = subscribeBinanceCandles(candle => { received = candle; });
  try {
    publishBinanceCandle({ symbol: "PF_XBTUSD" });
    assert.deepEqual(received, { symbol: "PF_XBTUSD" });
  } finally {
    offBad();
    offGood();
    console.warn = warn;
  }
});

test("merges forming one-minute candles into the selected Vela bucket", () => {
  const previous = { time: 0, open: 10, high: 12, low: 9, close: 11, volume: 100 };
  assert.deepEqual(
    mergeLiveCandle(previous, { t: 60, o: 11, h: 13, l: 10, c: 12, v: 20 }, "5"),
    { time: 0, open: 10, high: 13, low: 9, close: 12, volume: 100 },
  );
  const first = accumulateLiveCandle(previous, { t: 60, o: 11, h: 13, l: 10, c: 12, v: 20 }, "5");
  const second = accumulateLiveCandle(first.bar, { t: 120, o: 12, h: 14, l: 11, c: 13, v: 5 }, "5", first.accumulator);
  assert.equal(second.bar.volume, 105);
  assert.deepEqual(
    mergeLiveCandle(previous, { t: 300, o: 12, h: 14, l: 11, c: 13, v: 5 }, "5"),
    { time: 300_000, open: 12, high: 14, low: 11, close: 13, volume: 5 },
  );
  assert.equal(mergeLiveCandle(previous, { t: -60, o: 1, h: 1, l: 1, c: 1, v: 1 }, "5"), null);
});
