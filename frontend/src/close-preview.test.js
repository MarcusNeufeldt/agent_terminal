import test from "node:test";
import assert from "node:assert/strict";
import { closePreview, sortedLevels, walkBook, TAKER_FEE, HL_CLOSE_SLIPPAGE } from "./close-preview.js";

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("Kraken's worst-first bids are walked best-first", () => {
  // As /api/orderbook returns them on Kraken: ascending, a stink bid first.
  const levels = sortedLevels([[150, 0.76], [341.71, 20], [342.25, 10], [342.0, 30]], "bids");
  assert.deepEqual(levels.map(l => l.px), [342.25, 342.0, 341.71, 150]);
  const walk = walkBook(levels, 51.8);
  assert.equal(walk.levelsUsed, 3);
  close(walk.avgPrice, (10 * 342.25 + 30 * 342.0 + 11.8 * 341.71) / 51.8);
  assert.equal(walk.worstPrice, 341.71, "never reaches the 150 stink bid");
});

test("the BCH close: display at best bid vs what the book returns", () => {
  const book = { bids: [[342.25, 10], [342.0, 30], [341.71, 40]], asks: [[342.5, 50]] };
  const p = closePreview({ position: { side: "long", size: 51.8, price: 341.18 }, book, feeRate: TAKER_FEE.kraken });
  close(p.pnlAtBest, 51.8 * (342.25 - 341.18));
  close(p.bookWalkCost, 51.8 * (342.25 - p.avgPrice));
  close(p.exitFee, 0.0005 * 51.8 * p.avgPrice);
  close(p.net, p.pnlAtBest - p.bookWalkCost - p.exitFee, 1e-6);
  assert.ok(p.net < p.pnlAtBest, "the real close is worse than the display");
  assert.equal(p.unfilled, 0);
});

test("a short closes into the asks", () => {
  const book = { bids: [[99, 5]], asks: [[101, 1], [100, 1], [102, 5]] };
  const p = closePreview({ position: { side: "short", size: 3, price: 110 }, book, feeRate: 0 });
  assert.equal(p.best, 100);
  close(p.avgPrice, (100 + 101 + 102) / 3);
  close(p.pnlWalked, 3 * (110 - 101));
  close(p.bookWalkCost, 3 * (101 - 100));
});

test("thin visible depth is reported as beyond the snapshot, not as a fill", () => {
  const p = closePreview({ position: { side: "long", size: 10, price: 100 }, book: { bids: [[101, 4]] }, feeRate: 0 });
  assert.equal(p.filled, 4);
  assert.equal(p.unfilled, 6);
  assert.equal(p.unfilledReason, "depth");
  close(p.pnlWalked, 4, 1e-9);
});

test("Hyperliquid's 0.5% IOC bound stops the walk and says so", () => {
  const book = { bids: [[100, 1], [99.6, 1], [99.4, 5]] };
  const p = closePreview({ position: { side: "long", size: 3, price: 90 }, book, feeRate: TAKER_FEE.hyperliquid,
    slippageBound: HL_CLOSE_SLIPPAGE });
  assert.equal(p.limit, 99.5);
  assert.equal(p.filled, 2);
  assert.equal(p.unfilledReason, "bound");
});

test("contract multiplier, funding, and unusable input", () => {
  const p = closePreview({ position: { side: "long", size: 2, price: 100 }, book: { bids: [[110, 5]] },
    contractSize: 10, feeRate: 0.001, funding: -1.5 });
  close(p.pnlWalked, 200);
  close(p.exitFee, 0.001 * 20 * 110);
  close(p.net, 200 - 2.2 - 1.5);
  assert.equal(closePreview({ position: { side: "long", size: 1, price: 1 }, book: { bids: [[1, 1]] }, inverse: true, feeRate: 0 }), null);
  assert.equal(closePreview({ position: { side: "long", size: 1, price: 1 }, book: { bids: [] }, feeRate: 0 }), null);
  assert.equal(closePreview({ position: { side: "flat", size: 1, price: 1 }, book: { bids: [[1, 1]] }, feeRate: 0 }), null);
  const empty = closePreview({ position: { side: "long", size: 1, price: 1 }, book: { bids: [[2, 1]] }, feeRate: 0, slippageBound: 0.005 });
  assert.equal(empty.filled, 1);
});

test("the modal shows screen value, book walk, fee and the real result, and never blocks the close", async t => {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  globalThis.localStorage ??= { getItem: () => null, setItem: () => {} };
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const { ClosePreviewBody, default: ClosePreviewModal } = await vite.ssrLoadModule("/src/components/ClosePreviewModal.jsx");
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const book = { bids: [[342.25, 10], [342.0, 30], [341.71, 40]], asks: [[342.5, 50]], serverTime: "2026-09-23T15:57:05.000Z" };
  const preview = closePreview({ position: { side: "long", size: 51.8, price: 341.18 }, book, feeRate: TAKER_FEE.kraken, funding: 0 });
  const html = renderToStaticMarkup(React.createElement(ClosePreviewBody, { preview, venue: "kraken", book,
    now: Date.parse("2026-09-23T15:57:06.000Z") }));
  assert.match(html, /Screen value<span class="cp-detail">all at best bid 342.25<\/span>/);
  assert.match(html, /Walking the book<span class="cp-detail">→ avg 341\.982\d* over 3 levels, worst 341.71<\/span>/);
  assert.match(html, /Exit taker fee<span class="cp-detail">0.050%<\/span>/);
  assert.match(html, /Screen shows <b>\+\$\d+\.\d\d<\/b>.*in costs/, "the hero names the gap");
  assert.match(html, /Trade result if closed now/);
  assert.match(html, /Book 1.0s ago/);
  assert.match(renderToStaticMarkup(React.createElement(ClosePreviewBody, { preview: null, venue: "hyperliquid" })),
    /Book unavailable — no estimate\. The close still works\./);
  const hl = closePreview({ position: { side: "long", size: 10, price: 100 }, book: { bids: [[101, 4]] }, feeRate: TAKER_FEE.hyperliquid });
  assert.match(renderToStaticMarkup(React.createElement(ClosePreviewBody, { preview: hl, venue: "hyperliquid", book: {} })),
    /Hyperliquid shows 20 levels/);

  // Before any book arrives the close button is still live.
  store.setState({ exchange: "kraken", ticketBusy: false, instruments: [{ symbol: "PF_BCHUSD", contractSize: 1 }],
    positions: [{ symbol: "PF_BCHUSD", side: "long", size: 51.8, price: 341.18 }] });
  Object.assign(store.getInitialState(), store.getState());
  const modal = renderToStaticMarkup(React.createElement(ClosePreviewModal, { symbol: "PF_BCHUSD", onClose() {} }));
  assert.match(modal, /Market close PF_BCHUSD/);
  assert.match(modal, /<button type="button" class="cp-confirm sell">Sell to close at market<\/button>/,
    "closing a long sells, in red, and is live before any book arrives");
  store.setState({ positions: [{ symbol: "PF_BCHUSD", side: "short", size: 5, price: 341 }] });
  Object.assign(store.getInitialState(), store.getState());
  assert.match(renderToStaticMarkup(React.createElement(ClosePreviewModal, { symbol: "PF_BCHUSD", onClose() {} })),
    /class="cp-confirm buy">Buy to close at market/);
});

test("size beyond the visible book is valued at the worst visible level, never left out", () => {
  const p = closePreview({ position: { side: "long", size: 10, price: 100 }, book: { bids: [[102, 4], [101, 2]] }, feeRate: 0.001 });
  assert.equal(p.unfilled, 4);
  const walked = 4 * 2 + 2 * 1 - 0.001 * (4 * 102 + 2 * 101);
  close(p.net, walked);
  close(p.netFull, walked + 4 * (101 - 100) - 0.001 * 4 * 101);
});

test("discipline rules ratchet on net PnL under a fresh storage key", async t => {
  const { createServer } = await import("vite");
  const saved = new Map();
  const original = globalThis.localStorage;
  globalThis.localStorage = { getItem: k => saved.get(k) ?? null, setItem: (k, v) => saved.set(k, v) };
  t.after(() => { globalThis.localStorage = original; });
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const { default: store } = await vite.ssrLoadModule("/src/store.js");
  const position = { symbol: "PF_BCHUSD", side: "long", size: 51.8, price: 341.18, unrealizedFunding: 0 };
  store.setState({ exchange: "kraken", instruments: [{ symbol: "PF_BCHUSD", contractSize: 1 }], positions: [position],
    dataStatus: { positions: { state: "current" } }, rulePeaks: {}, tickers: {},
    books: { PF_BCHUSD: { bids: [[342.25, 10], [342.0, 30], [341.71, 40]], asks: [[342.5, 50]], at: Date.now() } } });
  const netValue = store.getState().computeUpnl(position);
  const expected = closePreview({ position, book: store.getState().books.PF_BCHUSD, feeRate: TAKER_FEE.kraken,
    entryFeeRate: TAKER_FEE.kraken }).netFull;
  close(netValue, expected);
  store.getState().updateRulePeaks();
  assert.equal(saved.has("kt.rulePeaks"), false, "gross-era peaks are not reused");
  const peaks = JSON.parse(saved.get("kt.rulePeaks.trade"));
  close(Object.values(peaks)[0], netValue);
});

test("the entry fee makes net the whole trade's result, and defaults to off", () => {
  const book = { bids: [[110, 5]] };
  const position = { side: "long", size: 2, price: 100 };
  const exitOnly = closePreview({ position, book, feeRate: 0.0005 });
  const trade = closePreview({ position, book, feeRate: 0.0005, entryFeeRate: 0.0005 });
  close(exitOnly.entryFee, 0);
  close(trade.entryFee, 0.0005 * 2 * 100);
  close(trade.net, exitOnly.net - 0.1);
  close(trade.netFull, exitOnly.netFull - 0.1);
  close(trade.net, 2 * 10 - 0.0005 * 2 * 110 - 0.0005 * 2 * 100);
});
