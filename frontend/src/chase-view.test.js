import test from "node:test";
import assert from "node:assert/strict";
import { chaseCards, chaseOverlays, chaseProgress, isChaseOrder } from "./chase-view.js";

const now = 1_000_000;
const chases = {
  a: { id: "a", exchange: "hyperliquid", symbol: "HL_TAO", side: "buy", size: 1, filled: 0.25, status: "running",
    activePrice: 295.3, activeSize: 0.75, pegs: 3, started: 900, spec: { timeoutSec: 300 } },
  b: { id: "b", exchange: "hyperliquid", symbol: "HL_TAO", side: "sell", size: 1, filled: 1, status: "filled", updated: now - 5000, started: 980 },
  c: { id: "c", exchange: "hyperliquid", symbol: "HL_NEAR", side: "sell", size: 1, filled: 1, status: "filled", updated: now - 120000, started: 850 },
  // Reloaded: the browser just heard of it, but it started an hour ago.
  old: { id: "old", exchange: "hyperliquid", symbol: "HL_NEAR", side: "buy", size: 1, filled: 1, status: "filled", updated: now - 1000, started: 1000 - 3600 },
  k: { id: "k", symbol: "PF_XBTUSD", side: "buy", size: 1, filled: 0, status: "unknown", started: 950 },
};

test("status cards list this venue's live Chases, then the latest recently finished one", () => {
  assert.deepEqual(chaseCards(chases, "hyperliquid", now).map(c => c.id), ["a", "b"]);
  assert.deepEqual(chaseCards(chases, "kraken", now).map(c => c.id), ["k"]);
  assert.deepEqual(chaseCards({}, "kraken", now), []);
  assert.deepEqual(chaseCards(chases, "hyperliquid", now, { showFinished: false }).map(c => c.id), ["a"]);
});

test("progress shows filled share and the time left before the timeout", () => {
  const p = chaseProgress(chases.a, 1000);
  assert.equal(p.filledPct, 25);
  assert.equal(p.remaining, 0.75);
  assert.equal(p.elapsed, 100);
  assert.equal(p.left, 200);
  assert.equal(chaseProgress({ size: 0 }).filledPct, 0);
});

test("the chart draws only running Chases on this symbol and venue at their peg", () => {
  const lines = chaseOverlays(chases, "HL_TAO", "hyperliquid");
  assert.equal(lines.length, 1);
  assert.equal(lines[0].price, 295.3);
  assert.match(lines[0].title, /CHASE buy 0.75/);
  assert.deepEqual(chaseOverlays(chases, "HL_TAO", "kraken"), []);
  assert.deepEqual(chaseOverlays({ x: { ...chases.a, activePrice: null } }, "HL_TAO", "hyperliquid"), []);
});

test("Chase orders are recognised on both venues by client id", () => {
  assert.equal(isChaseOrder({ cliOrdId: "ch-726da97f-4-de2425" }), true);
  assert.equal(isChaseOrder({ cliOrdId: "0x636861735aceea355e797a9cd8b8597d" }), true);
  assert.equal(isChaseOrder({ cliOrdId: "kt-full-tp-PF_HYPEUSD-1" }), false);
  assert.equal(isChaseOrder({}), false);
});
