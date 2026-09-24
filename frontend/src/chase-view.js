/* Chase state for the ticket status card and the chart, for both venues. */

const LIVE = ["running", "unknown", "orphaned"];
// Kraken Chase client ids start "ch-"; Hyperliquid cloids start with hex "chas".
const CHASE_ID_PREFIXES = ["ch-", "0x63686173"];

export function chaseVenue(chase) {
  return chase?.exchange === "hyperliquid" ? "hyperliquid" : "kraken";
}

// Live Chases on this venue first, then (when asked) the most recent finished one for
// keepFinishedMs. `updated` is when the browser heard of it, which a reload resets, so
// a finished Chase must also have started recently enough to have just ended: a Chase
// never outlives its timeout by more than the market finish.
export function chaseCards(chases, exchange, now = Date.now(), { showFinished = true, keepFinishedMs = 60000 } = {}) {
  const list = Object.values(chases || {}).filter(c => c && chaseVenue(c) === exchange);
  const live = list.filter(c => LIVE.includes(c.status)).sort((a, b) => (b.started || 0) - (a.started || 0));
  if (!showFinished) return live;
  const recent = c => {
    const started = Number(c.started) || 0;
    const timeout = Number(c.spec?.timeoutSec) || 300;
    return started > 0 && now / 1000 - started < timeout + 30 + keepFinishedMs / 1000;
  };
  const finished = list.filter(c => !LIVE.includes(c.status) && now - (c.updated || 0) < keepFinishedMs && recent(c))
    .sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 1);
  return [...live, ...finished];
}

export function chaseProgress(chase, nowSec = Date.now() / 1000) {
  const size = Number(chase?.size) || 0;
  const filled = Number(chase?.filled) || 0;
  const timeout = Number(chase?.spec?.timeoutSec) || 0;
  const started = Number(chase?.started) || 0;
  const elapsed = started > 0 ? Math.max(0, nowSec - started) : null;
  return {
    filledPct: size > 0 ? Math.min(100, (filled / size) * 100) : 0,
    remaining: Math.max(0, size - filled),
    elapsed,
    left: elapsed !== null && timeout > 0 ? Math.max(0, timeout - elapsed) : null,
  };
}

export function isChaseOrder(order) {
  const id = String(order?.cliOrdId || "").toLowerCase();
  return CHASE_ID_PREFIXES.some(prefix => id.startsWith(prefix));
}

// One line per running Chase at its resting peg. The polled order list lags a re-peg
// by up to five seconds, so the Chase's own order is drawn from here instead.
export function chaseOverlays(chases, symbol, exchange) {
  return Object.values(chases || {})
    .filter(c => c && c.status === "running" && c.symbol === symbol && chaseVenue(c) === exchange &&
      Number(c.activePrice) > 0)
    .map(c => ({
      key: `chase:${c.id}`,
      price: Number(c.activePrice),
      color: "#b388ff",
      title: `CHASE ${c.side} ${Number(c.activeSize ?? c.size)} · peg ${c.pegs ?? 0}`,
      dashed: false,
    }));
}
