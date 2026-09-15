import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

globalThis.localStorage = { getItem: () => null, setItem: () => {} };

test("grid ticket renders, submits once, and replays ambiguous requests without a new ID", async t => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const { default: GridTicket, quickGridPreset } = await vite.ssrLoadModule("/src/components/GridTicket.jsx");
  const position = { size: 691.5 };
  const quote = { bid: 100, ask: 101 };
  assert.deepEqual(quickGridPreset("buy", quote, position, "current"), {
    side: "buy", start: 100, end: 95, size: 691.5, orders: 20, orderType: "post", reduceOnly: false,
  });
  const sellPreset = quickGridPreset("sell", quote, position, "current");
  assert.equal(sellPreset.start, 101);
  assert.equal(sellPreset.end, 101 * 1.05);
  assert.equal(sellPreset.size, 691.5);
  assert.equal(sellPreset.orders, 20);
  for (const [side, ticker, pos, state] of [
    ["buy", quote, position, "stale"], ["buy", quote, null, "current"],
    ["buy", {}, position, "current"], ["sell", { ask: Infinity }, position, "current"],
    ["sell", quote, { size: 0 }, "current"], ["other", quote, position, "current"],
  ]) assert.equal(quickGridPreset(side, ticker, pos, state), null);
  const action = { symbol: "PF_UNIUSD", side: "buy", startPrice: 6.8, endPrice: 6.6,
    size: 691.5, orders: 10, orderType: "post", reduceOnly: false };
  const preview = { armed: false, ready: true, plan: { symbol: action.symbol, previewHash: "fixed-plan", orders: [] } };
  store.setState({ symbol: action.symbol, armed: false, ticketBusy: false, bulkBusy: false,
    positions: [{ symbol: action.symbol, size: 691.5, side: "long" }],
    gridSeed: { symbol: action.symbol, size: 691.5, side: "buy" },
    dataStatus: { positions: { state: "current" } },
    toast: () => {}, refreshTables: async () => {}, refreshAccount: async () => {},
  });
  const html = renderToStaticMarkup(createElement(GridTicket, { symbol: action.symbol }));
  for (const text of ["Start price", "End price", "Post-only", "Reduce-only exit", "Simulate 10 buy orders", "Quick buy", "Quick sell", "Presets fill the ticket only"]) {
    assert.ok(html.includes(text), text);
  }

  let resolveResponse;
  const gate = new Promise(resolve => { resolveResponse = resolve; });
  const calls = [];
  globalThis.fetch = async (path, options = {}) => {
    if (path === "/api/session") return new Response(JSON.stringify({ token: "fake-token" }));
    assert.equal(path, "/api/grid");
    calls.push(JSON.parse(options.body));
    await gate;
    return new Response(JSON.stringify({ results: [{ outcome: "simulated", simulated: true, orders: [] }] }));
  };
  const first = store.getState().submitGrid(action, preview);
  await store.getState().submitGrid(action, preview);
  resolveResponse();
  await first;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].size, 691.5);
  assert.equal(store.getState().gridResult.outcome, "simulated");
  assert.equal(store.getState().ticketBusy, false);
  assert.equal(store.getState().gridPreview, null);

  store.setState({ gridRequest: null });
  globalThis.fetch = async (_path, options) => { calls.push(JSON.parse(options.body)); throw new Error("connection lost"); };
  await store.getState().submitGrid(action, preview);
  const ambiguous = store.getState().gridRequest;
  assert.equal(store.getState().gridResult.transportError, true);
  store.setState({ armed: true }); // replay must work even if ARM changed after the original submission
  globalThis.fetch = async (_path, options) => {
    calls.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ results: [{ outcome: "simulated", simulated: true }] }));
  };
  await store.getState().submitGrid(action, preview);
  assert.equal(calls.at(-1).requestId, ambiguous.requestId);
  assert.deepEqual(calls.at(-1), ambiguous);
  assert.equal(store.getState().ticketBusy, false);

  const before = calls.length;
  await store.getState().submitGrid(action, { ...preview, plan: { ...preview.plan, previewHash: "new-plan" } });
  assert.equal(calls.length, before, "new submission with stale ARM preview is blocked");
  store.getState().setGridPreview({ symbol: action.symbol, orders: [{ side: "buy", limitPrice: 6.6, size: 10 }] });
  assert.ok(store.getState().overlayPrices.includes(6.6));
  store.getState().setGridPreview(null);
  assert.ok(!store.getState().overlayPrices.includes(6.6));

  const seed = store.getState().gridSeed;
  store.setState({ rightCollapsed: true });
  store.getState().showRight("agent");
  assert.equal(store.getState().rightView, "agent");
  assert.equal(store.getState().rightCollapsed, false);
  assert.equal(store.getState().gridSeed, seed, "switching views preserves the ticket draft");
  store.setState({ selectSymbol: () => {} });
  store.getState().openGrid(action.symbol);
  assert.equal(store.getState().rightView, "ticket", "position Grid shortcut reveals the ticket");
  assert.equal(store.getState().otype, "grid");
});
