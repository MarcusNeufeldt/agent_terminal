import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const SYMBOL = "HL_APT";
const SESSION = { token: "fixture", exchange: "hyperliquid", exchangeEpoch: 1, exchangeRouting: 1 };
const capacityFixture = () => ({ state: "current", exchange: "hyperliquid", network: "mainnet",
  accountAddress: "0x" + "1".repeat(40), symbol: SYMBOL, maxTradeSizes: { buy: "120", sell: "240" },
  leverage: { value: 3, type: "cross" } });

async function harness(t, respond, readRespond) {
  const original = Object.fromEntries(["localStorage", "location", "fetch", "confirm"].map(k => [k, globalThis[k]]));
  t.after(() => Object.assign(globalThis, original));
  const saved = new Map([["kt.exchange", "hyperliquid"], ["kt.symbol", SYMBOL]]);
  globalThis.localStorage = { getItem: k => saved.get(k) ?? null, setItem: (k, v) => saved.set(k, v) };
  globalThis.location = { reload() {} };
  const prompts = [];
  globalThis.confirm = message => { prompts.push(message); return true; };
  const posts = [];
  const postPaths = [];
  const reads = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === "/api/session") return Response.json(SESSION);
    if ((options.method || "GET").toUpperCase() === "POST") {
      postPaths.push(url);
      posts.push(JSON.parse(options.body));
      const response = respond(posts.length);
      return response instanceof Response ? response : Response.json(response);
    }
    reads.push(url);
    return Response.json(readRespond?.(url) || { state: "current", orders: [], positions: [], items: [] });
  };
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const api = await vite.ssrLoadModule("/src/api.js");
  const { default: HyperliquidTicket, HyperliquidRiskPreview } = await vite.ssrLoadModule("/src/components/HyperliquidTicket.jsx");
  const { default: BottomTabs } = await vite.ssrLoadModule("/src/components/BottomTabs.jsx");
  const toasts = [];
  store.setState({ toast: (msg, kind) => toasts.push([kind, String(msg).slice(0, 200)]) });
  store.setState({
    symbol: SYMBOL, otype: "lmt", canTrade: false, armed: false, ticketBusy: false, hlReceipt: null,
    signedTrading: "off", hlRecoveryLoaded: true, hlServerUnresolved: [], instruments: [{ symbol: SYMBOL, contractValueTradePrecision: 2 }],
    tickers: { [SYMBOL]: { symbol: SYMBOL, last: 0.6, markPrice: 0.61 } },
  });
  store.getState().setSignedTradingMode("off");
  store.getState().loadHyperliquidRecovery("mainnet", "0x" + "1".repeat(40));
  const render = () => {
    Object.assign(store.getInitialState(), store.getState());
    return renderToStaticMarkup(React.createElement(HyperliquidTicket));
  };
  const renderOrders = () => {
    Object.assign(store.getInitialState(), store.getState());
    return renderToStaticMarkup(React.createElement(BottomTabs));
  };
  const renderRisk = props => renderToStaticMarkup(React.createElement(HyperliquidRiskPreview, props));
  return { store, api, render, renderRisk, renderOrders, posts, postPaths, reads, toasts, prompts };
}

const resting = () => ({
  exchange: "hyperliquid", type: "order", live: true, outcome: "confirmed",
  action: { type: "order", orders: [{ a: 1, b: true, p: "0.6", s: "20", r: false, t: { limit: { tif: "Gtc" } } }], grouping: "na" },
  rows: [{ state: "resting", oid: 77738308 }],
});

test("Chart protection callback sends one confirmed exact-ID native intent and preserves the ladder", async t => {
  const { store, posts, postPaths, prompts } = await harness(t, resting);
  const position = { symbol: SYMBOL, side: "long", size: 10, sizeExact: "10", price: 0.6 };
  const target = { symbol: SYMBOL, order_id: "9223372036854775813", cliOrdId: "0x" + "b".repeat(32), side: "sell",
    orderType: "stp", triggerKind: "sl", triggerMarket: true, reduceOnly: true, unfilledSizeExact: "3", stopPrice: 0.5, limitPrice: 0.5 };
  const sibling = { ...target, order_id: "321", unfilledSizeExact: "2" };
  store.setState({ armed: true, positions: [position], orders: [target, sibling],
    dataStatus: { orders: { state: "current" }, positions: { state: "current" } } });
  store.getState().setSignedTradingMode("mainnet");
  const previous = globalThis.window;
  globalThis.window = {};
  t.after(() => { store.getState().unbindChart(); globalThis.window = previous; });
  const controller = { setOverlayMap() {}, symbols: () => [SYMBOL] };
  store.getState().bindChart(controller);
  await controller.onProtectionDrop({ symbol: SYMBOL, kind: "sl", price: 0.4, positionSnapshot: position,
    order: { orderId: target.order_id, snapshot: target, positionSnapshot: position } });
  assert.equal(posts.length, 1);
  assert.match(postPaths[0], /^\/api\/chart-order\?/);
  assert.equal(posts[0].target.order_id, target.order_id);
  assert.equal(posts[0].target.unfilledSizeExact, "3");
  assert.equal(posts[0].acknowledgeReplacement, true);
  assert.equal(posts[0].expectedArmed, true);
  assert.match(prompts[0], /ALWAYS places the replacement/);
  assert.equal(sibling.unfilledSizeExact, "2");
  assert.equal(store.getState().hlReceipt.kind, "chart");
});

test("Position handles request native full-position protection with an explicit future-size confirmation", async t => {
  const { store, posts, prompts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ positions: [{ symbol: SYMBOL, side: "long", size: 10, sizeExact: "10", price: 0.6 }], orders: [],
    dataStatus: { orders: { state: "current" }, positions: { state: "current" } } });
  await store.getState().adjustProtection(SYMBOL, "tp", 0.8);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].fullPosition, true);
  assert.equal(posts[0].acknowledgeFullPosition, true);
  assert.match(prompts[0], /follows future position increases and decreases/);
});

test("Fixed trigger conversion is confirmed, refuses ladders, and native order size renders from current position", async t => {
  const { store, posts, renderOrders } = await harness(t, resting);
  const position = { symbol: SYMBOL, side: "long", size: 10, sizeExact: "10", price: 0.6 };
  const target = { symbol: SYMBOL, order_id: "456", side: "sell", orderType: "take_profit", triggerKind: "tp",
    triggerMarket: true, reduceOnly: true, unfilledSizeExact: "3", stopPrice: 0.8, limitPrice: 0.8 };
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ tab: "orders", positions: [position], orders: [target, { ...target, order_id: "789" }],
    dataStatus: { orders: { state: "current" }, positions: { state: "current" } } });
  const convert = () => store.getState().submitHyperliquidProtection(SYMBOL, "tp", 0.8,
    { snapshot: target, orderId: target.order_id }, null, true);
  await convert();
  assert.equal(posts.length, 0);
  store.setState({ orders: [target] });
  assert.match(renderOrders(), />Full position<\/button>/);
  globalThis.confirm = () => false;
  await convert();
  assert.equal(posts.length, 0);
  globalThis.confirm = () => true;
  await convert();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].target.order_id, "456");
  assert.equal(posts[0].target.unfilledSizeExact, "3");
  assert.equal(posts[0].fullPosition, true);
  store.setState({ positions: [{ ...position, size: 20, sizeExact: "20" }],
    orders: [{ ...target, positionTpsl: true, size: 0, unfilledSizeExact: "0" }] });
  assert.match(renderOrders(), /Full position · 20/);
  assert.doesNotMatch(renderOrders(), />Full position<\/button>/);
});

test("Position chart handles refuse existing ladders, stale data, and declined confirmation", async t => {
  const { store, posts } = await harness(t, resting);
  const position = { symbol: SYMBOL, side: "long", size: 10, sizeExact: "10", price: 0.6 };
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ positions: [position], orders: [{ symbol: SYMBOL, orderType: "take_profit", reduceOnly: true }],
    dataStatus: { orders: { state: "current" }, positions: { state: "current" } } });
  await store.getState().adjustProtection(SYMBOL, "tp", 0.8);
  assert.equal(posts.length, 0);
  store.setState({ orders: [] });
  globalThis.confirm = () => false;
  await store.getState().adjustProtection(SYMBOL, "tp", 0.8);
  assert.equal(posts.length, 0);
  globalThis.confirm = () => true;
  store.setState({ dataStatus: { orders: { state: "stale" }, positions: { state: "current" } } });
  await store.getState().adjustProtection(SYMBOL, "tp", 0.8);
  assert.equal(posts.length, 0);
});

test("Lost chart response persists through reload and blocks a second drag", async t => {
  const { store, posts } = await harness(t, () => { throw new Error("Lost response"); });
  const position = { symbol: SYMBOL, side: "long", size: 10, sizeExact: "10", price: 0.6 };
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ positions: [position], orders: [],
    dataStatus: { orders: { state: "current" }, positions: { state: "current" } } });
  await store.getState().adjustProtection(SYMBOL, "tp", 0.8);
  store.getState().loadHyperliquidRecovery("mainnet", "0x" + "1".repeat(40));
  assert.equal(store.getState().hlReceipt.kind, "chart");
  assert.equal(store.getState().hlReceipt.outcome, "unknown");
  await store.getState().adjustProtection(SYMBOL, "sl", 0.4);
  assert.equal(posts.length, 1);
});

test("Hyperliquid ticket blocks writes until the backend gate is on", async t => {
  const { store, render, posts } = await harness(t, resting);
  const markup = render();
  assert.match(markup, /Hyperliquid signed trading is off/);
  assert.match(markup, /id="hl-btn-buy" disabled=""/);
  assert.match(markup, /Minimum order value \$10/);
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6, reduceOnly: false });
  assert.equal(posts.length, 0, "no request may leave a gated ticket");
});

test("Legacy bounded close drafts remain reduce-only IOC and require confirmation", async t => {
  const { store, render, renderOrders, posts, prompts, toasts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, tab: "positions", positions: [{ symbol: SYMBOL, side: "long", size: 2, sizeExact: "2" }],
    dataStatus: { positions: { state: "current" } } });
  assert.match(renderOrders(), /class="row-btn sell">Close/);
  store.setState({ hlCloseDraft: { symbol: SYMBOL, side: "sell", size: "2" }, otype: "ioc" });
  assert.equal(posts.length, 0);
  assert.equal(prompts.length, 0);
  const markup = render();
  assert.match(markup, /SELL TO CLOSE/);
  assert.match(markup, /Price bound \(IOC limit\)/);
  assert.doesNotMatch(markup, /id="hl-btn-buy"/);
  assert.match(markup, /id="hl-reduce"[^>]*disabled=""[^>]*checked=""/);
  await store.getState().submitHyperliquidOrder("buy", { size: 1, limitPrice: 0.6 });
  await store.getState().submitHyperliquidOrder("sell", { size: 3, limitPrice: 0.6 });
  assert.equal(posts.length, 0, "wrong side and oversized close drafts must not submit");
  await store.getState().submitHyperliquidOrder("sell", { size: 1, limitPrice: 0.59, reduceOnly: false });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].orderType, "ioc");
  assert.equal(posts[0].closePosition, true);
  assert.equal(posts[0].reduceOnly, true);
  assert.equal(posts[0].limitPrice, 0.59);
  assert.match(prompts[0], /LIVE.*Unfilled quantity may remain/);
  assert.ok(toasts.some(([, text]) => text.includes("does not confirm the position is flat")));
  store.getState().clearHyperliquidClose();
  assert.equal(store.getState().hlCloseDraft, null);
  assert.match(render(), /id="hl-btn-buy"/);
});

test("Hyperliquid close refuses stale position data and clears drafts on a symbol change", async t => {
  const { store, posts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ positions: [{ symbol: SYMBOL, side: "short", size: 2 }], dataStatus: { positions: { state: "unavailable" } } });
  await store.getState().closePosition(SYMBOL);
  assert.equal(store.getState().hlCloseDraft, null);
  store.setState({ dataStatus: { positions: { state: "current" } } });
  await store.getState().closePosition(SYMBOL);
  assert.equal(posts.length, 0, "missing exact size and entry must not submit");
  store.setState({ hlCloseDraft: { symbol: SYMBOL, side: "buy", size: "2" } });
  store.getState().selectSymbol("HL_BTC");
  assert.equal(store.getState().hlCloseDraft, null);
  assert.equal(posts.length, 0);
});

test("Close directly submits one confirmed market reduction for the clicked row without opening a ticket", async t => {
  const { store, posts, prompts, reads, toasts } = await harness(t, resting,
    () => ({ state: "current", exchange: "hyperliquid", positions: [], orders: [] }));
  store.getState().setSignedTradingMode("mainnet");
  const symbol = "HL_BTC";
  store.setState({ armed: true, otype: "take_profit", positions: [{ symbol, side: "long", size: 2, sizeExact: "2", price: 100 }],
    instruments: [{ symbol, contractValueTradePrecision: 5 }], dataStatus: { positions: { state: "current" } } });
  await Promise.all([store.getState().closePosition(symbol), store.getState().closePosition(symbol)]);
  assert.equal(posts.length, 1);
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /LIVE.*MARKET.*slippage limit 0.5%/);
  assert.equal(posts[0].symbol, symbol);
  assert.equal(posts[0].side, "sell");
  assert.equal(posts[0].orderType, "mkt");
  assert.equal(posts[0].closePosition, true);
  assert.equal(posts[0].reduceOnly, true);
  assert.equal(posts[0].expectedArmed, true);
  assert.deepEqual(posts[0].position, { side: "long", sizeExact: "2", price: 100 });
  assert.equal(posts[0].slippagePercent, 0.5);
  assert.equal(posts[0].limitPrice, undefined);
  assert.equal(store.getState().symbol, SYMBOL, "closing another row must not change the chart or ticket symbol");
  assert.equal(store.getState().hlCloseDraft, null);
  assert.ok(reads.some(url => url.includes("/api/positions?fresh=1")));
  assert.equal(store.getState().hlReceipt.closeObservation.state, "flat");
  assert.ok(toasts.some(([, text]) => text.includes("flat on fresh exchange readback")));
});

test("Market close reports residual short exposure and never retries it", async t => {
  const position = { symbol: SYMBOL, side: "short", size: 2, sizeExact: "2", price: 0.6 };
  const { store, posts, toasts } = await harness(t, resting,
    () => ({ state: "current", exchange: "hyperliquid", positions: [{ ...position, size: 0.5, sizeExact: "0.5" }], orders: [] }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, positions: [position], dataStatus: { positions: { state: "current" } } });
  await store.getState().closePosition(SYMBOL);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].side, "buy");
  assert.equal(store.getState().hlReceipt.closeObservation.state, "remaining");
  assert.equal(store.getState().hlReceipt.closeObservation.sizeExact, "0.5");
  assert.ok(toasts.some(([, text]) => text.includes("0.5 contracts remain")));
});

test("Declined and unknown closes do not retry or claim flatness", async t => {
  const { store, posts, reads } = await harness(t, () => ({ outcome: "unknown", uncertain: true }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ positions: [{ symbol: SYMBOL, side: "long", size: 2, sizeExact: "2", price: 0.6 }],
    dataStatus: { positions: { state: "current" } } });
  globalThis.confirm = () => false;
  await store.getState().closePosition(SYMBOL);
  assert.equal(posts.length, 0);
  globalThis.confirm = () => true;
  await store.getState().closePosition(SYMBOL);
  await store.getState().closePosition(SYMBOL);
  assert.equal(posts.length, 1);
  assert.equal(store.getState().hlReceipt.outcome, "unknown");
  assert.equal(store.getState().hlReceipt.closeObservation, undefined);
  assert.ok(!reads.some(url => url.includes("fresh=1")));
});

test("Simulated market close keeps its receipt and never claims an executed close", async t => {
  const { store, posts, reads, prompts } = await harness(t, () => ({ outcome: "simulated", simulated: true }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: false, positions: [{ symbol: SYMBOL, side: "long", size: 2, sizeExact: "2", price: 0.6 }],
    dataStatus: { positions: { state: "current" } } });
  await store.getState().closePosition(SYMBOL);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].expectedArmed, false);
  assert.match(prompts[0], /^SIMULATED/);
  assert.equal(store.getState().hlReceipt.outcome, "simulated");
  assert.equal(store.getState().hlReceipt.closeObservation, undefined);
  assert.ok(!reads.some(url => url.includes("fresh=1")));
});

test("Failed fresh close readback cannot report a flat position", async t => {
  const { store, posts, toasts } = await harness(t, resting,
    () => ({ state: "unavailable", exchange: "hyperliquid", positions: [] }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ positions: [{ symbol: SYMBOL, side: "long", size: 2, sizeExact: "2", price: 0.6 }],
    dataStatus: { positions: { state: "current" } } });
  await store.getState().closePosition(SYMBOL);
  assert.equal(posts.length, 1);
  assert.equal(store.getState().hlReceipt.closeObservation, undefined);
  assert.ok(toasts.some(([, text]) => text.includes("position verification failed")));
});

test("saved cancellation status can be checked with trading off, without resending cancellation", async t => {
  const target = "12345678901234567890";
  const item = { requestId: "cancel-original", body: { symbol: SYMBOL, orderIds: [target] }, targets: [target],
    result: { outcome: "unknown" }, evidence: {} };
  const evidence = { requestId: item.requestId, target, state: "observed", canReplace: false,
    checkedAt: "2026-09-14T12:00:00Z", status: { orderStatus: "canceled" } };
  const { store, posts, renderOrders } = await harness(t, () => evidence, url =>
    url.startsWith("/api/cancel-recovery") ? { state: "current", items: [item], hasMore: false } : null);
  store.setState({ tab: "orders" });
  await store.getState().loadHyperliquidCancellations();
  store.getState().inspectHyperliquidCancellation(store.getState().hlCancelHistory[0]);
  await Promise.all([store.getState().reconcileHyperliquidCancel(target), store.getState().reconcileHyperliquidCancel(target)]);
  assert.deepEqual(posts, [{ requestId: "cancel-original", target }]);
  assert.equal(store.getState().hlCancelReceipt.outcome, "unknown", "original execution receipt is immutable");
  assert.match(renderOrders(), /Observed: canceled/);
  assert.match(renderOrders(), /No retry or replacement/);
  await store.getState().reconcileHyperliquidCancel("999");
  store.getState().inspectHyperliquidCancellation({ ...item, result: { outcome: "simulated" } });
  await store.getState().reconcileHyperliquidCancel(target);
  assert.equal(posts.length, 1);
});

test("cancellation lookup finds exact older receipts and rejects mismatched responses", async t => {
  const item = { requestId: "cancel:older", body: { symbol: SYMBOL }, targets: ["123"], result: {} };
  let response = { state: "current", items: [item], hasMore: false };
  const { store, reads, posts, renderOrders } = await harness(t, resting, url =>
    url.startsWith("/api/cancel-recovery") ? response : null);
  store.setState({ tab: "orders" });
  await store.getState().loadHyperliquidCancellations("  cancel:older  ");
  assert.ok(reads.some(url => url.includes("requestId=cancel%3Aolder")));
  assert.equal(store.getState().hlCancelHistory[0].requestId, item.requestId);
  assert.match(renderOrders(), /Find an older cancellation by request ID/);
  response = { state: "current", items: [{ ...item, requestId: "cancel-other" }], hasMore: false };
  await store.getState().loadHyperliquidCancellations(item.requestId);
  assert.match(store.getState().hlCancelHistoryNote, /refresh failed/);
  assert.equal(store.getState().hlCancelHistory[0].requestId, item.requestId);
  response = { state: "current", items: [], hasMore: false };
  await store.getState().loadHyperliquidCancellations(item.requestId);
  assert.match(store.getState().hlCancelHistoryNote, /does not prove no cancellation/);
  const count = reads.length;
  await store.getState().loadHyperliquidCancellations("bad/input");
  assert.equal(reads.length, count);
  assert.equal(posts.length, 0);
});

test("restoring a partial cancellation keeps row errors and leaves missing results unknown", async t => {
  const { store } = await harness(t, resting);
  store.getState().inspectHyperliquidCancellation({ requestId: "cancel-partial", body: { symbol: SYMBOL },
    targets: ["123", "456"], result: { outcome: "partial", cancelResults: [
      { orderId: "123", outcome: "rejected", error: "already filled" }] } });
  const receipt = store.getState().hlCancelReceipt;
  assert.equal(receipt.outcome, "partial");
  assert.equal(receipt.results[0].error, "already filled");
  assert.equal(receipt.results[1].outcome, "unknown");
});

test("Market buy and sell need no manual price and retain slippage in the recovery intent", async t => {
  const { store, render, posts, prompts } = await harness(t, resting);
  store.setState({ otype: "mkt" });
  const markup = render();
  assert.match(markup, /MARKET BUY \/ LONG/);
  assert.match(markup, /MARKET SELL \/ SHORT/);
  assert.match(markup, /hl-slippage/);
  assert.doesNotMatch(markup, /id="hl-limit"/);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true });
  for (const side of ["buy", "sell"]) {
    await store.getState().submitHyperliquidOrder(side, { size: 20, slippagePercent: 0.5, maxNotional: "12" });
    const sent = posts.at(-1);
    assert.equal(sent.orderType, "mkt");
    assert.equal(sent.side, side);
    assert.equal(sent.slippagePercent, 0.5);
    assert.equal(sent.limitPrice, undefined);
    assert.equal(store.getState().hlReceipt.body.cloid, sent.cloid);
    assert.equal(store.getState().hlReceipt.body.slippagePercent, 0.5);
  }
  assert.equal(posts.length, 2);
  assert.match(prompts[0], /LIVE MARKET BUY/);
  assert.match(prompts[1], /LIVE MARKET SELL/);
  await store.getState().submitHyperliquidOrder("buy", { size: 20, slippagePercent: 10 });
  assert.equal(posts.length, 2);
  globalThis.confirm = () => false;
  await store.getState().submitHyperliquidOrder("buy", { size: 20, slippagePercent: 0.5 });
  assert.equal(posts.length, 2);
});

test("Hyperliquid positions show read-only stop observations and withdraw them for stale data", async t => {
  const { store, renderOrders, posts } = await harness(t, resting);
  store.setState({ tab: "positions", positions: [{ symbol: SYMBOL, side: "long", size: 2, sizeExact: "2", price: 0.6 }],
    orders: [], dataStatus: { positions: { state: "current" }, orders: { state: "current" } } });
  assert.match(renderOrders(), /NO STOP OBSERVED/);
  store.setState({ orders: [{ symbol: SYMBOL, order_id: "123", side: "sell", reduceOnly: true,
    orderType: "stp", stopPrice: 0.5, unfilledSizeExact: "2" }] });
  assert.match(renderOrders(), /Full-size stop observed/);
  store.setState({ dataStatus: { positions: { state: "current" }, orders: { state: "stale" } } });
  const stale = renderOrders();
  assert.match(stale, /Stop coverage unknown/);
  assert.doesNotMatch(stale, /Full-size stop observed|NO STOP OBSERVED/);
  store.setState({ exchange: "kraken" });
  assert.doesNotMatch(renderOrders(), /Stop observation|Stop coverage unknown/);
  assert.equal(posts.length, 0);
});

test("Hyperliquid Grid previews with trading off, and the gate still guards placement", async t => {
  const response = { previewOnly: false, ready: true, armed: false,
    plan: { orders: [], previewHash: "fixture", warnings: [] } };
  const { store, api, render, posts } = await harness(t, () => response);
  store.setState({ otype: "grid" });
  const markup = render();
  assert.match(markup, /Check current orders and quotes/, "the venue keeps its read-only preflight");
  assert.match(markup, /one signed batch/, "the note says how Hyperliquid places a grid");
  const action = { symbol: SYMBOL, side: "buy", startPrice: 0.6, endPrice: 0.5, orders: 3, notional: 100 };
  await api.api("/api/grid/preview", { method: "POST", body: action });
  assert.equal(posts.length, 1, "preview works with signed trading off");
  await api.api("/api/grid/preview", { method: "POST", body: { ...action, checkCurrentOrders: true } });
  assert.equal(posts.at(-1).checkCurrentOrders, true);

  // Placement is refused by the request layer while the backend gate is off.
  await assert.rejects(api.api("/api/grid", { method: "POST", body: action }));
  assert.equal(posts.length, 2);

  // Gate on, but a preview that planned no rungs still places nothing.
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true });
  await store.getState().submitGrid(action, { ...response, armed: true });
  assert.equal(posts.length, 2, "a ready preview with no rungs mints no identities and sends nothing");
  assert.equal(store.getState().hlReceipt, null);
});

test("USD budget is retained in the journal body and rounded-price rejection is not retried", async t => {
  const { store, posts } = await harness(t, () => Response.json({ outcome: "rejected", error: "Rounded order exceeds the USD notional budget" }, { status: 400 }));
  store.getState().setSignedTradingMode("mainnet");
  for (const maximum of [true, null, -1, Infinity]) {
    await store.getState().submitHyperliquidOrder("buy", { size: 100, limitPrice: 0.60006, maxNotional: maximum });
  }
  assert.equal(posts.length, 0);
  await store.getState().submitHyperliquidOrder("buy", { size: 100, limitPrice: 0.60006, maxNotional: "60.006" });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].maxNotional, "60.006");
  assert.equal(store.getState().hlReceipt.body.maxNotional, "60.006");
  assert.equal(store.getState().hlReceipt.outcome, "rejected");
  assert.equal(store.getState().hlReceipt.uncertain, false);
});

test("account cancellation freezes multiple symbols in one batch and excludes later orders", async t => {
  const ids = ["12345678901234567890", "2"];
  const { store, posts, prompts, renderOrders } = await harness(t, () => ({ outcome: "partial",
    cancelResults: [{ orderId: ids[0], outcome: "confirmed" }, { orderId: ids[1], outcome: "rejected", error: "already filled" }] }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, tab: "orders", dataStatus: { orders: { state: "current" } },
    orders: [{ symbol: SYMBOL, order_id: ids[0] }, { symbol: "HL_BTC", order_id: ids[1] }] });
  const confirm = globalThis.confirm;
  globalThis.confirm = message => {
    store.setState({ orders: [...store.getState().orders, { symbol: "HL_ETH", order_id: "3" }] });
    return confirm(message);
  };
  await Promise.all([store.getState().cancelHyperliquidOrders(true), store.getState().cancelHyperliquidOrders(true)]);
  assert.equal(posts.length, 1);
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /all supported native-perp symbols in this Hyperliquid account/);
  assert.match(prompts[0], /includes TP\/SL orders; positions will remain open/);
  assert.deepEqual(posts[0].targets, [{ symbol: SYMBOL, orderId: ids[0] }, { symbol: "HL_BTC", orderId: ids[1] }]);
  assert.equal(posts[0].symbol, undefined);
  assert.equal(store.getState().hlCancelReceipt.results[1].outcome, "rejected");
  assert.match(renderOrders(), /HL_BTC · 2: rejected/);
  store.setState({ orders: [{ symbol: "PF_XBTUSD", order_id: "4" }], dataStatus: { orders: { state: "current" } } });
  await store.getState().cancelHyperliquidOrders(true);
  await store.getState().cancelHyperliquidOrders("false");
  assert.equal(posts.length, 1);
});

test("prepared batch recovery persists partial progress and only clears complete identities", async t => {
  const cloids = ["0x" + "a".repeat(32), "0x" + "b".repeat(32)];
  const requestId = "batch-original";
  const { store, posts, render } = await harness(t, count => ({ requestId, batch: true, cloids, canReplace: false,
    outcome: count === 1 ? "unknown" : "reconciled", remaining: count === 1 ? 1 : 0,
    targets: { [cloids[0]]: { state: "observed" }, ...(count === 1 ? {} : { [cloids[1]]: { state: "rejected" } }) } }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ otype: "grid" });
  store.getState().restoreHyperliquidRequest({ requestId, batch: true, cloids, outcome: "unknown", uncertain: true,
    body: { symbol: SYMBOL, orders: 2 }, endpoint: "/api/grid" });
  const { readReceipt, hasRecoveryIdentity } = await import("./hyperliquid-receipt.js");
  assert.equal(readReceipt(store.getState().hlReceiptKey).batch, true);
  assert.match(render(), /Prepared batch: 2 orders for HL_APT/);
  await store.getState().reconcileHyperliquidOrder();
  assert.equal(store.getState().hlReceipt.uncertain, true);
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  assert.equal(posts.length, 1, "partial recovery blocks a new order");
  await store.getState().reconcileHyperliquidOrder();
  assert.equal(store.getState().hlReceipt.outcome, "reconciled");
  assert.equal(readReceipt(store.getState().hlReceiptKey).uncertain, false);
  assert.equal(posts.length, 2, "only the two local reconciliation requests were sent");
  assert.equal(hasRecoveryIdentity({ batch: true, cloids: [cloids[0], cloids[0]] }), false);
});

test("batch recovery cannot claim completion with missing evidence", async t => {
  const cloids = ["0x" + "a".repeat(32), "0x" + "b".repeat(32)];
  const { store } = await harness(t, () => ({ requestId: "batch-original", batch: true, cloids,
    outcome: "reconciled", remaining: 0, targets: { [cloids[0]]: { state: "observed" } } }));
  store.getState().restoreHyperliquidRequest({ requestId: "batch-original", batch: true, cloids, outcome: "unknown", uncertain: true,
    body: { symbol: SYMBOL } });
  await store.getState().reconcileHyperliquidOrder();
  assert.equal(store.getState().hlReceipt.outcome, "unknown");
  assert.equal(store.getState().hlReceipt.uncertain, true);
});

test("trigger risk preview distinguishes an estimate from a guaranteed stop", async t => {
  const { renderRisk, posts } = await harness(t, resting);
  const props = { position: { side: "long", price: 100, size: 10 }, quantity: 20, price: 90, current: true };
  const markup = renderRisk(props);
  assert.match(markup, /SELL reduction/);
  assert.match(markup, /gross PnL ≈ -\$100.00/);
  assert.match(markup, /100.0% of the current position/);
  assert.match(markup, /Requested size exceeds/);
  assert.match(markup, /not a loss cap/);
  assert.match(markup, /remain unfilled/);
  const stale = renderRisk({ ...props, current: false });
  assert.match(stale, /Estimate unavailable/);
  assert.doesNotMatch(stale, /gross PnL/);
  assert.equal(posts.length, 0);
});

test("Percentage buttons use current exchange capacity without guessing from balance", async t => {
  const { store, render } = await harness(t, resting);
  store.setState({ account: { balanceValue: 10000, withdrawable: 10000, availableMargin: null },
    instruments: [{ symbol: SYMBOL, contractValueTradePrecision: 2, maxLeverage: 5 }] });
  let markup = render();
  assert.match(markup, /id="hl-usd"/);
  assert.match(markup, /disabled="" data-pct="25"/);
  store.setState({ hlCapacity: { symbol: SYMBOL, scopeKey: store.getState().hlReceiptKey, fetchedAt: Date.now(), maxTradeSizes: { buy: "20", sell: "30" }, leverage: { value: 3, type: "cross" } } });
  markup = render();
  assert.doesNotMatch(markup, /disabled="" data-pct="25"/);
  assert.match(markup, /Exchange maximum: Buy 20, Sell 30/);
  assert.match(markup, /Changes actual exchange leverage/);
  assert.match(markup, /disabled="" data-lev="10"/);
  store.setState({ hlCapacity: { ...store.getState().hlCapacity, fetchedAt: Date.now() - 20000 } });
  assert.match(render(), /disabled="" data-pct="100"/);
  assert.equal(localStorage.getItem("kt.lev"), null);
});

test("Capacity reads reject foreign identities and late responses after a symbol change", async t => {
  let data = capacityFixture();
  const { store } = await harness(t, resting, url => url.includes("trading-capacity") ? data : undefined);
  await store.getState().refreshHyperliquidCapacity();
  assert.ok(store.getState().hlCapacity, `${store.getState().hlCapacityError}; exchange=${store.getState().exchange}; key=${store.getState().hlReceiptKey}`);
  assert.equal(store.getState().hlCapacity.maxTradeSizes.sell, "240");
  data = { ...data, accountAddress: "0x" + "2".repeat(40) };
  await store.getState().refreshHyperliquidCapacity();
  assert.equal(store.getState().hlCapacity, null);
  const previousFetch = globalThis.fetch;
  let release;
  globalThis.fetch = (url, options) => url.includes("trading-capacity")
    ? new Promise(resolve => { release = () => resolve(Response.json(capacityFixture())); }) : previousFetch(url, options);
  const pending = store.getState().refreshHyperliquidCapacity();
  await new Promise(resolve => setImmediate(resolve));
  store.setState({ symbol: "HL_BTC" });
  release();
  await pending;
  assert.equal(store.getState().hlCapacity, null);
});

test("Leverage control submits an actual venue setting, preserves mode and never rewrites Kraken preferences", async t => {
  let data = capacityFixture();
  const { store, posts, prompts, render } = await harness(t, () => {
    data = { ...data, leverage: { ...data.leverage, value: 5 } };
    return { type: "updateLeverage", outcome: "confirmed", action: { type: "updateLeverage", asset: 1, isCross: true, leverage: 5 } };
  }, url => url.includes("trading-capacity") ? data : undefined);
  store.setState({ instruments: [{ symbol: SYMBOL, contractValueTradePrecision: 2, maxLeverage: 10 }], armed: true });
  store.getState().setSignedTradingMode("mainnet");
  await store.getState().refreshHyperliquidCapacity();
  const confirm = globalThis.confirm;
  globalThis.confirm = () => false;
  await store.getState().submitHyperliquidLeverage(5);
  assert.equal(posts.length, 0);
  globalThis.confirm = confirm;
  const fresh = store.getState().hlCapacity;
  store.setState({ hlCapacity: { ...fresh, fetchedAt: Date.now() - 20000 } });
  await store.getState().submitHyperliquidLeverage(5);
  assert.equal(posts.length, 0);
  store.setState({ hlCapacity: fresh });
  await store.getState().submitHyperliquidLeverage(5);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].leverage, 5);
  assert.equal(posts[0].expectedLeverage, 3);
  assert.equal(posts[0].cross, true);
  assert.equal(posts[0].expectedArmed, true);
  assert.equal(posts[0].cloid, undefined);
  assert.equal(store.getState().hlReceipt.kind, "leverage");
  assert.match(prompts[0], /LIVE exchange leverage change/);
  assert.doesNotMatch(render(), /Check lifecycle/);
  assert.equal(localStorage.getItem("kt.lev"), null);
});

test("Unknown leverage survives reload, blocks new orders and needs expired matching readback", async t => {
  let recovering = false;
  const { store, posts, render } = await harness(t, () => {
    if (!recovering) throw new Error("Response lost");
    return { kind: "leverage", state: "current", outcome: "reconciled", requestId: store.getState().hlReceipt.requestId,
      expiresAfter: Date.now() - 10000, exchangeTime: Date.now(), canReplace: false,
      capacity: { ...capacityFixture(), leverage: { type: "cross", value: 5 } } };
  }, url => url.includes("trading-capacity") ? capacityFixture() : undefined);
  store.setState({ instruments: [{ symbol: SYMBOL, contractValueTradePrecision: 2, maxLeverage: 10 }], armed: true });
  store.getState().setSignedTradingMode("mainnet");
  await store.getState().refreshHyperliquidCapacity();
  await store.getState().submitHyperliquidLeverage(5);
  assert.equal(store.getState().hlReceipt.outcome, "unknown");
  store.getState().loadHyperliquidRecovery("mainnet", "0x" + "1".repeat(40));
  assert.equal(store.getState().hlReceipt.kind, "leverage");
  assert.match(render(), /Check leverage after request expiry/);
  await store.getState().submitHyperliquidOrder("buy", { size: 1, limitPrice: 1 });
  assert.equal(posts.length, 1);
  recovering = true;
  await store.getState().reconcileHyperliquidOrder();
  assert.equal(store.getState().hlReceipt.outcome, "reconciled");
  assert.equal(posts.length, 2, "Only one setting request and one readback, no setting retry");
});

test("Percentage sizing preserves the requested fraction and expected exchange leverage", async t => {
  const { store, posts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  await store.getState().submitHyperliquidOrder("buy", { size: 30, limitPrice: 0.6,
    quickPercent: 25, expectedLeverage: 3, expectedMarginMode: "cross" });
  assert.equal(posts[0].quickPercent, 25);
  assert.equal(posts[0].expectedLeverage, 3);
  assert.equal(posts[0].size, 30);
});

test("Hyperliquid ticket submits the exact wire body the server validates", async t => {
  const { store, render, posts, toasts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  assert.equal(store.getState().canTrade, true);
  const unarmed = render();
  assert.match(unarmed, /DISARMED — the exact order is validated/);

  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6, reduceOnly: false });
  assert.equal(posts.length, 1, `toasts=${JSON.stringify(toasts)}`);
  const body = posts[0];
  assert.equal(body.symbol, SYMBOL);
  assert.equal(body.side, "buy");
  assert.equal(body.orderType, "lmt");
  assert.equal(body.size, 20);
  assert.equal(body.limitPrice, 0.6);
  assert.equal(body.reduceOnly, false);
  assert.equal(body.stopPrice, undefined);
  assert.match(body.requestId, /^[A-Za-z0-9._:-]{8,100}$/);
  assert.match(body.cloid, /^0x[0-9a-f]{32}$/);
  assert.equal(store.getState().hlReceipt?.outcome, "confirmed", `toasts=${JSON.stringify(toasts)} posts=${posts.length}`);

  store.setState({ armed: true });
  assert.match(render(), /ARMED — a confirmed click signs and sends a real order/);
  assert.match(render(), /id="hl-btn-buy"/);
});

test("trigger types force reduce-only because the venue refuses them otherwise", async t => {
  const { store, posts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, otype: "take_profit" });
  await store.getState().submitHyperliquidOrder("sell", { size: 5, stopPrice: 0.75, reduceOnly: false });
  const body = posts[0];
  assert.equal(body.orderType, "take_profit");
  assert.equal(body.stopPrice, 0.75);
  assert.equal(body.reduceOnly, true);
  assert.equal(body.limitPrice, undefined);
});

test("invalid input never reaches the network", async t => {
  const { store, posts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true });
  await store.getState().submitHyperliquidOrder("buy", { size: 0, limitPrice: 1, reduceOnly: false });
  await store.getState().submitHyperliquidOrder("buy", { size: 1, limitPrice: 0, reduceOnly: false });
  await store.getState().submitHyperliquidOrder("buy", { size: 1, limitPrice: NaN, reduceOnly: false });
  store.setState({ otype: "stp" });
  await store.getState().submitHyperliquidOrder("sell", { size: 1, stopPrice: 0, reduceOnly: true });
  assert.equal(posts.length, 0);
});

test("an unknown outcome warns instead of inviting a blind retry", async t => {
  const { store, render, toasts } = await harness(t, () => ({
    exchange: "hyperliquid", type: "order", live: true, outcome: "unknown", uncertain: true,
    error: "Exchange request failed: timeout", action: { type: "order", orders: [] }, rows: [],
  }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true });
  await store.getState().submitHyperliquidOrder("buy", { size: 5, limitPrice: 0.6, reduceOnly: false });
  assert.equal(store.getState().hlReceipt?.outcome, "unknown", `toasts=${JSON.stringify(toasts)}`);
  assert.match(render(), /verify on Hyperliquid before retrying/i);
});

test("lost response persists identity through reload and blocks a second submission", async t => {
  const { store, posts } = await harness(t, () => { throw new Error("response lost"); });
  store.getState().setSignedTradingMode("mainnet");
  const order = { size: 20, limitPrice: 0.6, reduceOnly: false };
  await store.getState().submitHyperliquidOrder("buy", order);
  const pending = store.getState().hlReceipt;
  assert.equal(pending.outcome, "unknown");
  assert.equal(pending.requestId, posts[0].requestId);
  assert.equal(pending.cloid, posts[0].cloid);
  store.setState({ hlReceipt: null });
  store.getState().loadHyperliquidRecovery("mainnet", "0x" + "1".repeat(40));
  assert.equal(store.getState().hlReceipt.requestId, pending.requestId);
  await store.getState().submitHyperliquidOrder("buy", order);
  assert.equal(posts.length, 1);
  await store.getState().reconcileHyperliquidOrder();
  assert.equal(store.getState().hlReceipt.outcome, "unknown", "no match must not release the block");
  store.getState().loadHyperliquidRecovery("testnet", "0x" + "1".repeat(40));
  assert.equal(store.getState().hlReceipt, null, "network receipts must stay isolated");
});

test("server readback resolves the saved submission without another order POST", async t => {
  let cloid;
  const { store, posts } = await harness(t, count => {
    if (count === 1) throw new Error("lost response");
    return { state: "current", outcome: "reconciled", status: { found: true, cliOrdId: cloid,
      symbol: SYMBOL, order_id: "12345678901234567890", orderStatus: "open" } };
  });
  store.getState().setSignedTradingMode("mainnet");
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  cloid = store.getState().hlReceipt.cloid;
  await store.getState().reconcileHyperliquidOrder();
  assert.equal(store.getState().hlReceipt.outcome, "reconciled");
  assert.equal(store.getState().hlReceipt.status.order_id, "12345678901234567890");
  assert.equal(posts.length, 2, "one order POST plus one local reconciliation POST");
  assert.deepEqual(posts[1], { requestId: posts[0].requestId }, "recovery sends no exchange action");
});

test("a server-side unresolved guard does not strand a new local receipt", async t => {
  const prior = { requestId: "prior-browser-request", cloid: "0x" + "c".repeat(32),
    body: { symbol: SYMBOL }, outcome: "unknown" };
  const { store, posts } = await harness(t, () => Response.json({ outcome: "rejected",
    error: "Resolve prior submission", unresolvedRequestId: prior.requestId }, { status: 409 }),
    url => url.startsWith("/api/execution-recovery") ? { state: "current", items: [prior] } : null);
  store.getState().setSignedTradingMode("mainnet");
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  assert.equal(posts.length, 1);
  assert.equal(store.getState().hlReceipt.outcome, "rejected", "the rejected new request was not submitted");
  store.getState().restoreHyperliquidRequest(prior);
  assert.equal(store.getState().hlReceipt.requestId, prior.requestId);
});

test("another browser discovers server intents and cannot place around them", async t => {
  const item = { requestId: "other-browser-request", cloid: "0x" + "b".repeat(32),
    body: { symbol: SYMBOL, size: 20 }, outcome: "unknown", uncertain: true };
  const { store, posts, render } = await harness(t, resting, url =>
    url.startsWith("/api/execution-recovery") ? { state: "current", items: [item], hasMore: false } : null);
  store.setState({ hlRecoveryLoaded: false });
  store.getState().setSignedTradingMode("mainnet");
  await store.getState().refreshHyperliquidRecovery();
  assert.equal(store.getState().hlServerUnresolved.length, 1);
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  assert.equal(posts.length, 0);
  assert.match(render(), /other-browser-request/);
  store.getState().restoreHyperliquidRequest(item);
  assert.equal(store.getState().hlReceipt.requestId, item.requestId);
  store.setState({ hlReceipt: null });
  store.getState().loadHyperliquidRecovery("mainnet", "0x" + "1".repeat(40));
  assert.equal(store.getState().hlReceipt.requestId, item.requestId);
});

test("order fill backfill works with trading off and labels incomplete totals", async t => {
  const oid = "12345678901234567890";
  const { store, posts, render } = await harness(t, () => ({ state: "incomplete", scanComplete: false,
    historyComplete: false, pages: 16, gaps: [{ reason: "not-scanned" }] }), url =>
    url.startsWith("/api/fill-history?") ? { state: "current", orderId: oid, fillCount: 2,
      observedFilledSize: "0.2", averagePrice: "0.6", symbol: SYMBOL, side: "buy", historyComplete: false } : null);
  store.setState({ hlReceipt: { version: 1, requestId: "fixture-fill-request", cloid: "0x" + "a".repeat(32),
    createdAt: new Date(Date.now() - 60000).toISOString(), body: { symbol: SYMBOL, side: "buy" },
    outcome: "reconciled", status: { order_id: oid, orderStatus: "open" } } });
  await store.getState().loadHyperliquidOrderFills();
  assert.equal(posts.length, 1);
  assert.deepEqual(Object.keys(posts[0]).sort(), ["endTime", "startTime"]);
  assert.equal(store.getState().canTrade, false);
  assert.equal(store.getState().hlReceipt.fillSummary.observedFilledSize, "0.2");
  assert.match(render(), /Scan incomplete/);
  assert.match(render(), /totals may be incomplete/);
});

test("lifecycle readback never releases an unresolved submission or sends an order", async t => {
  let matches = true;
  const requestId = "lifecycle-fixture-request";
  const { store, posts, render } = await harness(t, resting, url =>
    url.startsWith("/api/order-lifecycle") ? { state: "current", requestId: matches ? requestId : "wrong-request",
      lifecycle: "filled", fillEvidence: "missing", automationDecision: "wait", canReplace: false,
      reason: "Missing fill evidence", checkedAt: "2026-09-13T00:00:00Z" } : null);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ hlReceipt: { version: 1, requestId, cloid: "0x" + "a".repeat(32),
    body: { symbol: SYMBOL, side: "buy", size: 20 }, outcome: "unknown" } });
  await store.getState().checkHyperliquidLifecycle();
  assert.equal(store.getState().hlReceipt.outcome, "unknown");
  assert.equal(store.getState().hlReceipt.lifecycleReport.fillEvidence, "missing");
  assert.match(render(), /Lifecycle snapshot: filled/);
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  assert.equal(posts.length, 0);
  matches = false;
  await store.getState().checkHyperliquidLifecycle();
  assert.equal(store.getState().hlReceipt.lifecycleReport.state, "unavailable");
  assert.equal(posts.length, 0);
});

test("corrupt recovery data and unknown trading modes fail closed", async t => {
  const { store, posts } = await harness(t, resting);
  assert.equal(store.getState().setSignedTradingMode("enabled"), "off");
  assert.equal(store.getState().canTrade, false);
  store.getState().setSignedTradingMode("mainnet");
  localStorage.setItem(store.getState().hlReceiptKey, "{broken");
  store.getState().loadHyperliquidRecovery("mainnet", "0x" + "1".repeat(40));
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  assert.equal(posts.length, 0);
  assert.ok(store.getState().hlRecoveryError);
});

test("receipt storage failure prevents any order request", async t => {
  const { store, posts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  globalThis.localStorage.setItem = () => { throw new Error("quota"); };
  await store.getState().submitHyperliquidOrder("buy", { size: 20, limitPrice: 0.6 });
  assert.equal(posts.length, 0);
  assert.match(store.getState().hlRecoveryError, /quota/);
});

test("cancel sends 64-bit order ids as strings and never as rounded numbers", async t => {
  const bigId = "12345678901234567890";
  const { store, posts, toasts } = await harness(t, () => ({
    exchange: "hyperliquid", type: "cancel", live: true, outcome: "confirmed",
    action: { type: "cancel", cancels: [{ a: 1, o: 12345678901234567890 }] }, rows: [],
  }));
  store.getState().setSignedTradingMode("off");
  await store.getState().cancelOrder({ symbol: SYMBOL, orderId: bigId });
  assert.equal(posts.length, 0, "a gated venue must not cancel");

  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, dataStatus: { orders: { state: "current" } } });
  const ok = await store.getState().cancelOrder({ symbol: SYMBOL, orderId: bigId });
  assert.equal(ok, true, `toasts=${JSON.stringify(toasts)} posts=${posts.length}`);
  assert.equal(posts[0].orderId, bigId, "the id must survive as an exact decimal string");
  assert.equal(typeof posts[0].orderId, "string");
  assert.equal(posts[0].symbol, SYMBOL);

  await store.getState().cancelOrder({ symbol: SYMBOL, cliOrdId: "0x" + "a".repeat(32), orderId: bigId });
  assert.equal(posts[1].symbol, SYMBOL, "client-id cancellation still needs the venue asset");
  assert.equal(posts[1].cliOrdId, "0x" + "a".repeat(32));
  assert.equal(posts[1].orderId, undefined, "an exact client id is preferred when present");
  await store.getState().cancelOrder({ symbol: SYMBOL, orderId: Number(bigId) });
  assert.equal(posts.length, 2, "already-rounded numeric ids must not reach the network");
});

test("Hyperliquid chart cancellation keeps the symbol and exact ID, and blocks duplicate or stale clicks", async t => {
  const { store, posts, prompts } = await harness(t, () => ({ outcome: "confirmed", type: "cancel" }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, dataStatus: { orders: { state: "current" } } });
  const order = { symbol: SYMBOL, orderId: "12345678901234567890", orderType: "lmt", side: "buy", price: 0.6 };
  await Promise.all([store.getState().cancelChartOrder(order), store.getState().cancelChartOrder(order)]);
  assert.equal(posts.length, 1);
  assert.equal(prompts.length, 1);
  assert.equal(posts[0].symbol, SYMBOL);
  assert.equal(posts[0].orderId, order.orderId);
  store.setState({ dataStatus: { orders: { state: "unavailable" } } });
  await store.getState().cancelChartOrder(order);
  assert.equal(posts.length, 1);
  store.setState({ dataStatus: { orders: { state: "current" } } });
  await store.getState().cancelChartOrder({ ...order, symbol: "PF_APTUSD" });
  assert.equal(posts.length, 1, "a Kraken symbol cannot enter the Hyperliquid cancellation path");
});

test("Hyperliquid row cancellation requires a trading gate and current order data", async t => {
  const { store, renderOrders } = await harness(t, resting);
  store.setState({ tab: "orders", orders: [{ symbol: SYMBOL, order_id: "123", size: 20 }],
    dataStatus: { orders: { state: "current" } } });
  assert.match(renderOrders(), /class="row-btn" disabled="">Cancel/);
  store.getState().setSignedTradingMode("mainnet");
  assert.match(renderOrders(), /class="row-btn">Cancel/);
  store.setState({ dataStatus: { orders: { state: "unavailable" } } });
  assert.match(renderOrders(), /class="row-btn" disabled="">Cancel/);
});

test("symbol bulk cancellation freezes IDs, blocks double clicks, and reports partial results", async t => {
  const ids = ["12345678901234567890", "2"];
  const { store, posts, prompts, renderOrders } = await harness(t, () => ({ outcome: "partial",
    cancelResults: [{ orderId: ids[0], outcome: "confirmed" }, { orderId: ids[1], outcome: "rejected", error: "already filled" }] }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, tab: "orders", dataStatus: { orders: { state: "current" } },
    orders: [...ids.map(order_id => ({ symbol: SYMBOL, order_id })), { symbol: "HL_BTC", order_id: "900" }] });
  let release;
  const barrier = new Promise(resolve => { release = resolve; });
  const fetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.startsWith("/api/cancel")) await barrier;
    return fetch(url, options);
  };
  const first = store.getState().cancelAllForSymbol();
  await new Promise(setImmediate);
  assert.equal(store.getState().bulkBusy, true);
  store.setState({ orders: [...store.getState().orders, { symbol: SYMBOL, order_id: "3" }] });
  await store.getState().cancelAllForSymbol();
  release();
  await first;
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].orderIds, ids);
  assert.equal(posts[0].symbol, SYMBOL);
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /LIVE/);
  assert.equal(store.getState().hlCancelReceipt.outcome, "partial");
  assert.equal(store.getState().bulkBusy, false);
  assert.match(renderOrders(), /12345678901234567890: confirmed/);
  assert.match(renderOrders(), /2: rejected/);
});

test("bulk cancellation rejects stale data and preserves uncertain receipts", async t => {
  const { store, posts, prompts } = await harness(t, () => { throw new Error("response lost"); });
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ orders: [{ symbol: SYMBOL, order_id: "1" }], dataStatus: { orders: { state: "unavailable" } } });
  await store.getState().cancelAllForSymbol();
  assert.equal(posts.length, 0);
  assert.equal(prompts.length, 0);
  store.setState({ dataStatus: { orders: { state: "current" } } });
  await store.getState().cancelAllForSymbol();
  assert.equal(posts.length, 1);
  const receipt = store.getState().hlCancelReceipt;
  assert.equal(receipt.outcome, "unknown");
  assert.equal(receipt.requestId, posts[0].requestId);
  assert.deepEqual(receipt.orderIds, ["1"]);
  assert.equal(receipt.results[0].outcome, "unknown");
});

test("bulk cancel simulation is not reported as a live cancellation", async t => {
  const { store, posts, prompts } = await harness(t, () => ({ outcome: "simulated", simulated: true,
    cancelResults: [{ orderId: "1", outcome: "simulated" }] }));
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: false, orders: [{ symbol: SYMBOL, order_id: "1" }], dataStatus: { orders: { state: "current" } } });
  await store.getState().cancelAllForSymbol();
  assert.equal(posts.length, 1);
  assert.match(prompts[0], /SIMULATED/);
  assert.equal(store.getState().hlCancelReceipt.outcome, "simulated");
});

test("the ARM gate is venue-neutral, but trading writes stay gated", async t => {
  const { store, api, posts } = await harness(t, resting);
  store.getState().setSignedTradingMode("off");
  assert.equal(store.getState().canTrade, false);

  // The challenge and the toggle belong to the process, so both must pass. Disarming
  // especially must always work, or the terminal could be stuck live.
  await api.api("/api/arm/challenge");
  await api.api("/api/arm", { method: "POST", body: { armed: false } });
  assert.equal(posts.length, 1, "disarming must not be blocked by the trading gate");

  await assert.rejects(api.api("/api/order", { method: "POST", body: {} }), /disabled by the backend gate/);
  assert.equal(posts.length, 1, "trading writes stay gated while the gate is off");

  store.getState().setSignedTradingMode("mainnet");
  await api.api("/api/arm", { method: "POST", body: { armed: true, challenge: "fixture" } });
  assert.equal(posts.length, 2, "arming must be reachable from the Hyperliquid venue");
  await api.api("/api/order", { method: "POST", body: { probe: true } });
  assert.equal(posts.length, 3);
});

test("armToggle can always disarm, even when the venue can no longer trade", async t => {
  const { store, posts } = await harness(t, resting);
  const armPath = () => posts.filter(p => p.armed !== undefined);
  store.setState({ armed: true });
  store.getState().setSignedTradingMode("off");
  assert.equal(store.getState().canTrade, false);
  await store.getState().armToggle();
  assert.equal(armPath().length, 1, "a live terminal must be disarmable regardless of the gate");
  assert.equal(armPath()[0].armed, false);
  assert.equal(store.getState().armed, false);
});

test("the request layer refuses Hyperliquid writes while the gate is off", async t => {
  const { store, api, reads } = await harness(t, resting);
  store.getState().setSignedTradingMode("off");
  await assert.rejects(api.api("/api/order", { method: "POST", body: {} }), /disabled by the backend gate/);
  await assert.rejects(api.api("/api/cancel", { method: "POST", body: {} }), /disabled by the backend gate/);
  assert.equal(reads.length, 0, "a blocked write must not even open a session");
  api.setSignedTrading("mainnet");
  assert.equal(store.getState().canTrade, false, "the store flag alone is not enough; both must move together");
  store.getState().setSignedTradingMode("mainnet");
  assert.equal(api.apiUrl("/api/order"), "/api/order?exchange=hyperliquid");
});

test("Stop and take-profit orders default to a market trigger so protection cannot rest unfilled", async t => {
  const { store, render, posts, prompts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, otype: "stp" });
  assert.match(render(), /id="hl-stop-limit"/, "the ticket offers stop-limit as an explicit opt-in");
  assert.doesNotMatch(render(), /id="hl-stop-limit"[^>]*checked=""/, "market trigger is the default");
  await store.getState().submitHyperliquidOrder("sell", { size: 2, stopPrice: 0.55 });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].orderType, "stp");
  assert.equal(posts[0].triggerMarket, true, "a stop must trigger at market, not rest as a limit");
  assert.equal(posts[0].limitPrice, undefined, "a market trigger carries no limit price");
  assert.equal(posts[0].reduceOnly, true);
  store.setState({ otype: "take_profit" });
  await store.getState().submitHyperliquidOrder("sell", { size: 2, stopPrice: 0.75 });
  assert.equal(posts[1].triggerMarket, true, "take profit defaults the same way");
  assert.equal(prompts.length, 0, "the safe default needs no extra confirmation");
});

test("Stop-limit is still available but demands a price and a confirmation naming the non-fill risk", async t => {
  const { store, posts, prompts, toasts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, otype: "stp" });
  await store.getState().submitHyperliquidOrder("sell", { size: 2, stopPrice: 0.55, triggerMarket: false });
  assert.equal(posts.length, 0, "a stop-limit without its own limit price must not submit");
  assert.ok(toasts.some(([, message]) => /stop-limit price/i.test(message)));
  await store.getState().submitHyperliquidOrder("sell", { size: 2, stopPrice: 0.55, triggerMarket: false, limitPrice: 0.54 });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].triggerMarket, false);
  assert.equal(posts[0].limitPrice, 0.54);
  assert.equal(prompts.length, 1, "the unsafe variant is confirmed explicitly");
  assert.match(prompts[0], /may remain unfilled/i);
});

test("Market orders sized in contracts still carry a USD ceiling and show it before signing", async t => {
  const { store, posts, prompts } = await harness(t, resting);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, otype: "mkt" });
  await store.getState().submitHyperliquidOrder("buy", { size: 20, slippagePercent: 0.5, reduceOnly: false, maxNotional: "12.06" });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].orderType, "mkt");
  assert.ok(Number(posts[0].maxNotional) > 0, "a contract-sized market order is still bounded in dollars");
  assert.match(prompts.at(-1), /20 contracts/, "the contract count stays visible");
  assert.match(prompts.at(-1), /\$12\.06/, "the dollar ceiling is shown alongside it");
});

test("Every open position gets its full book, so PnL is what a market close really returns", async t => {
  const book = { bids: [[0.6, 1], [0.59, 1000]], asks: [[0.61, 1000]] };
  const { store, reads } = await harness(t, resting, url => url.includes("/api/orderbook")
    ? { state: "current", exchange: "hyperliquid", orderBook: book, time: Date.now() }
    : { state: "current", orders: [], positions: [], items: [] });
  store.setState({ readOnly: true, exchange: "hyperliquid", symbol: SYMBOL, books: {},
    tickers: { HL_CHIP: { symbol: "HL_CHIP", bid: 0.6, ask: 0.61 } },
    instruments: [{ symbol: SYMBOL, contractValueTradePrecision: 2 }, { symbol: "HL_CHIP", contractValueTradePrecision: 0 }],
    positions: [
      { symbol: SYMBOL, side: "long", size: 1, sizeExact: "1", price: 0.5 },
      { symbol: "HL_CHIP", side: "long", size: 1598, sizeExact: "1598", price: 0.5 },
      { symbol: "HL_CHIP", side: "long", size: 1598, sizeExact: "1598", price: 0.5 },
      { error: "positions unavailable" },
    ] });
  reads.length = 0;
  await store.getState().refreshPositionBooks();
  assert.deepEqual(reads.map(url => url.match(/symbol=([A-Z_]+)/)[1]), ["HL_CHIP", SYMBOL],
    "largest position first, the selected symbol included, duplicates collapsed");
  assert.ok(store.getState().books.HL_CHIP.at > 0);

  // 1598 CHIP walks past the 1-contract best bid; the net shows it and pays the fee.
  const chip = store.getState().positions[1];
  const net = store.getState().computeUpnl(chip);
  const expected = 1 * (0.6 - 0.5) + 1597 * (0.59 - 0.5) - 0.00045 * (0.6 + 1597 * 0.59) - 0.00045 * 1598 * 0.5;
  assert.ok(Math.abs(net - expected) < 1e-9, `${net} != ${expected}`);
  assert.ok(net < store.getState().computeUpnl(chip, { mode: "gross" }), "net is below the best-bid value");

  // A failing read keeps the last book; a stale book falls back to best bid less the fee.
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("book unavailable"); };
  await store.getState().refreshPositionBooks();
  globalThis.fetch = originalFetch;
  assert.ok(store.getState().books.HL_CHIP, "a failed read does not erase the last book");
  store.setState({ books: { HL_CHIP: { ...book, at: Date.now() - 60000 } } });
  const fallback = store.getState().computeUpnl(chip);
  assert.ok(Math.abs(fallback - (1598 * 0.1 - 0.00045 * 1598 * (0.6 + 0.5))) < 1e-9, "stale book: best bid less both fees");

  // Kraken now pays for books too: its tickers carry only the best price.
  store.setState({ readOnly: false, exchange: "kraken", positions: [{ symbol: "PF_APTUSD", side: "long", size: 3, price: 1 }] });
  reads.length = 0;
  await store.getState().refreshPositionBooks();
  assert.deepEqual(reads.map(url => url.match(/symbol=([A-Z_]+)/)[1]), ["PF_APTUSD"]);
  assert.deepEqual(Object.keys(store.getState().books), ["PF_APTUSD"], "books for closed positions are dropped");
});

const gridOk = () => ({
  exchange: "hyperliquid", type: "order", live: true, outcome: "confirmed",
  action: { type: "order", orders: [] }, rows: [{ state: "resting" }, { state: "resting" }, { state: "resting" }],
  results: [{ outcome: "confirmed", simulated: false, batch: true,
              responses: [{ state: "resting" }, { state: "resting" }, { state: "resting" }] }],
});
const gridPreview = (hash = "hash-1", rungs = 3, armed = true) =>
  ({ armed, plan: { previewHash: hash, orders: Array.from({ length: rungs }, () => ({})) } });

test("A Hyperliquid grid carries one client id per rung and is recorded before it is sent", async t => {
  let receiptAtSend = null;
  const saved = new Map();
  const { store, posts } = await harness(t, n => {
    // Read the durable receipt at the moment the request is in flight.
    receiptAtSend = saved.get("seen") ?? null;
    return gridOk(n);
  });
  // Mirror localStorage writes so the assertion above can observe ordering.
  const realSet = globalThis.localStorage.setItem;
  globalThis.localStorage.setItem = (k, v) => { if (k.startsWith("kt.hyperliquid.receipt")) saved.set("seen", v); return realSet(k, v); };
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, symbol: SYMBOL });

  await store.getState().submitGrid({ symbol: SYMBOL, side: "buy" }, gridPreview());
  assert.equal(posts.length, 1);
  const body = posts[0];
  assert.equal(body.cloids.length, 3, "one immutable id per rung");
  assert.ok(body.cloids.every(id => /^0x[0-9a-f]{32}$/.test(id)));
  assert.equal(new Set(body.cloids).size, 3, "rung ids must be distinct");
  assert.equal(body.expectedArmed, true);
  assert.ok(body.previewHash && body.requestId);

  assert.ok(receiptAtSend, "the batch receipt must exist before the request leaves");
  const pending = JSON.parse(receiptAtSend);
  assert.equal(pending.batch, true);
  assert.deepEqual(pending.cloids, body.cloids);
  assert.equal(pending.outcome, "pending");
  assert.equal(pending.requestId, body.requestId);
  assert.equal(store.getState().hlReceipt.outcome, "confirmed", "the response resolves the receipt");
});

test("One unresolved Hyperliquid batch blocks the next grid, and a replay keeps its identity", async t => {
  const { store, posts, toasts } = await harness(t, gridOk);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, symbol: SYMBOL,
    hlReceipt: { version: 1, requestId: "stuck", batch: true, outcome: "unknown", uncertain: true,
                 cloids: ["0x" + "a".repeat(32), "0x" + "b".repeat(32)], body: {} } });
  await store.getState().submitGrid({ symbol: SYMBOL, side: "buy" }, gridPreview());
  assert.equal(posts.length, 0, "an unresolved batch must not be followed by another");
  assert.ok(toasts.some(([kind, m]) => kind === "err" && /unfinished/i.test(m)));

  // Cleared receipt: the grid goes, and checking the same submission reuses the ids.
  store.setState({ hlReceipt: null });
  await store.getState().submitGrid({ symbol: SYMBOL, side: "buy" }, gridPreview("hash-2"));
  assert.equal(posts.length, 1);
  const first = posts[0];
  store.setState({ ticketBusy: false });
  await store.getState().submitGrid(first, { armed: true, plan: { previewHash: "hash-2" } });
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1].cloids, first.cloids, "a replay must not mint new identities");
  assert.equal(posts[1].requestId, first.requestId);
});

test("A grid outside the venue's rung limits is refused before any identity is minted", async t => {
  const { store, posts, toasts } = await harness(t, gridOk);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ armed: true, symbol: SYMBOL });
  for (const rungs of [1, 21]) {
    store.setState({ ticketBusy: false, gridRequest: null });
    await store.getState().submitGrid({ symbol: SYMBOL, side: "buy" }, gridPreview("h" + rungs, rungs));
    assert.equal(posts.length, 0);
  }
  assert.ok(toasts.some(([kind, m]) => kind === "err" && /2 to 20/.test(m)));
  assert.equal(store.getState().hlReceipt, null, "no receipt for a grid that was never sent");
});

test("Chase renders without a price field and sends explicit Hyperliquid values, never the Kraken DOM", async t => {
  const replies = [
    { exchange: "hyperliquid", type: "chase", outcome: "simulated", simulated: true,
      action: { type: "order", orders: [{ a: 1, b: false, p: "0.61", s: "20", r: true, t: { limit: { tif: "Alo" } } }] } },
    { exchange: "hyperliquid", type: "chase", outcome: "confirmed", live: true,
      chase: { id: "c1", exchange: "hyperliquid", symbol: SYMBOL, side: "buy", size: 20, filled: 0, status: "running", pegs: 1 } },
    { ok: true },
  ];
  const { store, render, posts, postPaths, prompts, toasts } = await harness(t, n => replies[n - 1]);
  store.setState({ otype: "chase" });
  const markup = render();
  assert.match(markup, /CHASE BUY/);
  assert.match(markup, /CHASE SELL/);
  assert.doesNotMatch(markup, /id="hl-limit"/, "a Chase prices itself from the book");
  assert.match(markup, /re-pegs as the book moves/);
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ canTrade: true });

  await store.getState().submitHyperliquidChase("sell", { size: 20, reduceOnly: true });
  assert.deepEqual({ ...posts[0], requestId: undefined },
    { symbol: SYMBOL, side: "sell", size: 20, reduceOnly: true, expectedArmed: false, requestId: undefined });
  assert.match(postPaths[0], /^\/api\/chase\?exchange=hyperliquid/);
  assert.equal(prompts.length, 0, "a DISARMED simulation needs no live confirmation");
  assert.match(toasts.at(-1)[1], /simulated \(DISARMED\).*0\.61.*Nothing was sent/);

  store.setState({ armed: true });
  await store.getState().submitHyperliquidChase("buy", { size: 20, reduceOnly: false });
  assert.match(prompts[0], /LIVE CHASE BUY 20 HL_APT/);
  assert.match(prompts[0], /unfilled rest is cancelled/, "an entry never goes to market on timeout");
  assert.equal(posts[1].expectedArmed, true);
  assert.equal(store.getState().chases.c1.status, "running");
  assert.match(render(), /Chase buy HL_APT: running/);
  assert.match(render(), />Stop</);

  await store.getState().abortChase("c1");
  assert.match(postPaths[2], /^\/api\/chase\/abort/);
  assert.deepEqual({ ...posts[2], requestId: undefined }, { chaseId: "c1", requestId: undefined });

  globalThis.confirm = () => false;
  await store.getState().submitHyperliquidChase("buy", { size: 20, reduceOnly: false });
  assert.equal(posts.length, 3, "a declined live Chase sends nothing");

  // An order found resting after a restart blocks new orders, so it must be visible and clearable.
  store.getState().onChaseEvent({ id: "orphan-0x6368", exchange: "hyperliquid", symbol: SYMBOL, side: "buy",
    size: 20, filled: 0, status: "orphaned", unknownReason: "exchange order exists without a live Chase worker" });
  const orphaned = render();
  assert.match(orphaned, /orphaned/);
  assert.match(orphaned, /Mark checked/);
});

test("Stopping a Chase works even with the signed-trading gate off", async t => {
  const { store, posts, postPaths } = await harness(t, () => ({ ok: true }));
  store.getState().setSignedTradingMode("off");
  await store.getState().abortChase("c9");
  assert.match(postPaths[0], /^\/api\/chase\/abort/);
  assert.equal(posts[0].chaseId, "c9");
});

test("Soft close on Hyperliquid starts one confirmed reduce-only Chase per position", async t => {
  const replies = [];
  const { store, posts, postPaths, prompts, renderOrders } = await harness(t, n => replies[n - 1] || {
    exchange: "hyperliquid", type: "chase", outcome: "simulated", simulated: true,
    action: { type: "order", orders: [{ s: "1", p: "1", t: { limit: { tif: "Alo" } } }] } });
  store.getState().setSignedTradingMode("mainnet");
  store.setState({ canTrade: true, readOnly: true, exchange: "hyperliquid", tab: "positions",
    instruments: [{ symbol: "HL_BTC", contractValueTradePrecision: 5 }, { symbol: SYMBOL, contractValueTradePrecision: 2 }],
    positions: [{ symbol: "HL_BTC", side: "long", size: 0.01, sizeExact: "0.01", price: 60000 },
                { symbol: SYMBOL, side: "short", size: 20, sizeExact: "20", price: 0.6 }],
    dataStatus: { positions: { state: "current" }, orders: { state: "current" } } });
  const markup = renderOrders();
  assert.doesNotMatch(markup.match(/<button[^>]*aria-label="Soft close HL_BTC[^>]*>/)[0], /disabled/, "the row button is live on Hyperliquid");

  await store.getState().softCloseHyperliquid("HL_BTC");
  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /SIMULATED SOFT CLOSE 1 position: sell 0.01 HL_BTC/);
  assert.deepEqual({ ...posts[0], requestId: undefined },
    { symbol: "HL_BTC", side: "sell", size: 0.01, reduceOnly: true, expectedArmed: false, requestId: undefined });
  assert.match(postPaths[0], /^\/api\/chase/);

  await store.getState().softCloseHyperliquid();
  assert.equal(prompts.length, 2, "one confirmation for the whole batch");
  assert.match(prompts[1], /SOFT CLOSE 2 positions: sell 0.01 HL_BTC, buy 20 HL_APT/);
  assert.deepEqual(posts.slice(1).map(p => [p.symbol, p.side, p.reduceOnly]), [["HL_BTC", "sell", true], [SYMBOL, "buy", true]]);

  globalThis.confirm = () => false;
  await store.getState().softCloseHyperliquid();
  assert.equal(posts.length, 3, "declined: nothing sent");
  store.setState({ dataStatus: { positions: { state: "unavailable" } } });
  globalThis.confirm = () => true;
  await store.getState().softCloseHyperliquid();
  assert.equal(posts.length, 3, "stale positions: nothing sent");
});

test("An exchange switch keeps the ARM state and says so before switching", async t => {
  const { store, posts, postPaths, prompts, toasts } = await harness(t,
    () => ({ exchange: "kraken", exchangeEpoch: 2, exchangeRouting: 1, armed: true }));
  let reloaded = 0;
  globalThis.location = { reload() { reloaded++; } };
  store.setState({ exchangeRouting: true, exchangeBusy: false, armed: true, ticketBusy: false });
  await store.getState().switchExchange("kraken");
  assert.match(prompts[0], /stays ARMED: orders on Kraken Futures will be LIVE/);
  assert.match(postPaths[0], /^\/api\/exchange/);
  assert.equal(posts[0].exchange, "kraken");
  assert.equal(reloaded, 1, "an armed switch is accepted, not treated as unconfirmed");
  assert.equal(globalThis.localStorage.getItem("kt.exchange"), "kraken");
  assert.equal(toasts.filter(([kind]) => kind === "err").length, 0);
});
