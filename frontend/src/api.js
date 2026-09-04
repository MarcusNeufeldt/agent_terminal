let terminalToken = typeof document === "undefined"
  ? ""
  : document.querySelector('meta[name="terminal-token"]')?.content || "";
if (terminalToken.includes("__TERMINAL_TOKEN__")) terminalToken = "";

async function getTerminalToken() {
  if (terminalToken) return terminalToken;
  const res = await fetch("/api/session", { cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.token) throw new Error(data.error || "Could not start terminal session");
  terminalToken = data.token;
  return terminalToken;
}

export async function api(path, opts = {}) {
  const token = await getTerminalToken();
  const { body, headers, ...rest } = opts;
  const res = await fetch(path, {
    ...rest,
    headers: { "Content-Type": "application/json", "X-Terminal-Token": token, ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
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
