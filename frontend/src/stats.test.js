import test from "node:test";
import assert from "node:assert/strict";
import { computeStats, ledgerNet } from "./stats.js";

// Shapes match /api/stats rows projected from the Kraken account log.
const T0 = Date.UTC(2026, 5, 1) / 1000;
const rows = [
  { t: T0, info: "cross-exchange transfer", contract: null, pnl: null, funding: null, fee: null },
  { t: T0 + 1, info: "conversion", contract: null, pnl: null, funding: null, fee: null },
  // open: fee only
  { t: T0 + 10, info: "futures trade", contract: "pf_ethusd", pnl: 0, funding: 0, fee: 5, liqFee: 0 },
  // close in profit
  { t: T0 + 20, info: "futures trade", contract: "pf_ethusd", pnl: 30, funding: 1, fee: 5, liqFee: 0 },
  // liquidation: price loss, small fee, separate penalty
  { t: T0 + 30, info: "futures partial liquidation", contract: "pf_xbtusd", pnl: -100, funding: 0, fee: 0.5, liqFee: 60 },
  { t: T0 + 40, info: "funding rate change", contract: "pf_ethusd", pnl: null, funding: 2, fee: null },
  { t: T0 + 50, info: "interest payment", contract: null, pnl: null, funding: null, fee: 0.25 },
];

test("net is the wallet change: every cost is subtracted, transfers and conversions are not trading", () => {
  const s = computeStats(rows, "all");
  assert.equal(s.pricePnl, -70);
  assert.equal(s.fees, 10.5);
  assert.equal(s.liqPenalty, 60);
  assert.equal(s.funding, 3);
  assert.equal(s.interest, 0.25);
  assert.equal(s.net, -70 + 3 - 10.5 - 60 - 0.25);
  assert.equal(s.liqNet, -160.5, "a liquidation's all-in cost includes its penalty");
  assert.equal(s.curve.at(-1)[1], s.net, "the curve ends at the net");
  assert.equal(ledgerNet(rows[0]), 0);
  assert.equal(ledgerNet(rows[1]), 0);
});

test("closing-fill figures count only fills that realized PnL", () => {
  const s = computeStats(rows, "all");
  assert.deepEqual(s.closes, [30, -100], "the opening fill is not a close");
  assert.equal(s.wins.length, 1);
  assert.equal(s.pf, 0.3);
  assert.equal(s.best, 30);
  assert.equal(s.worst, -100);
});

test("per-symbol rows carry their own fees, penalties, funding and net", () => {
  const s = computeStats(rows, "all");
  const eth = s.perSym.find(r => r.symbol === "pf_ethusd");
  const xbt = s.perSym.find(r => r.symbol === "pf_xbtusd");
  assert.deepEqual({ n: eth.n, pnl: eth.pnl, fees: eth.fees, fund: eth.fund, net: eth.net },
    { n: 1, pnl: 30, fees: 10, fund: 3, net: 23 });
  assert.deepEqual({ liq: xbt.liq, net: xbt.net }, { liq: 60, net: -160.5 });
  assert.equal(s.perSym[0].symbol, "pf_ethusd", "sorted by net");
});

test("a window plus everything before it adds back to the all-time net", () => {
  const now = new Date((T0 + 35) * 1000);
  const all = computeStats(rows, "all", now).net;
  for (const tf of ["1d", "1w", "1mo"]) {
    const w = computeStats(rows, tf, now);
    assert.ok(Math.abs(w.net + w.preWindow - all) < 1e-9, tf);
  }
  assert.equal(computeStats(rows, "all").preWindow, null);
});

test("unusable input reads as empty, never as a crash", () => {
  const s = computeStats(null, "all");
  assert.equal(s.net, 0);
  assert.deepEqual(s.curve, []);
  assert.equal(s.pf, null);
  assert.equal(computeStats([{ t: T0, info: "futures trade", pnl: "x", fee: undefined }], "all").net, 0);
});
