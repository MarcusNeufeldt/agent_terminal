import test from "node:test";
import assert from "node:assert/strict";
import {
  RULES,
  accountEquity,
  evaluateCooldown,
  evaluateRules,
  evaluateSize,
  evaluateTakeProfit,
  formatCountdown,
  nextPeaks,
  peakKey,
  positionNotional,
  realizedEvents,
} from "./rules.js";

const EQUITY = 5000;

test("equity falls back through payload keys and rejects unusable values", () => {
  assert.equal(accountEquity({ portfolioValue: 5000, collateralValue: 9 }), 5000);
  assert.equal(accountEquity({ collateralValue: 4000 }), 4000);
  assert.equal(accountEquity({ balanceValue: "2500" }), 2500);
  for (const account of [null, undefined, {}, { portfolioValue: 0 },
    { portfolioValue: -1 }, { portfolioValue: "abc" }, { portfolioValue: Infinity }]) {
    assert.equal(accountEquity(account), null);
  }
});

test("notional follows contract type: flexible scales with price, inverse does not", () => {
  const flexible = { symbol: "PF_UNIUSD", contractSize: 1, type: "flexible_futures" };
  const inverse = { symbol: "PI_XBTUSD", contractSize: 1, type: "futures_inverse" };
  assert.equal(positionNotional({ side: "long", size: 100, price: 6 }, flexible, 6.5), 650);
  assert.equal(positionNotional({ side: "long", size: 500, price: 60000 }, inverse, 61000), 500);
  // A flexible contract without a usable last price cannot be valued.
  assert.equal(positionNotional({ side: "long", size: 100 }, flexible, null), null);
  assert.equal(positionNotional({ side: "long", size: 100 }, flexible, 0), null);
  assert.equal(positionNotional({ side: "long", size: 0 }, flexible, 6), null);
  assert.equal(positionNotional({ error: "stale", size: 1 }, flexible, 6), null);
  assert.equal(positionNotional({ size: 1 }, null, 6), null);
});

test("peak key changes with entry price so scaling in restarts the trail", () => {
  assert.equal(peakKey({ symbol: "PF_UNIUSD", side: "long", price: 6 }), "PF_UNIUSD|long|6");
  assert.notEqual(
    peakKey({ symbol: "PF_UNIUSD", side: "long", price: 6 }),
    peakKey({ symbol: "PF_UNIUSD", side: "long", price: 6.2 }),
  );
  assert.equal(peakKey({ symbol: "PF_UNIUSD", side: "long", price: 6, error: "x" }), null);
  assert.equal(peakKey({ symbol: "PF_UNIUSD", side: "sideways", price: 6 }), null);
  assert.equal(peakKey({}), null);
});

test("peaks ratchet up, never down, and drop keys for closed positions", () => {
  let peaks = nextPeaks({}, [{ key: "a", upnl: 50 }]);
  assert.deepEqual(peaks, { a: 50 });
  peaks = nextPeaks(peaks, [{ key: "a", upnl: 120 }]);
  assert.deepEqual(peaks, { a: 120 });
  peaks = nextPeaks(peaks, [{ key: "a", upnl: 30 }]);
  assert.deepEqual(peaks, { a: 120 }, "a pullback must not lower the peak");
  // A position whose uPnL is briefly unreadable keeps its peak.
  peaks = nextPeaks(peaks, [{ key: "a", upnl: null }]);
  assert.deepEqual(peaks, { a: 120 });
  // Closing the position removes it entirely.
  assert.deepEqual(nextPeaks(peaks, [{ key: "b", upnl: 5 }]), { b: 5 });
  // Losing positions never record a negative peak.
  assert.deepEqual(nextPeaks({}, [{ key: "c", upnl: -80 }]), { c: 0 });
});

test("take profit fires at 3% of equity and reports progress below it", () => {
  const below = evaluateTakeProfit({ upnl: 75, equity: EQUITY });
  assert.equal(below.target, 150);
  assert.equal(below.state, "hold");
  assert.equal(below.progress, 0.5);
  const at = evaluateTakeProfit({ upnl: 150, equity: EQUITY });
  assert.equal(at.state, "take");
  assert.equal(evaluateTakeProfit({ upnl: 400, equity: EQUITY }).state, "take");
});

test("trail fires only after the target was cleared and 30% handed back", () => {
  // Peaked at 300 (target 150), now 220: gave back 27%, not yet.
  const holding = evaluateTakeProfit({ upnl: 220, equity: EQUITY, peak: 300 });
  assert.equal(holding.armed, true);
  assert.equal(holding.state, "take", "still above target, so take is the signal");
  // Below target but 30% off the peak.
  const given = evaluateTakeProfit({ upnl: 140, equity: EQUITY, peak: 300 });
  assert.equal(given.state, "trail");
  assert.ok(Math.abs(given.giveback - 0.5333333) < 1e-6);
  // Never armed: peak never reached the target, so no trail signal.
  const unarmed = evaluateTakeProfit({ upnl: 20, equity: EQUITY, peak: 60 });
  assert.equal(unarmed.armed, false);
  assert.equal(unarmed.state, "hold");
  // A peak below current uPnL is corrected upward rather than trusted.
  assert.equal(evaluateTakeProfit({ upnl: 200, equity: EQUITY, peak: 10 }).peak, 200);
});

test("take profit refuses to evaluate without usable inputs", () => {
  for (const input of [{ upnl: null, equity: EQUITY }, { upnl: 10, equity: 0 },
    { upnl: 10, equity: null }, { upnl: NaN, equity: EQUITY }]) {
    assert.equal(evaluateTakeProfit(input), null);
  }
});

test("size breaches only above 3x equity notional", () => {
  assert.equal(evaluateSize({ notional: 15000, equity: EQUITY }).breach, false);
  assert.equal(evaluateSize({ notional: 15000, equity: EQUITY }).multiple, 3);
  assert.equal(evaluateSize({ notional: 35948, equity: EQUITY }).breach, true);
  assert.equal(evaluateSize({ notional: null, equity: EQUITY }), null);
  assert.equal(evaluateSize({ notional: 100, equity: 0 }), null);
});

test("ledger lines for one close collapse into a single realized event", () => {
  const rows = [
    { t: 1000, contract: "pf_xbtusd", pnl: -600, funding: 0, fee: 10 },
    { t: 1005, contract: "pf_xbtusd", pnl: -500, funding: 0, fee: 8 },
    { t: 1010, contract: "pf_uniusd", pnl: 40, funding: 0, fee: 2 },
  ];
  const events = realizedEvents(rows);
  assert.equal(events.length, 2);
  const xbt = events.find(e => e.contract === "PF_XBTUSD");
  assert.equal(xbt.net, -1118, "fee is a positive cost and is subtracted");
  assert.equal(events.find(e => e.contract === "PF_UNIUSD").net, 38);
});

test("a liquidation's penalty counts toward the realized loss", () => {
  // Kraken books the penalty in liquidation_fee, apart from the trading fee.
  const events = realizedEvents([{ t: 1000, contract: "pf_ethusd", pnl: -100, funding: 0, fee: 0.5, liqFee: 60 }]);
  assert.equal(events[0].net, -160.5);
});

test("realized events ignore unusable and non-contract ledger lines", () => {
  const rows = [
    { t: 1, contract: null, pnl: null, funding: null, fee: 0.0009 },
    { t: "bad", contract: "pf_xbtusd", pnl: -5, funding: 0, fee: 0 },
    { t: 2, contract: "pf_xbtusd", pnl: -5, funding: null, fee: null },
  ];
  const events = realizedEvents(rows);
  assert.deepEqual(events, [{ t: 2, contract: "PF_XBTUSD", net: -5 }]);
  assert.deepEqual(realizedEvents(null), []);
  assert.deepEqual(realizedEvents([], { windowSeconds: 0 }), []);
});

test("realized events can be bounded to a recent window", () => {
  const now = 10_000_000;
  const rows = [
    { t: (now - 10 * 24 * 3600 * 1000) / 1000, contract: "pf_old", pnl: -500, funding: 0, fee: 0 },
    { t: (now - 60_000) / 1000, contract: "pf_new", pnl: -500, funding: 0, fee: 0 },
  ];
  const all = realizedEvents(rows);
  assert.equal(all.length, 2);
  // The cooldown only ever looks at the last couple of hours, so older ledger
  // lines must be dropped before they reach the render path.
  const recent = realizedEvents(rows, { sinceMs: now - 2 * 3600 * 1000 });
  assert.deepEqual(recent.map(e => e.contract), ["PF_NEW"]);
  assert.deepEqual(realizedEvents(rows, { sinceMs: now + 1000 }), []);
});

test("evaluateRules takes pre-computed realized events, not the raw ledger", () => {
  const now = 10_000_000;
  const realized = [{ t: (now - 60_000) / 1000, contract: "PF_XBTUSD", net: -400 }];
  const result = evaluateRules({
    positions: [],
    account: { portfolioValue: EQUITY },
    realized,
    now,
  });
  assert.equal(result.cooldown.active, true);
  assert.equal(result.cooldown.trigger.contract, "PF_XBTUSD");
  // No events supplied means no cooldown, never a crash.
  assert.equal(evaluateRules({ account: { portfolioValue: EQUITY }, now }).cooldown.active, false);
});

test("cooldown activates on a loss over 3% of equity and expires after two hours", () => {
  const now = 10_000_000;
  const loss = { t: (now - 60_000) / 1000, contract: "PF_XBTUSD", net: -200 };
  const active = evaluateCooldown({ events: [loss], equity: EQUITY, now });
  assert.equal(active.active, true);
  assert.equal(active.trigger.contract, "PF_XBTUSD");
  assert.ok(active.remainingMs > 0 && active.remainingMs <= RULES.cooldownMs);

  // Same loss, but longer ago than the cooldown window.
  const stale = { ...loss, t: (now - RULES.cooldownMs - 1000) / 1000 };
  assert.equal(evaluateCooldown({ events: [stale], equity: EQUITY, now }).active, false);

  // A loss under the threshold never triggers.
  const small = { ...loss, net: -149 };
  assert.equal(evaluateCooldown({ events: [small], equity: EQUITY, now }).active, false);
  // Neither does a win.
  assert.equal(evaluateCooldown({ events: [{ ...loss, net: 900 }], equity: EQUITY, now }).active, false);
  // Future-dated rows are ignored rather than trusted.
  assert.equal(evaluateCooldown({ events: [{ ...loss, t: (now + 60_000) / 1000 }], equity: EQUITY, now }).active, false);
  assert.equal(evaluateCooldown({ events: [loss], equity: 0, now }), null);
});

test("the most recent qualifying loss drives the countdown", () => {
  const now = 10_000_000;
  const older = { t: (now - 90 * 60_000) / 1000, contract: "PF_A", net: -300 };
  const newer = { t: (now - 10 * 60_000) / 1000, contract: "PF_B", net: -300 };
  const result = evaluateCooldown({ events: [older, newer], equity: EQUITY, now });
  assert.equal(result.trigger.contract, "PF_B");
});

test("countdown formatting", () => {
  assert.equal(formatCountdown(0), "0m");
  assert.equal(formatCountdown(-5), "0m");
  assert.equal(formatCountdown(60_000), "1m");
  assert.equal(formatCountdown(90 * 60_000), "1h 30m");
  assert.equal(formatCountdown(NaN), "0m");
});

test("evaluateRules combines positions, basket and cooldown", () => {
  const instruments = [
    { symbol: "PF_UNIUSD", contractSize: 1, type: "flexible_futures" },
    { symbol: "PF_XBTUSD", contractSize: 1, type: "flexible_futures" },
  ];
  const positions = [
    { symbol: "PF_UNIUSD", side: "long", size: 100, price: 6 },
    { symbol: "PF_XBTUSD", side: "long", size: 1, price: 60000 },
  ];
  const tickers = { PF_UNIUSD: { last: 8 }, PF_XBTUSD: { last: 61000 } };
  const upnls = { PF_UNIUSD: 200, PF_XBTUSD: -50 };
  const result = evaluateRules({
    positions, instruments, tickers,
    account: { portfolioValue: EQUITY },
    upnlFor: p => upnls[p.symbol],
    statsRows: [],
    now: 1_000_000,
  });
  assert.equal(result.equity, EQUITY);
  assert.equal(result.rows.length, 2);
  assert.equal(result.rows[0].takeProfit.state, "take");
  // 1 BTC at 61000 on 5000 equity is 12.2x.
  assert.equal(result.rows[1].size.breach, true);
  assert.equal(result.basket.upnl, 150);
  assert.equal(result.basket.positions, 2);
  assert.equal(result.cooldown.active, false);
});

test("evaluateRules reports nulls rather than zeros when inputs are unreadable", () => {
  const result = evaluateRules({
    positions: [{ symbol: "PF_UNIUSD", side: "long", size: 100, price: 6 }],
    instruments: [],
    tickers: {},
    account: {},
    upnlFor: () => null,
    now: 1,
  });
  assert.equal(result.equity, null);
  assert.equal(result.rows[0].takeProfit, null);
  assert.equal(result.rows[0].size, null);
  assert.equal(result.basket.upnl, null, "one unreadable position must not total to 0");
  assert.equal(result.cooldown, null);
});

test("evaluateRules skips errored and flat positions", () => {
  const result = evaluateRules({
    positions: [
      { symbol: "PF_A", side: "long", size: 0, price: 1 },
      { symbol: "PF_B", error: "stale" },
    ],
    account: { portfolioValue: EQUITY },
    upnlFor: () => 10,
    now: 1,
  });
  assert.equal(result.rows.length, 0);
  assert.equal(result.basket.positions, 0);
});

test("an exit filled in clips is one loss, however it straddles the clock", () => {
  // Measured against this account: ENA lost $360 across 191 seconds in 12 fills.
  // Fixed 60-second buckets split it into sub-threshold pieces, so the largest
  // qualifying loss in the whole history never started a cooldown.
  const start = 1_000_000;
  const rows = Array.from({ length: 12 }, (_, i) =>
    ({ t: start + i * 17, contract: "pf_enausd", pnl: -30, funding: 0, fee: 0 }));
  const events = realizedEvents(rows);
  assert.equal(events.length, 1, "one exit is one event");
  assert.equal(events[0].net, -360);
  assert.equal(events[0].t, start + 11 * 17, "stamped at the last fill, so the pause runs from the end");
  const cooldown = evaluateCooldown({
    events, equity: 5000, now: (start + 11 * 17) * 1000 + 1000,
  });
  assert.equal(cooldown.active, true, "-$360 on $5,000 equity is over the 3% limit");
});

test("fills further apart than the window are separate exits", () => {
  const apart = realizedEvents([
    { t: 1000, contract: "pf_enausd", pnl: -100, funding: 0, fee: 0 },
    { t: 1061, contract: "pf_enausd", pnl: -100, funding: 0, fee: 0 },
  ]);
  assert.equal(apart.length, 2, "a 61-second gap starts a new exit");
  const touching = realizedEvents([
    { t: 1000, contract: "pf_enausd", pnl: -100, funding: 0, fee: 0 },
    { t: 1060, contract: "pf_enausd", pnl: -100, funding: 0, fee: 0 },
  ]);
  assert.equal(touching.length, 1, "exactly at the window they are still one");
  assert.equal(touching[0].net, -200);
  // Clustering is per contract: two symbols exiting together stay distinct.
  const mixed = realizedEvents([
    { t: 1000, contract: "pf_enausd", pnl: -100, funding: 0, fee: 0 },
    { t: 1001, contract: "pf_uniusd", pnl: -100, funding: 0, fee: 0 },
  ]);
  assert.equal(mixed.length, 2);
});

test("ledger rows arriving out of order still cluster correctly", () => {
  const events = realizedEvents([
    { t: 1030, contract: "pf_enausd", pnl: -50, funding: 0, fee: 0 },
    { t: 1000, contract: "pf_enausd", pnl: -50, funding: 0, fee: 0 },
    { t: 1200, contract: "pf_enausd", pnl: -50, funding: 0, fee: 0 },
  ]).sort((a, b) => a.t - b.t);
  assert.equal(events.length, 2);
  assert.equal(events[0].net, -100);
  assert.equal(events[1].net, -50);
});

test("the size cap prices exposure off the book, not a quiet tape", () => {
  // The tape is 10% away from the live book, which is the case that made position
  // PnL wrong. Exposure is measured at mid: it is what the position is worth, not
  // what closing it would realise.
  const result = evaluateRules({
    positions: [{ symbol: "PF_UNIUSD", side: "long", size: 1000, price: 6 }],
    account: { portfolioValue: 5000 },
    instruments: [{ symbol: "PF_UNIUSD", contractSize: 1, type: "flexible_futures" }],
    tickers: { PF_UNIUSD: { symbol: "PF_UNIUSD", bid: 6, ask: 6.02, last: 6.6 } },
    peaks: {}, realized: [], upnlFor: () => 0, now: 1_000_000,
  });
  assert.equal(result.rows[0].size.notional, 6010, "book mid, not the stale last (6600)");
  assert.equal(result.basket.notional, 6010);
  // With no book at all it still works from the last trade.
  const noBook = evaluateRules({
    positions: [{ symbol: "PF_UNIUSD", side: "long", size: 1000, price: 6 }],
    account: { portfolioValue: 5000 },
    instruments: [{ symbol: "PF_UNIUSD", contractSize: 1, type: "flexible_futures" }],
    tickers: { PF_UNIUSD: { symbol: "PF_UNIUSD", last: 6.6 } },
    peaks: {}, realized: [], upnlFor: () => 0, now: 1_000_000,
  });
  assert.equal(noBook.rows[0].size.notional, 6600);
});
