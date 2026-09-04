import test from "node:test";
import assert from "node:assert/strict";
import { buildChartOverlays } from "./chart-overlays.js";

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

test("keeps symbol overlays isolated", () => {
  assert.deepEqual(buildChartOverlays("PF_ETHUSD", positions, [], instruments), []);
});
