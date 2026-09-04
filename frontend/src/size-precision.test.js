import test from "node:test";
import assert from "node:assert/strict";
import { formatContractSize, normalizeContractSize } from "./size-precision.js";

test("normalizes positive contract precision by rounding down", () => {
  assert.equal(normalizeContractSize(1.239, 2), 1.23);
  assert.equal(formatContractSize(1.239, 2), "1.23");
});

test("normalizes negative precision to whole contract lots", () => {
  assert.equal(normalizeContractSize(12345, -2), 12300);
  assert.equal(formatContractSize(12345, -2), "12300");
  assert.equal(normalizeContractSize(99, -2), 0);
});
