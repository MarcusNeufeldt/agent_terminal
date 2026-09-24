import test from "node:test";
import assert from "node:assert/strict";
import { leverageChoices, pairMaxLeverage } from "./leverage.js";

test("leverage buttons stop at the pair maximum and include it", () => {
  assert.deepEqual(leverageChoices(3), [1, 2, 3]);
  assert.deepEqual(leverageChoices(5), [1, 2, 3, 5]);
  assert.deepEqual(leverageChoices(10), [1, 2, 3, 5, 10]);
  assert.deepEqual(leverageChoices(40), [1, 2, 3, 5, 10, 20, 25, 40]);
  assert.deepEqual(leverageChoices(7), [1, 2, 3, 5, 7]);
  assert.deepEqual(leverageChoices(null), [1, 2, 3, 5, 10]);
  assert.deepEqual(leverageChoices(50, [1, 2, 3, 5, 10]), [1, 2, 3, 5, 10, 50]);
});

test("pair maximum comes from Hyperliquid maxLeverage or Kraken's first margin tier", () => {
  assert.equal(pairMaxLeverage({ maxLeverage: 5 }), 5);
  assert.equal(pairMaxLeverage({ maxLeverage: null, retailMarginLevels: [{ initialMargin: 0.02 }] }), 50);
  assert.equal(pairMaxLeverage({ marginLevels: [{ initialMargin: 0.01 }, { initialMargin: 0.02 }] }), 100);
  assert.equal(pairMaxLeverage({ retailMarginLevels: [] }), null);
  assert.equal(pairMaxLeverage({}), null);
  assert.equal(pairMaxLeverage(undefined), null);
});
