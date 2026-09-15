import test from "node:test";
import assert from "node:assert/strict";
import { hyperliquidProtection } from "./hyperliquid-protection.js";
import { compareContractSizes } from "./size-precision.js";

const position = { symbol: "HL_APT", side: "long", sizeExact: "2" };
const stop = { symbol: "HL_APT", exchange: "hyperliquid", order_id: "12345678901234567890",
  side: "sell", reduceOnly: true, orderType: "stp", stopPrice: 0.5, unfilledSizeExact: "2" };
const check = (orders, p = position) => hyperliquidProtection(p, orders, "current", "current");

test("stop observations distinguish missing, smaller and full-size orders without guaranteeing fills", () => {
  assert.equal(check([]).state, "missing");
  assert.equal(check([stop]).state, "observed");
  assert.match(check([stop]).detail, /not guaranteed/);
  assert.equal(check([{ ...stop, unfilledSizeExact: "1.999999999999999999" }]).state, "smaller");
  assert.equal(check([{ ...stop, unfilledSizeExact: "3" }]).state, "observed");
  const halves = [{ ...stop, unfilledSizeExact: "1" }, { ...stop, order_id: "2", unfilledSizeExact: "1" }];
  assert.equal(check(halves).state, "smaller", "does not claim to assess combined ladder coverage");
  assert.equal(check([{ ...stop, side: "buy" }], { ...position, side: "short" }).state, "observed");
});

test("native zero-size stop follows current long and short positions without confusing fixed zero orders", () => {
  for (const sizeExact of ["1", "3781", "0.1"]) {
    assert.equal(check([{ ...stop, positionTpsl: true, unfilledSizeExact: "0" }], { ...position, sizeExact }).state, "observed");
    assert.equal(check([{ ...stop, positionTpsl: true, side: "buy", unfilledSizeExact: "0" }],
      { ...position, side: "short", sizeExact }).state, "observed");
  }
  assert.equal(check([{ ...stop, positionTpsl: false, unfilledSizeExact: "0" }]).state, "unknown");
});

test("only opposite-side reduce-only stops count, never take-profit or entry orders", () => {
  for (const patch of [{ side: "buy" }, { reduceOnly: false }, { orderType: "take_profit" }, { orderType: "lmt" }, { symbol: "HL_BTC" }]) {
    assert.equal(check([{ ...stop, ...patch }]).state, "missing");
  }
});

test("stale, missing, malformed and conflicting data cannot establish missing or sufficient stops", () => {
  assert.equal(hyperliquidProtection(position, [], "stale", "current").state, "unknown");
  assert.equal(hyperliquidProtection(position, [], "current", "stale").state, "unknown");
  for (const orders of [null, [null], [{ error: "unavailable" }], [stop, stop],
    [{ ...stop, order_id: 123 }], [{ ...stop, exchange: "kraken" }], [{ ...stop, symbol: "PF_APTUSD" }],
    [{ ...stop, stopPrice: null }], [{ ...stop, unfilledSizeExact: undefined }], [{ ...stop, unfilledSizeExact: "0" }],
    [{ ...stop, reduceOnly: "true" }], [{ ...stop, order_id: "99999999999999999999" }]]) {
    assert.equal(check(orders).state, "unknown");
  }
  for (const p of [null, { ...position, symbol: 1 }, { ...position, sizeExact: undefined }, { ...position, side: "flat" }]) {
    assert.equal(check([], p).state, "unknown");
  }
});

test("contract-size comparison is exact and rejects invalid quantities", () => {
  assert.equal(compareContractSizes("1.999999999999999999", "2"), -1);
  assert.equal(compareContractSizes("20e-1", "2.00"), 0);
  assert.equal(compareContractSizes("2.000000000000000001", "2"), 1);
  for (const value of [null, true, 0, -1, Infinity, "1e999", "bad"]) assert.equal(compareContractSizes(value, "2"), null);
});
