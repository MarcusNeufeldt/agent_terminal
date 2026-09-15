import test from "node:test";
import assert from "node:assert/strict";
import { contractsForNotional, formatContractSize, normalizeContractSize } from "./size-precision.js";

test("normalizes positive contract precision by rounding down", () => {
  assert.equal(normalizeContractSize(1.239, 2), 1.23);
  assert.equal(formatContractSize(1.239, 2), "1.23");
});

test("USD sizing uses exact decimal division and rounds down for both venue tickets", () => {
  assert.equal(contractsForNotional("0.3", "0.1", 0), "3");
  assert.equal(contractsForNotional("10", "0.6", 2), "16.66");
  assert.equal(contractsForNotional("12", "0.6", 2), "20.00");
  assert.equal(contractsForNotional("100", "20000", 5), "0.00500");
  assert.equal(contractsForNotional("30", "10", 0), "3", "inverse contract USD face value works without a coin price");
  assert.equal(contractsForNotional("0.01", "1e-7", 0), "100000");
  assert.equal(contractsForNotional("12500", "3", -3), "4000");
  assert.equal(contractsForNotional("0.001", "100", 2), "0.00");
  for (const value of [null, "", "abc", -1, 0, Infinity, "1e999", "1e30"]) {
    assert.equal(contractsForNotional(value, 1, 2), "");
  }
  assert.equal(contractsForNotional(10, 0, 2), "");
  assert.equal(contractsForNotional(10, 1, 2.5), "");
});

test("USD-sized contract quantities never exceed the requested notional", () => {
  for (const cents of [1000n, 1001n, 10000n]) {
    for (const price of ["0.6", "0.00013", "1.03", "20000"]) {
      for (const precision of [0, 2, 5]) {
        const qty = contractsForNotional(`${cents / 100n}.${String(cents % 100n).padStart(2, "0")}`, price, precision);
        const qtyUnits = BigInt(qty.replace(".", ""));
        const priceUnits = BigInt(price.replace(".", ""));
        const scale = 10n ** BigInt(precision + (price.split(".")[1]?.length || 0));
        assert.ok(qtyUnits * priceUnits * 100n <= cents * scale, `${qty} at ${price} exceeds budget`);
      }
    }
  }
});

test("Percentage sizing uses exact division, floors lots and never multiplies leverage again", () => {
  assert.equal(contractsForNotional("123.456", 1, 2, 25), "30.86");
  assert.equal(contractsForNotional("123.456", 1, 2, 50), "61.72");
  assert.equal(contractsForNotional("123.456", 1, 2, 75), "92.59");
  assert.equal(contractsForNotional("123.456", 1, 2, 100), "123.45");
  for (const percent of [0, -1, 101, 25.5, "25", NaN]) assert.equal(contractsForNotional(100, 1, 2, percent), "");
});

test("normalizes negative precision to whole contract lots", () => {
  assert.equal(normalizeContractSize(12345, -2), 12300);
  assert.equal(formatContractSize(12345, -2), "12300");
  assert.equal(normalizeContractSize(99, -2), 0);
});
