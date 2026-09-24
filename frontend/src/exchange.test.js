import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const makeVite = () => createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });

test("Hyperliquid requests, UI, storage, and overlays stay separate from Kraken", async t => {
  const original = Object.fromEntries(["localStorage", "location", "fetch", "confirm"].map(k => [k, globalThis[k]]));
  t.after(() => Object.assign(globalThis, original));
  const storage = new Map([["kt.exchange", "hyperliquid"], ["kt.symbol", "PF_UNIUSD"], ["kt.pro", "1"]]);
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  let reloads = 0;
  globalThis.location = { reload: () => { reloads++; } };
  globalThis.confirm = () => true;
  let backend = { exchangeRouting: 1, exchange: "hyperliquid", exchangeEpoch: 4 };
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url === "/api/session") return Response.json({ token: "fixture-token", ...backend });
    const parsed = new URL(url, "http://terminal.test");
    assert.equal(parsed.searchParams.get("exchange"), "hyperliquid");
    assert.equal(options.headers["X-Terminal-Exchange-Epoch"], "4");
    if (parsed.pathname === "/api/exchange") {
      assert.equal(JSON.parse(options.body).exchange, "kraken");
      backend = { ...backend, exchange: "kraken", exchangeEpoch: 5 };
      return Response.json({ ...backend, armed: false });
    }
    assert.notEqual(options.method, "POST", "no Hyperliquid trading writes may reach fetch");
    return Response.json({ ...backend, armed: false, readOnly: true, hasKeys: false, env: "live" });
  };

  const vite = await makeVite();
  t.after(() => vite.close());
  const { api, apiUrl } = await vite.ssrLoadModule("/src/api.js");
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const { default: App } = await vite.ssrLoadModule("/src/App.jsx");
  const { buildChartOverlays } = await vite.ssrLoadModule("/src/chart-overlays.js");
  const { venueKey, isVenueSymbol } = await vite.ssrLoadModule("/src/exchange.js");
  const { TerminalVelaProvider } = await vite.ssrLoadModule("/src/vela-provider.js");
  assert.equal(store.getState().symbol, "HL_BTC", "never restore Kraken's active market into HL");
  assert.equal(store.getState().pro, false, "do not fabricate a balance for an unconfigured venue");
  assert.equal(venueKey("terminal-vela-workspace"), "terminal-vela-workspace.hyperliquid");
  assert.equal(isVenueSymbol("PF_APTUSD"), false);
  assert.equal(new TerminalVelaProvider().info().displayName, "Hyperliquid / Hyperliquid charts");
  await api("/api/health");
  assert.equal(apiUrl("/api/stream"), "/api/stream?exchange=hyperliquid");
  const before = requests.length;
  // /api/arm is deliberately absent: the ARM gate is venue-neutral, so it is reachable here.
  // So is /api/chase/abort: stopping a Chase only cancels (see hyperliquid-ticket.test.js).
  for (const path of ["order", "cancel", "action", "grid", "flatten", "chase", "chat", "chat/reset"]) {
    await assert.rejects(api(`/api/${path}`, { method: "POST", body: {} }), /disabled by the backend gate/);
  }
  await assert.rejects(api("/api/account?exchange=kraken"), /Cross-exchange/);
  assert.equal(requests.length, before, "blocked writes and cross-venue reads never reach the network");

  const position = { symbol: "HL_APT", side: "long", size: 2, price: .5, liqPriceEstimate: .3 };
  const order = { symbol: "HL_APT", side: "sell", size: 2, stopPrice: .8, orderType: "take_profit", order_id: "123" };
  const instruments = [{ symbol: "HL_APT", tickSize: .0001, contractSize: 1 }];
  const overlays = buildChartOverlays("HL_APT", [position], [order], instruments, true);
  assert.ok(overlays.length >= 3);
  assert.ok(overlays.every(line => !line.order && !line.position && !line.tp), "read-only charts have no execution handles");
  store.setState({ exchangeRouting: true, positions: [position], orders: [order], instruments,
    account: { balanceValue: 0, withdrawable: 0, spotUsdc: 13.77 },
    dataStatus: { positions: { state: "current" }, orders: { state: "current" } },
    tickers: { HL_APT: { symbol: "HL_APT", last: .6, markPrice: .7 } },
  });
  assert.ok(Math.abs(store.getState().totalUpnl() - (.2 - 0.00045 * 2 * .6 - 0.00045 * 2 * .5)) < 1e-12,
    "HL net PnL: actual last less the exit and entry taker fees, never mark");
  store.getState().onTicker({ symbol: "PF_APTUSD", last: 999, exchange: "kraken" });
  assert.equal(store.getState().tickers.PF_APTUSD, undefined);
  const workingFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("fixture network offline"); };
  await store.getState().refreshTables();
  await store.getState().refreshFills();
  assert.deepEqual(store.getState().positions, [position], "failed transport preserves last-known rows");
  assert.deepEqual(store.getState().orders, [order]);
  assert.equal(store.getState().dataStatus.positions.state, "unavailable");
  assert.equal(store.getState().dataStatus.fills.state, "unavailable");
  assert.equal(store.getState().totalUpnl(), null, "unavailable positions are not a current PnL total");
  globalThis.fetch = workingFetch;
  store.setState({ dataStatus: { positions: { state: "current" }, orders: { state: "current" } } });
  const render = () => {
    // Zustand's bound hook owns the original initial snapshot.
    Object.assign(store.getInitialState(), store.getState());
    return renderToStaticMarkup(React.createElement(App));
  };
  let markup = render();
  assert.match(markup, /id="exchange-select"/);
  assert.doesNotMatch(markup, /id="book"|Order book/, "the order book panel is not mounted");
  assert.match(markup, /Hyperliquid · Read-only/);
  assert.match(markup, /id="arm-btn" disabled=""/);
  assert.match(markup, /Liq \(exchange\)/);
  assert.doesNotMatch(markup, /id="btn-buy"|id="chat-input"|Kraken last:/);
  assert.match(markup, /Perp balance/);
  assert.match(markup, /Spot USDC/);
  assert.match(markup, /Perp withdrawable/);
  // Spot and perp are separate balances: showing the perp figure alone would read
  // as an empty account while funds sit in spot.
  assert.match(markup, /Perp balance<\/span><span class="v">\$0<\/span>/);
  assert.match(markup, /Spot USDC<\/span><span class="v">\$13\.77<\/span>/);
  assert.doesNotMatch(markup, /\$4,300/);
  // Unified account: one USDC balance backs both spot and perps, so a "Perp balance $0"
  // row would read as an empty account while the money is right there.
  store.setState({ account: { balanceValue: 13.77, unified: true, spotUsdc: 13.77, withdrawable: null } });
  const unifiedMarkup = render();
  assert.match(unifiedMarkup, /USDC balance<\/span><span class="v">\$13\.77<\/span>/);
  assert.doesNotMatch(unifiedMarkup, /Perp balance/);
  assert.doesNotMatch(unifiedMarkup, /Spot USDC/);
  assert.doesNotMatch(unifiedMarkup, /Perp withdrawable/);
  store.setState({ account: { balanceValue: 0, withdrawable: 0, spotUsdc: 13.77 } });
  store.setState({ positions: [], dataStatus: { positions: { state: "unavailable", error: "Account address not configured" } } });
  markup = render();
  assert.match(markup, /Account address not configured/);
  assert.doesNotMatch(markup, /No open positions/);

  await store.getState().switchExchange("kraken");
  assert.equal(storage.get("kt.exchange"), "kraken");
  assert.equal(reloads, 1, "switch uses a full reload, not reused requests or drafts");
  assert.equal(store.getState().exchange, "hyperliquid", "old page never mutates its request target in place");
  assert.equal(requests.filter(r => r.options.method === "POST").length, 1, "only the local switch was sent");

  const freshVite = await makeVite();
  t.after(() => freshVite.close());
  const { default: fresh } = await freshVite.ssrLoadModule("/src/store.js");
  assert.equal(fresh.getState().exchange, "kraken");
  assert.equal(fresh.getState().symbol, "PF_UNIUSD", "Kraken's own selected symbol was preserved");
  assert.deepEqual(fresh.getState().orders, []);
  assert.deepEqual(fresh.getState().positions, []);
  assert.deepEqual(fresh.getState().chat, []);
  assert.equal(fresh.getState().gridRequest, null);
});

test("old backend cannot receive Hyperliquid requests, and stale epochs never replay writes", async t => {
  const original = Object.fromEntries(["localStorage", "location", "fetch"].map(k => [k, globalThis[k]]));
  t.after(() => Object.assign(globalThis, original));
  const saved = new Map([["kt.exchange", "hyperliquid"]]);
  globalThis.localStorage = { getItem: k => saved.get(k) ?? null, setItem: (k, v) => saved.set(k, v) };
  let reloads = 0;
  globalThis.location = { reload: () => { reloads++; } };
  let calls = [];
  globalThis.fetch = async url => { calls.push(url); return Response.json({ token: "old-token" }); };
  const oldVite = await makeVite();
  t.after(() => oldVite.close());
  const { api: oldApi } = await oldVite.ssrLoadModule("/src/api.js");
  await assert.rejects(oldApi("/api/account"), /Restart the updated backend/);
  assert.deepEqual(calls, ["/api/session"], "no HL read is ever sent to an old Kraken-only server");

  saved.set("kt.exchange", "kraken");
  calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/api/session") return Response.json({ token: "token", exchange: "kraken", exchangeRouting: 1, exchangeEpoch: 0 });
    assert.equal(options.headers["X-Terminal-Exchange-Epoch"], "0");
    return Response.json({ error: "Expired", exchange: "kraken", exchangeRouting: 1, exchangeEpoch: 2 }, { status: 409 });
  };
  const staleVite = await makeVite();
  t.after(() => staleVite.close());
  const { api } = await staleVite.ssrLoadModule("/src/api.js");
  await assert.rejects(api("/api/order", { method: "POST", body: { requestId: "fixture-request" } }), /no request was retried/);
  assert.equal(calls.filter(c => c.options.method === "POST").length, 1);
  assert.equal(reloads, 1, "even an A -> B -> A switch requires a fresh page");
});

test("Hyperliquid boot owns its stream and never loads Kraken automation or chat", async t => {
  const keys = ["localStorage", "location", "window", "EventSource", "setInterval", "clearInterval", "fetch"];
  const original = Object.fromEntries(keys.map(k => [k, globalThis[k]]));
  t.after(() => Object.assign(globalThis, original));
  const saved = new Map([["kt.exchange", "hyperliquid"]]);
  const vite = await makeVite();
  t.after(() => vite.close());
  const intervals = new Set(), listeners = new Map();
  let reloads = 0, source;
  globalThis.localStorage = { getItem: k => saved.get(k) ?? null, setItem: (k, v) => saved.set(k, v) };
  globalThis.location = { reload: () => { reloads++; } };
  globalThis.window = { addEventListener: (key, fn) => listeners.set(key, fn), removeEventListener: key => listeners.delete(key) };
  globalThis.setInterval = fn => { intervals.add(fn); return fn; };
  globalThis.clearInterval = fn => intervals.delete(fn);
  globalThis.EventSource = class {
    constructor(url) { assert.equal(url, "/api/stream?exchange=hyperliquid"); source = this; this.handlers = {}; }
    addEventListener(kind, fn) { this.handlers[kind] = fn; }
    close() { this.closed = true; }
  };
  const requests = [];
  const session = { exchange: "hyperliquid", exchangeEpoch: 1, exchangeRouting: 1 };
  globalThis.fetch = async (url, options = {}) => {
    assert.notEqual(options.method, "POST");
    if (url === "/api/session") return Response.json({ token: "fixture", ...session });
    const parsed = new URL(url, "http://terminal.test");
    assert.equal(parsed.searchParams.get("exchange"), "hyperliquid");
    requests.push(parsed.pathname);
    const data = parsed.pathname === "/api/health" ? { ...session, armed: false, env: "live", hasKeys: false,
      network: "mainnet", accountAddress: "0x" + "1".repeat(40) }
      : parsed.pathname === "/api/execution-recovery" ? { state: "current", items: [], hasMore: false }
      : parsed.pathname === "/api/instruments" ? { instruments: [{ symbol: "HL_BTC", contractSize: 1 }] }
      : parsed.pathname === "/api/tickers" ? { tickers: {}, watchlist: ["HL_BTC"] }
      : assert.fail(`Unexpected boot request ${url}`);
    return Response.json(data);
  };
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const { bootTerminal } = await vite.ssrLoadModule("/src/components/boot.js");
  store.setState({ loadChart: async () => {}, loadChatHistory: () => assert.fail("HL must not load Kraken chat"),
    refreshAccount: () => {}, refreshTables: () => {}, refreshFills: () => {}, pollMarketList: () => {},
    refreshSignal: () => assert.fail("HL must not request Kraken signals"), toast: () => {},
  });
  const controller = new AbortController();
  await bootTerminal(controller.signal);
  assert.deepEqual(requests, ["/api/health", "/api/execution-recovery", "/api/instruments", "/api/tickers"]);
  assert.equal(store.getState().hlRecoveryLoaded, true);
  assert.deepEqual(store.getState().chat, []);
  assert.equal(intervals.size, 6);
  source.handlers.ticker({ data: JSON.stringify({ symbol: "HL_BTC", exchange: "hyperliquid", last: 100 }) });
  source.handlers.ticker({ data: JSON.stringify({ symbol: "PF_XBTUSD", exchange: "kraken", last: 999 }) });
  assert.equal(store.getState().tickers.HL_BTC.last, 100);
  assert.equal(store.getState().tickers.PF_XBTUSD, undefined);
  saved.set("kt.exchange", "kraken");
  listeners.get("storage")({ key: "kt.exchange" });
  assert.equal(reloads, 1);
  controller.abort();
  assert.equal(source.closed, true);
  assert.equal(intervals.size, 0);
  assert.equal(listeners.size, 0);
});
