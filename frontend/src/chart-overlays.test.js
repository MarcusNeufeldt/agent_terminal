import test from "node:test";
import assert from "node:assert/strict";
import { buildChartOverlays } from "./chart-overlays.js";
import { riskOverlays } from "./vela-overlays.js";

const positions = [{ symbol: "PF_XBTUSD", side: "long", size: 2, price: 100, liqPriceEstimate: 60 }];
const instruments = [{ symbol: "PF_XBTUSD", tickSize: 0.5, contractSize: 1 }];

test("builds position, liquidation, and exact-order overlays", () => {
  const lines = buildChartOverlays("PF_XBTUSD", positions, [
    { symbol: "PF_XBTUSD", order_id: "tp-1", orderType: "take_profit", side: "sell", stopPrice: 120, size: 1, unfilledSize: 1 },
    { symbol: "PF_XBTUSD", cliOrdId: "limit-1", orderType: "lmt", side: "buy", limitPrice: 90, size: 3 },
  ], instruments);

  assert.deepEqual(lines.map(line => line.key), [
    "position:PF_XBTUSD",
    "liquidation:PF_XBTUSD",
    "order-stop:tp-1",
    "order-limit:limit-1",
  ]);
  const takeProfit = lines[2];
  assert.equal(takeProfit.tp.size, 1);
  assert.equal(takeProfit.tp.fullPosition, false);
  assert.match(takeProfit.title, /50%/);
  assert.equal(takeProfit.order.orderId, "tp-1");
});

test("Kraken stop-loss lines are draggable, so a stop can be moved into profit", () => {
  const lines = buildChartOverlays("PF_XBTUSD", positions, [
    { symbol: "PF_XBTUSD", order_id: "sl-1", orderType: "stop", side: "sell",
      stopPrice: 95, size: 2, unfilledSize: 2, reduceOnly: true },
  ], instruments);
  const stop = lines.find(line => line.key === "order-stop:sl-1");
  assert.ok(stop.protection, "an existing stop must carry drag metadata like TP does");
  assert.equal(stop.protection.kind, "sl", "the drop routes to replace_sl, not replace_tp");
  assert.equal(stop.protection.entry, 100);
  assert.equal(stop.protection.size, 2);
  assert.equal(stop.protection.dir, 1);
  assert.equal(stop.protection.tick, 0.5);
  assert.equal(stop.protection.symbol, "PF_XBTUSD");
  assert.equal(stop.protection.order.orderId, "sl-1", "edits target the exact order");
  assert.ok(!stop.tp, "a stop is not a take profit");

  // A stop above entry is still a stop: the server validates against mark, not entry,
  // so locking in profit must produce the same draggable line.
  const inProfit = buildChartOverlays("PF_XBTUSD", positions, [
    { symbol: "PF_XBTUSD", order_id: "sl-2", orderType: "stp", side: "sell",
      stopPrice: 110, size: 2, unfilledSize: 2, reduceOnly: true },
  ], instruments).find(line => line.key === "order-stop:sl-2");
  assert.equal(inProfit.protection.kind, "sl");
  assert.match(inProfit.title, /\+\$19\.89 after fee/, "a stop in profit shows the gain it locks in, less the 5bp taker fee");
});

test("take profit keeps its own drag path and a stop without a position is not draggable", () => {
  const tpLine = buildChartOverlays("PF_XBTUSD", positions, [
    { symbol: "PF_XBTUSD", order_id: "tp-1", orderType: "take_profit", side: "sell", stopPrice: 120, size: 2, unfilledSize: 2 },
  ], instruments).find(line => line.key === "order-stop:tp-1");
  assert.ok(tpLine.tp, "take profit still drags through the tp path");
  assert.ok(!tpLine.protection, "take profit must not be relabelled as a stop loss");

  // No position means no entry to price the stop against.
  const orphan = buildChartOverlays("PF_XBTUSD", [], [
    { symbol: "PF_XBTUSD", order_id: "sl-3", orderType: "stop", side: "sell", stopPrice: 95, size: 2 },
  ], instruments).find(line => line.key === "order-stop:sl-3");
  assert.ok(!orphan.protection);
  assert.ok(!orphan.tp);
});

test("Hyperliquid can expose exact cancellation without position or TP dragging", () => {
  const symbol = "HL_APT", id = "12345678901234567890";
  const pos = positions.map(p => ({ ...p, symbol }));
  const orders = [{ symbol, order_id: id, orderType: "take_profit", side: "sell", stopPrice: 120, size: 1 }];
  const locked = buildChartOverlays(symbol, pos, orders, [], true);
  assert.ok(locked.every(line => !line.order && !line.tp && !line.position));
  const cancellable = buildChartOverlays(symbol, pos, orders, [], true, true);
  assert.ok(cancellable.every(line => !line.tp && !line.position));
  const order = cancellable.find(line => line.order)?.order;
  assert.equal(order.orderId, id);
  assert.equal(order.symbol, symbol);
});

test("Native protection capability enables exact TP/SL and position handles but not limit moves", () => {
  const symbol = "HL_APT";
  const pos = [{ symbol, side: "long", size: 2, sizeExact: "2", price: 100 }];
  const orders = [
    { symbol, order_id: "12", orderType: "stp", triggerKind: "sl", triggerMarket: true, reduceOnly: true,
      side: "sell", stopPrice: 90, limitPrice: 90, unfilledSizeExact: "1", unfilledSize: 1 },
    { symbol, order_id: "13", orderType: "lmt", side: "buy", limitPrice: 80 },
  ];
  const lines = buildChartOverlays(symbol, pos, orders, [], true, true, true);
  assert.ok(lines.find(line => line.position));
  const stop = lines.find(line => line.protection);
  assert.equal(stop.protection.kind, "sl");
  assert.equal(stop.protection.order.snapshot.order_id, "12");
  assert.ok(lines.filter(line => line.key.startsWith("order-limit:")).every(line => !line.protection && !line.tp));
  const locked = buildChartOverlays(symbol, pos, orders, [], true, true, false);
  assert.ok(locked.every(line => !line.protection && !line.position));
  const tp = buildChartOverlays(symbol, pos, [{ ...orders[0], order_id: "14", orderType: "take_profit", triggerKind: "tp", stopPrice: 120 }], [], true, true, true)
    .find(line => line.protection);
  assert.equal(tp.tp.size, 1);
  assert.equal(riskOverlays([tp]).length, 3);
});

test("native whole-position TP profit follows size and entry changes while fixed ladders do not", () => {
  const symbol = "HL_GRIFFAIN";
  const native = { symbol, order_id: "123", orderType: "take_profit", triggerKind: "tp", triggerMarket: true,
    reduceOnly: true, positionTpsl: true, side: "sell", stopPrice: 0.013067, limitPrice: 0.013067,
    size: 0, unfilledSize: 0, unfilledSizeExact: "0" };
  for (const [size, entry] of [[1238, 0.01285], [3781, 0.012273], [1000, 0.012273]]) {
    const pos = [{ symbol, side: "long", size, sizeExact: String(size), price: entry }];
    const line = buildChartOverlays(symbol, pos, [native], [], true, true, true).find(l => l.protection);
    assert.equal(line.tp.size, size);
    assert.equal(line.tp.fullPosition, true);
    assert.match(line.title, /100% · auto size/);
    const net = (native.stopPrice - entry) * size - 0.00045 * size * native.stopPrice;
    assert.ok(line.title.includes(`${net.toFixed(2)} after fee`), `${line.title} lacks ${net.toFixed(2)}`);
    assert.equal(riskOverlays([line]).length, 3);
    const fixed = { ...native, positionTpsl: false, size: 1238, unfilledSize: 1238, unfilledSizeExact: "1238" };
    const fixedLine = buildChartOverlays(symbol, pos, [fixed], [], true, true, true).find(l => l.key.startsWith("order-stop:"));
    assert.doesNotMatch(fixedLine.title, /auto size/);
    if (size === 3781) assert.match(fixedLine.title, /0.98.*33%/);
  }
});

test("keeps symbol overlays isolated", () => {
  assert.deepEqual(buildChartOverlays("PF_ETHUSD", positions, [], instruments), []);
});

test("a resting reduce-only exit limit is a draggable maker TP; entries and Chase exits are not", async () => {
  const { previewProtection } = await import("./vela-overlays.js");
  const lines = buildChartOverlays("PF_XBTUSD", positions, [
    { symbol: "PF_XBTUSD", order_id: "mk-1", cliOrdId: "kt-full-tp-PF_XBTUSD-1", orderType: "lmt", side: "sell",
      limitPrice: 120, reduceOnly: true, size: 2, unfilledSize: 2 },
    { symbol: "PF_XBTUSD", order_id: "entry-1", orderType: "lmt", side: "buy", limitPrice: 90, reduceOnly: false, size: 1 },
    { symbol: "PF_XBTUSD", order_id: "ch-1", cliOrdId: "ch-726da97f-4-de2425", orderType: "lmt", side: "sell",
      limitPrice: 121, reduceOnly: true, size: 2 },
  ], instruments);
  const tp = lines.find(line => line.key === "order-stop:kt-full-tp-PF_XBTUSD-1");
  assert.ok(tp && tp.tp, "the maker TP drags through the tp path");
  assert.equal(tp.price, 120);
  assert.equal(tp.tp.maker, true);
  assert.equal(tp.tp.feeRate, 0.000175, "Kraken maker fee");
  assert.equal(tp.tp.stopFeeRate, 0.0005, "a mirrored stop still pays the taker fee");
  // (120 - 100) * 2 = 40 gross, less 0.0175% of 240 = 0.042.
  assert.match(tp.title, /\+\$39\.96 after maker fee/);
  assert.ok(!lines.some(line => line.key === "order-limit:kt-full-tp-PF_XBTUSD-1"), "no duplicate plain limit line");
  assert.ok(lines.find(line => line.key === "order-limit:entry-1"), "an entry limit stays a plain order line");
  const chase = lines.find(line => line.key === "order-limit:ch-726da97f-4-de2425");
  assert.ok(chase && !chase.tp, "a Chase exit is not a TP");

  // Dragging the position handle into profit previews a maker TP.
  const handle = lines.find(line => line.key === "position:PF_XBTUSD");
  const preview = previewProtection(handle, 110);
  assert.equal(preview.drop.kind, "tp");
  assert.match(preview.title, /\+\$19\.96 after maker fee/);
  const stop = previewProtection(handle, 95);
  assert.equal(stop.drop.kind, "sl");
  assert.match(stop.title, /-\$10\.10 after fee/, "a stop pays the taker fee");
});
