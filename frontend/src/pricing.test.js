import test from "node:test";
import assert from "node:assert/strict";
import { exitPrice, isLastStale, midPrice, valuationPrice } from "./pricing.js";

const book = { bid: 100, ask: 102, last: 101, markPrice: 100.5 };

test("mid is the middle of a two-sided book", () => {
  assert.equal(midPrice(book), 101);
  for (const t of [{ bid: 100 }, { ask: 100 }, { bid: 0, ask: 100 },
    { bid: 100, ask: null }, {}, null]) {
    assert.equal(midPrice(t), null);
  }
});

test("exit price is the side you actually close into", () => {
  assert.equal(exitPrice(book, "long"), 100, "a long sells into the bid");
  assert.equal(exitPrice(book, "short"), 102, "a short buys from the ask");
  assert.equal(exitPrice(book, "LONG"), 100);
  assert.equal(exitPrice(book, "sideways"), null);
  assert.equal(exitPrice({ bid: 0, ask: 102 }, "long"), null);
});

test("last is stale when it sits outside the live book", () => {
  assert.equal(isLastStale(book), false);
  assert.equal(isLastStale({ ...book, last: 99 }), true);
  assert.equal(isLastStale({ ...book, last: 103 }), true);
  assert.equal(isLastStale({ ...book, last: 100 }), false, "touching the bid is not stale");
  assert.equal(isLastStale({ ...book, last: 102 }), false, "touching the ask is not stale");
  // Without a two-sided book there is nothing to compare against.
  assert.equal(isLastStale({ last: 101 }), false);
});

test("valuation prefers the book and reports which basis it used", () => {
  assert.deepEqual(valuationPrice(book), { price: 101, basis: "mid" });
  assert.deepEqual(valuationPrice(book, { mode: "exit", side: "long" }),
    { price: 100, basis: "exit" });
  assert.deepEqual(valuationPrice(book, { mode: "exit", side: "short" }),
    { price: 102, basis: "exit" });
});

test("valuation falls back to last only when the book is unusable", () => {
  const oneSided = { bid: 100, ask: null, last: 101 };
  // A one-sided book cannot produce a mid, so last is used and reported as such.
  assert.deepEqual(valuationPrice(oneSided), { price: 101, basis: "last" });
  // Exit side still works when its own side is present.
  assert.deepEqual(valuationPrice(oneSided, { mode: "exit", side: "long" }),
    { price: 100, basis: "exit" });
  // Exit side missing falls through to mid, then last.
  assert.deepEqual(valuationPrice(oneSided, { mode: "exit", side: "short" }),
    { price: 101, basis: "last" });
  assert.deepEqual(valuationPrice({ last: 101 }), { price: 101, basis: "last" });
  assert.deepEqual(valuationPrice({}), { price: null, basis: "none" });
  assert.deepEqual(valuationPrice(null), { price: null, basis: "none" });
});

test("a stale last does not leak into a valuation when the book is present", () => {
  // The exact case that motivated this: tape quiet, last 1.5% above the book.
  const quiet = { bid: 100, ask: 100.2, last: 101.5 };
  assert.equal(isLastStale(quiet), true);
  assert.equal(valuationPrice(quiet).price, 100.1);
  assert.equal(valuationPrice(quiet, { mode: "exit", side: "long" }).price, 100);
});

test("a quiet tape yields to mark, a recent trade does not", () => {
  const now = 1_700_000_000_000;
  const fresh = { last: 0.0362, lastTime: now - 5_000, markPrice: 0.036193 };
  assert.deepEqual(valuationPrice(fresh, { now }), { price: 0.0362, basis: "last" },
    "a recent trade is still the best evidence when there is no book");

  // The Hyperliquid case: a position in a symbol whose book was never fetched, on a
  // thin coin whose last trade is minutes old while the market has moved away.
  const quiet = { last: 0.0362, lastTime: now - 10 * 60_000, markPrice: 0.036193 };
  assert.deepEqual(valuationPrice(quiet, { now }), { price: 0.036193, basis: "mark" });
  assert.deepEqual(valuationPrice(quiet, { mode: "exit", side: "long", now }),
    { price: 0.036193, basis: "mark" });

  // A quiet tape with no mark still reports the stale trade rather than nothing,
  // and says so.
  assert.deepEqual(valuationPrice({ last: 0.0362, lastTime: now - 10 * 60_000 }, { now }),
    { price: 0.0362, basis: "last" });

  // Kraken tickers carry no trade timestamp, so their behaviour is unchanged.
  assert.deepEqual(valuationPrice({ last: 110, markPrice: 150 }, { now }),
    { price: 110, basis: "last" });
  for (const lastTime of [undefined, null, 0, -1, "NaN", Infinity]) {
    assert.equal(valuationPrice({ last: 110, lastTime, markPrice: 150 }, { now }).basis, "last");
  }
  assert.deepEqual(valuationPrice({ markPrice: 0, last: 0 }, { now }), { price: null, basis: "none" });
});

test("mark never displaces a usable book, however quiet the tape", () => {
  // The reason mark was rejected as a primary basis: it is not tradeable and can
  // dislocate on a thin pair, showing a freshly opened position as deep in the red.
  const now = 1_700_000_000_000;
  const dislocated = { bid: 100, ask: 100.2, last: 100.1, lastTime: now - 10 * 60_000, markPrice: 150 };
  assert.equal(valuationPrice(dislocated, { now }).basis, "mid");
  assert.equal(valuationPrice(dislocated, { now }).price, 100.1);
  assert.equal(valuationPrice(dislocated, { mode: "exit", side: "long", now }).price, 100);
  assert.equal(valuationPrice(dislocated, { mode: "exit", side: "short", now }).price, 100.2);
  // A one-sided book still beats mark on the side it can serve.
  assert.equal(valuationPrice({ bid: 100, ask: null, lastTime: now - 10 * 60_000, markPrice: 150 },
    { mode: "exit", side: "long", now }).price, 100);
});
