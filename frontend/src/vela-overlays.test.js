import test from "node:test";
import assert from "node:assert/strict";
import { previewProtection, riskOverlays, snapOverlayPrice } from "./vela-overlays.js";

const takeProfit = {
  key: "order-stop:tp-1",
  price: 120,
  color: "#26a69a",
  tp: { symbol: "PF_TESTUSD", entry: 100, size: 2, mult: 1, dir: 1, tick: 0.5, fullPosition: true },
};

test("snaps overlay drags to the contract tick", () => {
  assert.equal(snapOverlayPrice(101.24, 0.5), 101);
  assert.equal(snapOverlayPrice(101.26, 0.5), 101.5);
});

test("turns position-handle drags into deterministic TP or SL previews", () => {
  const position = {
    key: "position:PF_TESTUSD",
    price: 100,
    position: { symbol: "PF_TESTUSD", entry: 100, size: 2, mult: 1, dir: 1, tick: 0.5 },
  };
  const stop = previewProtection(position, 89.8);
  assert.equal(stop.price, 90);
  assert.equal(stop.drop.kind, "sl");
  assert.equal(stop.drop.pnl, -20);
  assert.match(stop.title, /^SL 90\.0 \(-\$20\.00\)$/);
});

test("builds all three display-only mirrored risk levels", () => {
  const risks = riskOverlays([takeProfit]);
  assert.deepEqual(risks.map(line => line.price), [80, 60, 40]);
  assert.deepEqual(risks.map(line => line.key), [
    "risk:order-stop:tp-1:1",
    "risk:order-stop:tp-1:2",
    "risk:order-stop:tp-1:3",
  ]);
});
