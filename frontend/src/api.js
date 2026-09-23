import { EXCHANGE, READ_ONLY, reloadExchange } from "./exchange.js";

// Hyperliquid signed trading is gated by the backend. Until the gate is on, this venue
// accepts reads only, so a stale page cannot send a write the server would honour.
const HL_WRITE_PATHS = new Set(["/api/order", "/api/cancel", "/api/leverage", "/api/chart-order", "/api/grid", "/api/chase"]);
// Venue-neutral writes: switching venue, and the ARM gate itself. Disarming must always
// be possible, including when signed trading is off, or the terminal could be stuck live.
const VENUE_NEUTRAL_WRITES = new Set(["/api/exchange", "/api/arm"]);
// Stopping a Chase only cancels, so it must work whatever the trading gate says.
const HL_NONTRADING_POSTS = new Set(["/api/order-reconcile", "/api/cancel-reconcile", "/api/fill-history/sync", "/api/grid/preview", "/api/chase/abort"]);
let signedTrading = "off";

export function setSignedTrading(mode) {
  signedTrading = ["mainnet", "testnet"].includes(mode) ? mode : "off";
  return signedTrading;
}

let exchangeEpoch = 0;
let exchangeRouting = 0;
let sessionPromise = null;

async function getTerminalToken() {
  sessionPromise ||= fetch("/api/session", { cache: "no-store" }).then(async res => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.token) throw new Error(data.error || "Could not start terminal session");
    if (READ_ONLY && data.exchangeRouting !== 1) throw new Error("Restart the updated backend before using Hyperliquid.");
    exchangeRouting = data.exchangeRouting === 1 ? 1 : 0;
    if (exchangeRouting) {
      if (!Number.isSafeInteger(data.exchangeEpoch) || data.exchangeEpoch < 0) throw new Error("Invalid exchange session");
      exchangeEpoch = data.exchangeEpoch;
      checkExchangeSession(data);
    }
    return data.token;
  }).catch(error => { sessionPromise = null; throw error; });
  return sessionPromise;
}

export function checkExchangeSession(data) {
  if (data.exchangeRouting !== 1) return;
  if (data.exchange !== EXCHANGE || data.exchangeEpoch !== exchangeEpoch) {
    reloadExchange(data.exchange);
    throw new Error("Exchange changed. Reloading the terminal; no request was retried.");
  }
}

export function apiUrl(path) {
  if (!path.startsWith("/api/")) throw new Error("Expected a local terminal API path");
  const url = new URL(path, "http://terminal.local");
  const targets = url.searchParams.getAll("exchange");
  if (targets.length > 1 || targets.length && targets[0] !== EXCHANGE) throw new Error("Cross-exchange request blocked");
  if (!exchangeRouting && EXCHANGE === "kraken") return path; // Existing Kraken backend during rollout.
  url.searchParams.set("exchange", EXCHANGE);
  return url.pathname + url.search;
}

export function newRequestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  if (!["GET", "HEAD"].includes(method) && !VENUE_NEUTRAL_WRITES.has(path) && READ_ONLY
      && !HL_NONTRADING_POSTS.has(path) && !(signedTrading !== "off" && HL_WRITE_PATHS.has(path))) {
    throw new Error("Hyperliquid trading is disabled by the backend gate.");
  }
  const token = await getTerminalToken();
  const { body, headers, ...rest } = opts;
  const res = await fetch(apiUrl(path), {
    ...rest,
    headers: { "Content-Type": "application/json", ...headers, "X-Terminal-Token": token,
      ...(exchangeRouting ? { "X-Terminal-Exchange-Epoch": String(exchangeEpoch) } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || path === "/api/health") checkExchangeSession(data);
  if (!res.ok) {
    const error = new Error(data.error || `HTTP ${res.status}`);
    error.data = data;
    error.status = res.status;
    throw error;
  }
  return data;
}

export function fmt(x, digits) {
  if (x === null || x === undefined || Number.isNaN(Number(x))) return "–";
  const n = Number(x);
  if (digits !== undefined) return n.toFixed(digits);
  if (Math.abs(n) >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (Math.abs(n) >= 1) return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

export function fmtVol(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "–";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toFixed(1);
}

export function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export const RES_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400, "1w": 604800 };
