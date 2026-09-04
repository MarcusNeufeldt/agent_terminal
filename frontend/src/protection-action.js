export function buildProtectionAction(kind, symbol, stopPrice, order = null) {
  const action = {
    type: kind === "sl" ? "replace_sl" : "replace_tp",
    symbol,
    stopPrice,
  };
  if (order?.orderId) {
    action.orderId = order.orderId;
    action.preserveSize = true;
  } else if (order?.cliOrdId) {
    action.cliOrdId = order.cliOrdId;
    action.preserveSize = true;
  }
  return action;
}
