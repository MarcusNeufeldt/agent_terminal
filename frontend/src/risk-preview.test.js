import test from "node:test";
import assert from "node:assert/strict";
import { mirroredRisk } from "./risk-preview.js";

test("mirrors profit into the selected loss multiple", () => {
  for (const [ratio, price, pnl] of [[1, 90, -100], [2, 80, -200], [3, 70, -300]]) {
    assert.deepEqual(mirroredRisk({ entry: 100, target: 110, dir: 1, ratio, tick: 1, size: 10 }), { price, pnl });
  }
});

test("mirrors upward for a short", () => {
  assert.deepEqual(
    mirroredRisk({ entry: 100, target: 90, dir: -1, ratio: 3, tick: 1, size: 10 }),
    { price: 130, pnl: -300 },
  );
});
