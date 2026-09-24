import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";

globalThis.localStorage = { getItem: () => null, setItem: () => {} };

test("boot owns one live feed and releases pollers/listeners on remount or interrupted startup", async t => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const { bootTerminal } = await vite.ssrLoadModule("/src/components/boot.js");
  const originals = Object.fromEntries(["fetch", "window", "EventSource", "setInterval", "clearInterval"].map(k => [k, globalThis[k]]));
  t.after(() => Object.assign(globalThis, originals));
  const pollers = new Set(), sources = new Set(), listeners = new Set();
  globalThis.setInterval = callback => { pollers.add(callback); return callback; };
  globalThis.clearInterval = callback => pollers.delete(callback);
  globalThis.window = { addEventListener: (name, fn) => listeners.add(fn), removeEventListener: (name, fn) => listeners.delete(fn) };
  globalThis.EventSource = class {
    constructor() { this.handlers = {}; sources.add(this); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { sources.delete(this); }
  };
  const symbol = "PF_TESTUSD";
  globalThis.fetch = async (url, options = {}) => {
    assert.notEqual(options.method, "POST", "startup must not write to the exchange");
    const data = url === "/api/session" ? { token: "test-token" }
      : url === "/api/health" ? { armed: false, env: "test", hasKeys: true }
      : url === "/api/chase" ? { chases: [] }
      : url === "/api/protection/alerts" ? { alerts: [] }
      : url === "/api/instruments" ? { instruments: [{ symbol, contractSize: 1 }] }
      : url.startsWith("/api/tickers?") ? { tickers: {}, watchlist: [symbol] }
      : assert.fail(`Unexpected request ${url}`);
    return new Response(JSON.stringify(data));
  };
  store.setState({ symbol: "PF_OTHERUSD", chat: [{ role: "assistant", content: "fixture" }],
    positions: [{ symbol, side: "long", size: 2, price: 100 }],
    dataStatus: { positions: { state: "current" } },
    loadChart: async () => {}, loadChatHistory: async () => {},
    refreshAccount: () => {}, refreshTables: () => {}, refreshFills: () => {},
    refreshSignal: () => {}, pollMarketList: () => {}, toast: () => {},
    refreshStats: () => {}, refreshPositionBooks: () => {},
  });
  for (let mount = 0; mount < 2; mount++) {
    const controller = new AbortController();
    await bootTerminal(controller.signal);
    assert.equal(sources.size, 1);
    assert.equal(pollers.size, 11, "includes the position-book poller behind net PnL");
    assert.equal(listeners.size, 2);
    const source = [...sources][0];
    source.handlers.ticker({ data: JSON.stringify({ symbol, last: 110 + mount, markPrice: 150 }) });
    // Net of the Kraken taker fee at the streamed last price (no book in this fixture).
    assert.ok(Math.abs(store.getState().totalUpnl() - (20 + 2 * mount - 0.0005 * 2 * (110 + mount) - 0.0005 * 2 * 100)) < 1e-9,
      "non-selected position gets streamed last price");
    controller.abort();
    assert.equal(sources.size, 0, "unmount must close the stream instead of leaving a stale subscription");
    assert.equal(pollers.size, 0);
    assert.equal(listeners.size, 0);
  }
  let enterChart, releaseChart;
  const entered = new Promise(resolve => { enterChart = resolve; });
  const blocked = new Promise(resolve => { releaseChart = resolve; });
  store.setState({ loadChart: async () => { enterChart(); await blocked; } });
  const controller = new AbortController();
  const starting = bootTerminal(controller.signal);
  await entered;
  controller.abort();
  releaseChart();
  await starting;
  assert.equal(sources.size, 0, "an old startup must not reconnect after it was disposed");
  assert.equal(pollers.size, 0);
  assert.equal(listeners.size, 0);
});
