import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

globalThis.localStorage = { getItem: () => null, setItem: () => {} };

test("display PnL uses the exit side of the book, never mark or account PnL, without changing risk state", async t => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const oldFetch = globalThis.fetch;
  let networkCalls = 0;
  globalThis.fetch = () => { networkCalls++; throw new Error("Display calculation must not make requests"); };
  t.after(() => { globalThis.fetch = oldFetch; });
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const { default: Sidebar } = await vite.ssrLoadModule("/src/components/Sidebar.jsx");
  const { default: BottomTabs } = await vite.ssrLoadModule("/src/components/BottomTabs.jsx");
  const symbol = "PF_TESTUSD";
  const position = Object.freeze({ symbol, side: "long", size: 2, price: 100,
    unrealizedPnl: 999, unrealizedFunding: 3, liqPriceEstimate: 50, atr14d: 2 });
  const account = Object.freeze({ balanceValue: 1000, availableMargin: 600, pnl: 555 });
  const ticker = Object.freeze({ symbol, last: 110, markPrice: 150 });
  const instrument = Object.freeze({ symbol, contractSize: 1, type: "flexible_futures" });
  store.setState({ positions: [position], tickers: { [symbol]: ticker }, instruments: [instrument],
    account, dataStatus: { positions: { state: "current" } }, pro: false, tab: "positions" });
  const render = Component => {
    // React SSR reads Zustand's initial snapshot. Seed this isolated test snapshot with the fixture.
    Object.assign(store.getInitialState(), store.getState());
    return renderToStaticMarkup(createElement(Component));
  };
  const pnl = p => store.getState().computeUpnl(p);
  const total = () => store.getState().totalUpnl();
  assert.equal(pnl(position), 20);
  assert.equal(pnl({ ...position, side: "short" }), -20);
  assert.equal(total(), 20);
  const sidebar = render(Sidebar);
  const table = render(BottomTabs);
  assert.ok(sidebar.includes("Unrealized PnL · exit"));
  assert.ok(sidebar.includes(">+$20.00<"));
  assert.ok(table.includes("Unrealized PnL · exit"));
  assert.ok(table.includes(">+20.00<"));
  assert.ok(table.includes("Kraken last: 110"));
  assert.ok(table.includes(">Exit</th>"), "quote column is labelled by the basis it shows");
  assert.ok(table.includes('title="Mark for risk: 150">110</td>'), "quote column must match the PnL price basis");
  assert.ok(table.includes("66.7% · 50.0×ATR"), "liquidation distance must still use mark, not last");
  store.setState({ pro: true, tickers: { [symbol]: { ...ticker, markPrice: 1000 } } });
  assert.equal(pnl(position), 20, "mark changes and Pro Mode must not alter book-based PnL");
  assert.equal(store.getState().proAdj(0), 4300);
  assert.equal(store.getState().proAdj(1000), 5300);
  const proSidebar = render(Sidebar);
  assert.ok(proSidebar.includes("$5,300"), "balance uses the shared display offset");
  assert.ok(proSidebar.includes("$4,900"), "available margin uses the same offset");
  assert.equal(store.getState().account, account);
  assert.equal(position.liqPriceEstimate, 50);
  store.setState({ instruments: [{ ...instrument, contractSize: 10 }] });
  assert.equal(pnl(position), 200, "respect contract multipliers");
  store.setState({ instruments: [{ ...instrument, type: "futures_inverse" }] });
  assert.ok(Math.abs(pnl(position) - 2 * (1 / 100 - 1 / 110)) < 1e-12);
  assert.ok(Math.abs(pnl({ ...position, side: "short" }) + 2 * (1 / 100 - 1 / 110)) < 1e-12);
  store.setState({ instruments: [instrument] });

  const otherSymbol = "PF_OTHERUSD";
  const otherPosition = { ...position, symbol: otherSymbol, side: "short" };
  store.setState({ symbol: "PF_UNSELECTEDUSD", pro: false,
    positions: [position, otherPosition], instruments: [instrument, { ...instrument, symbol: otherSymbol }] });
  let notifications = 0;
  const unsubscribe = store.subscribe(() => { notifications++; });
  store.getState().onTicker({ symbol, last: 112, markPrice: 151 });
  store.getState().onTicker({ symbol: otherSymbol, last: 90, markPrice: 80 });
  assert.equal(notifications, 2, "both unselected position tickers notify React subscribers");
  assert.equal(total(), 44);
  assert.ok(render(Sidebar).includes(">+$44.00<"));
  const updatedTable = render(BottomTabs);
  assert.ok(updatedTable.includes('title="Mark for risk: 151">112</td>'));
  assert.ok(updatedTable.includes('title="Mark for risk: 80">90</td>'));
  assert.ok(updatedTable.includes(">+24.00<"));
  assert.ok(updatedTable.includes(">+20.00<"));
  unsubscribe();
  store.setState({ positions: [position], instruments: [instrument] });

  // With no book and an unusable last, mark is the remaining valuation. It beats a
  // stale trade and matches what Hyperliquid's own interface shows, but it is never
  // the exchange's reported PnL.
  for (const last of [undefined, null, 0, -1, "NaN", Infinity]) {
    store.setState({ tickers: { [symbol]: { ...ticker, last } } });
    assert.equal(pnl(position), 100, `invalid last ${last} falls back to mark, not reported PnL`);
  }
  assert.ok(!render(BottomTabs).includes("+999"), "never substitute exchange-reported PnL");

  // Nothing usable at all must read as unavailable, never as a confident zero.
  for (const last of [undefined, null, 0, -1, "NaN", Infinity]) {
    store.setState({ tickers: { [symbol]: { symbol, last } } });
    assert.equal(pnl(position), null, `invalid last ${last} with no mark must not invent a price`);
    assert.equal(total(), null);
  }
  const unavailableSidebar = render(Sidebar);
  const unavailableTable = render(BottomTabs);
  assert.ok(unavailableSidebar.includes("Book PnL unavailable"));
  assert.ok(!unavailableSidebar.includes("+$555"), "never label account PnL as position PnL");
  assert.ok(unavailableTable.includes('title="Book PnL unavailable">–</td>'));
  assert.ok(!unavailableTable.includes("+999"), "never substitute exchange-reported PnL");

  // The live book wins over the tape. On thin Kraken pairs the last trade drifts
  // outside the bid/ask and invents PnL that could never be realised.
  store.setState({ positions: [position], instruments: [instrument],
    tickers: { [symbol]: { symbol, last: 130, bid: 109, ask: 111, markPrice: 150 } } });
  assert.equal(pnl(position), 18,
    "a long is valued at the bid it would sell into, not the stale last (130) or mark (150)");
  assert.equal(store.getState().computeUpnl({ ...position, side: "short" }), -22,
    "a short is valued at the ask it would buy back at");
  assert.equal(store.getState().computeUpnl(position, { mode: "mid" }), 20,
    "mid stays available for callers that explicitly want the untraded middle");
  const bookTable = render(BottomTabs);
  assert.ok(bookTable.includes(">109.00<") || bookTable.includes(">109<"),
    "quote column shows the exit price, not the stale last");
  assert.ok(bookTable.includes("stale, outside book"), "a last outside the book is flagged");
  assert.ok(!bookTable.includes("+60.00"), "the stale last must never reach displayed PnL");

  store.setState({ tickers: { [symbol]: ticker } });
  for (const p of [{ ...position, size: NaN }, { ...position, price: 0 }, { ...position, side: "unknown" },
    { ...position, symbol: "PF_UNKNOWNUSD" }, { error: "positions unavailable" }]) assert.equal(pnl(p), null);
  store.setState({ positions: [position, { ...position, symbol: "PF_UNKNOWNUSD" }] });
  assert.equal(total(), null, "an incomplete total must not silently omit a position");
  store.setState({ positions: [], dataStatus: { positions: { state: "unavailable" } } });
  assert.equal(total(), null, "unavailable positions are not a flat account");
  store.setState({ dataStatus: { positions: { state: "current" } } });
  assert.equal(total(), 0, "confirmed flat account has zero unrealized PnL");
  assert.ok(render(Sidebar).includes(">+$0.00<"));
  assert.equal(networkCalls, 0);
});
