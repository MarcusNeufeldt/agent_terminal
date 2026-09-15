import test from "node:test";
import assert from "node:assert/strict";
import { previewProtection, riskOverlays, snapOverlayPrice, TerminalOverlayLayer, VelaChartController, overlayIntent } from "./vela-overlays.js";

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

test("Native SL drag retains SL above entry and ignores PnL-only updates", () => {
  const line = { key: "sl", price: 90, protection: { kind: "sl", symbol: "HL_APT", entry: 100, size: 2, dir: 1, tick: 0.1,
    snapshot: { unrealizedPnl: 1 } } };
  assert.equal(previewProtection(line, 110).drop.kind, "sl");
  assert.equal(overlayIntent(line), overlayIntent({ ...line, protection: { ...line.protection, snapshot: { unrealizedPnl: 5 } } }));
  assert.notEqual(overlayIntent(line), overlayIntent({ ...line, protection: { ...line.protection, size: 1 } }));
});

test("Pointer drop routes native SL once and removes the optimistic preview after refusal", async () => {
  const controller = new VelaChartController({ cells: () => [], on: () => () => {} });
  controller.publishCell = () => {};
  controller.bindCell({ id: "fixture" });
  const targetId = controller.cells.get("fixture").targetId;
  const line = { key: "sl", price: 90, protection: { kind: "sl", symbol: "HL_APT", entry: 100, size: 2, dir: 1, tick: 1 } };
  const layer = new TerminalOverlayLayer();
  layer.args = { coords: { yToPrice: () => 95 }, scale: {}, bounds: {} };
  layer.data = { targetId, lines: [line] };
  layer.point = () => ({ x: 1, y: 1 });
  layer.render = () => {};
  let calls = 0;
  controller.onProtectionDrop = drop => { calls++; assert.equal(drop.kind, "sl"); return false; };
  const event = { type: "pointerup", preventDefault() {}, stopImmediatePropagation() {} };
  layer.drag = { line, price: 90 }; layer.dragTargetId = targetId;
  layer.onDragEnd(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
  assert.equal(layer.pending.size, 0);
  assert.equal(layer.data.lines[0].price, 90);
  for (const type of ["pointercancel", "pointerup"]) {
    layer.drag = { line, price: 90 }; layer.dragTargetId = targetId;
    layer.data.lines = type === "pointerup" ? [] : [line];
    layer.onDragEnd({ ...event, type });
  }
  assert.equal(calls, 1, "Cancel and vanished target never dispatch");
  controller.destroy();
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
