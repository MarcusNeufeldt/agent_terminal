import test from "node:test";
import assert from "node:assert/strict";
import { buildProtectionAction } from "./protection-action.js";

test("TP drag carries the exact exchange order ID and preserves covered size", () => {
  assert.deepEqual(
    buildProtectionAction("tp", "PF_TESTUSD", 12.5, { orderId: "order-1", cliOrdId: "client-1" }),
    { type: "replace_tp", symbol: "PF_TESTUSD", stopPrice: 12.5, orderId: "order-1", preserveSize: true },
  );
});

test("position-handle protection creates or replaces an unambiguous full-position order", () => {
  assert.deepEqual(
    buildProtectionAction("sl", "PF_TESTUSD", 8.5),
    { type: "replace_sl", symbol: "PF_TESTUSD", stopPrice: 8.5 },
  );
});
