import test from "node:test";
import assert from "node:assert/strict";
import { linearExitPreview, mirroredRisk, riskLineOptions } from "./risk-preview.js";

test("linear exit estimates only the covered position and uses the correct direction", () => {
  const position = { side: "long", price: 100, size: 10 };
  assert.deepEqual(linearExitPreview({ position, quantity: 2, exitPrice: 90 }),
    { pnl: -20, coveredSize: 2, coveragePct: 20, oversized: false, side: "sell" });
  assert.equal(linearExitPreview({ position: { ...position, side: "short" }, quantity: 2, exitPrice: 90 }).pnl, 20);
  assert.deepEqual(linearExitPreview({ position, quantity: 15, exitPrice: 110 }),
    { pnl: 100, coveredSize: 10, coveragePct: 100, oversized: true, side: "sell" });
});

test("linear exit estimates reject unavailable, malformed or nonfinite input", () => {
  const valid = { position: { side: "long", price: 100, size: 10 }, quantity: 2, exitPrice: 90 };
  for (const patch of [{ position: null }, { quantity: 0 }, { quantity: Infinity }, { exitPrice: null },
    { exitPrice: true }, { quantity: [2] }, { position: { ...valid.position, error: "stale" } },
    { position: { ...valid.position, side: "unknown" } }, { position: { ...valid.position, price: 0 } }]) {
    assert.equal(linearExitPreview({ ...valid, ...patch }), null);
  }
});

test("mirrors profit into the selected loss multiple", () => {
  for (const [ratio, price, pnl] of [[1, 90, -100], [2, 80, -200], [3, 70, -300]]) {
    assert.deepEqual(mirroredRisk({ entry: 100, target: 110, dir: 1, ratio, tick: 1, size: 10 }), { price, pnl });
  }
});

test("price-line creation options include the required initial price", () => {
  assert.deepEqual(riskLineOptions(12.34), {
    price: 12.34, color: "#ef5350", lineWidth: 2, axisLabelVisible: true,
  });
});

test("mirrors upward for a short", () => {
  assert.deepEqual(
    mirroredRisk({ entry: 100, target: 90, dir: -1, ratio: 3, tick: 1, size: 10 }),
    { price: 130, pnl: -300 },
  );
});
