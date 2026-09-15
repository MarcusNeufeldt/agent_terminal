const saved = globalThis.localStorage?.getItem("kt.exchange");
// Fixed for this page lifetime. Switching reloads rather than reusing drafts,
// asynchronous reads, chart providers, or exact order IDs from the other venue.
export const EXCHANGE = saved === "hyperliquid" ? "hyperliquid" : "kraken";
export const EXCHANGE_NAME = EXCHANGE === "kraken" ? "Kraken Futures" : "Hyperliquid";
export const READ_ONLY = EXCHANGE === "hyperliquid";
export const venueKey = key => EXCHANGE === "kraken" ? key : `${key}.hyperliquid`;
export const isVenueSymbol = symbol => typeof symbol === "string" && symbol.startsWith(READ_ONLY ? "HL_" : "PF_");

export function reloadExchange(exchange) {
  if (!["kraken", "hyperliquid"].includes(exchange)) throw new Error("Unknown exchange");
  globalThis.localStorage?.setItem("kt.exchange", exchange);
  globalThis.location?.reload();
}
