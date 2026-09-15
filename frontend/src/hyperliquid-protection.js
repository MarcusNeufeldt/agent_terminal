import { compareContractSizes } from "./size-precision.js";

// Observes one full-size stop, not combined ladder coverage or guaranteed execution.
export function hyperliquidProtection(position, orders, positionState, orderState) {
  const unknown = { state: "unknown", label: "Stop coverage unknown", detail: "Current, valid position and order snapshots are required." };
  if (positionState !== "current" || orderState !== "current" || !position || position.error ||
      typeof position.symbol !== "string" || !position.symbol.startsWith("HL_") || !["long", "short"].includes(position.side) ||
      compareContractSizes(position.sizeExact, position.sizeExact) === null || !Array.isArray(orders)) return unknown;
  const side = position.side === "long" ? "sell" : "buy";
  const seen = new Set();
  let stops = 0, full = false;
  for (const order of orders) {
    if (!order || order.error || typeof order.symbol !== "string" || !order.symbol.startsWith("HL_") ||
        (order.exchange && order.exchange !== "hyperliquid") || typeof order.order_id !== "string" ||
        !/^\d{1,20}$/.test(order.order_id)) return unknown;
    const id = BigInt(order.order_id).toString();
    if (BigInt(id) > 18446744073709551615n || seen.has(id)) return unknown;
    seen.add(id);
    if (order.symbol !== position.symbol) continue;
    if (!["buy", "sell"].includes(order.side) || typeof order.reduceOnly !== "boolean" || typeof order.orderType !== "string") return unknown;
    if (order.side !== side || !order.reduceOnly || order.orderType !== "stp") continue;
    const coverage = order.positionTpsl === true ? 0 : compareContractSizes(order.unfilledSizeExact, position.sizeExact);
    if (coverage === null || !Number.isFinite(order.stopPrice) || order.stopPrice <= 0) return unknown;
    stops++;
    full ||= coverage >= 0;
  }
  if (full) return { state: "observed", label: "Full-size stop observed", detail: "A reduce-only stop covers the current position. Native entire-position stops follow future size changes; fixed-size stops do not. Trigger-limit orders may remain unfilled; execution is not guaranteed." };
  if (stops) return { state: "smaller", label: "Smaller stops only", detail: "No single stop covers the full position. Combined ladder coverage is not assessed. No orders were changed." };
  return { state: "missing", label: "NO STOP OBSERVED", detail: "No opposite-side reduce-only stop was found in the current order snapshot. Other risk controls are not assessed. No orders were changed." };
}
