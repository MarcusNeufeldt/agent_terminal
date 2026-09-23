// What a market close would actually return right now: walk the live book on the
// side the position closes into, then subtract the taker fee. The positions table
// values everything at the best bid/ask, which only holds for the first level's size.

// Taker rates. Kraken: measured 5.0bp on 97% of this account's fills (2026-09).
// Hyperliquid: base tier 0 (4.5bp); staking or referral discounts are not applied.
export const TAKER_FEE = { kraken: 0.0005, hyperliquid: 0.00045 };
// The Hyperliquid market close is an IOC bounded 0.5% from the best price (store.closePosition).
export const HL_CLOSE_SLIPPAGE = 0.005;

function level(row) {
  const px = Number(Array.isArray(row) ? row[0] : row?.px ?? row?.price);
  const sz = Number(Array.isArray(row) ? row[1] : row?.sz ?? row?.size ?? row?.qty);
  return Number.isFinite(px) && px > 0 && Number.isFinite(sz) && sz > 0 ? { px, sz } : null;
}

// Best price first. Never trust the feed's order: Kraken returns bids worst-first.
export function sortedLevels(rows, side) {
  const levels = (Array.isArray(rows) ? rows : []).map(level).filter(Boolean);
  return levels.sort((a, b) => (side === "bids" ? b.px - a.px : a.px - b.px));
}

// Take `qty` from best-first levels, optionally stopping at a price bound.
export function walkBook(levels, qty, { limit = null, side = "bids" } = {}) {
  let left = qty, cost = 0, used = 0, worst = null, stoppedByLimit = false;
  for (const { px, sz } of levels) {
    if (left <= 1e-12) break;
    if (limit !== null && (side === "bids" ? px < limit : px > limit)) { stoppedByLimit = true; break; }
    const take = Math.min(left, sz);
    cost += take * px;
    left -= take;
    used += 1;
    worst = px;
  }
  const filled = qty - Math.max(0, left);
  return { filled, unfilled: Math.max(0, left), avgPrice: filled > 0 ? cost / filled : null, levelsUsed: used,
    worstPrice: worst, stoppedByLimit };
}

export function closePreview({ position, book, contractSize = 1, inverse = false, feeRate, slippageBound = null, funding = 0 }) {
  const side = String(position?.side).toLowerCase();
  const qty = Number(position?.size), entry = Number(position?.price), mult = Number(contractSize);
  if (inverse || !["long", "short"].includes(side) || ![qty, entry, mult, feeRate].every(v => Number.isFinite(v) && v >= 0)
      || !(qty > 0) || !(entry > 0) || !(mult > 0)) return null;
  const bookSide = side === "long" ? "bids" : "asks";
  const levels = sortedLevels(book?.[bookSide], bookSide);
  if (!levels.length) return null;
  const best = levels[0].px;
  const limit = slippageBound === null ? null : best * (bookSide === "bids" ? 1 - slippageBound : 1 + slippageBound);
  const walk = walkBook(levels, qty, { limit, side: bookSide });
  const dir = side === "long" ? 1 : -1;
  const pnlAtBest = dir * qty * mult * (best - entry);
  if (!(walk.filled > 0)) return { side, qty, entry, best, filled: 0, unfilled: qty, pnlAtBest, net: null, levels: levels.length };
  const pnlWalked = dir * walk.filled * mult * (walk.avgPrice - entry);
  const bookWalkCost = dir * walk.filled * mult * (best - walk.avgPrice);
  const exitFee = feeRate * walk.filled * mult * walk.avgPrice;
  const fund = Number.isFinite(Number(funding)) ? Number(funding) : 0;
  const net = pnlWalked - exitFee + fund;
  // Whole-position value for display: size beyond the visible book is priced at the
  // worst visible level, so the figure can be too kind only if the book is thinner
  // past the snapshot, never because size was left out.
  const rest = walk.unfilled;
  const netFull = rest > 1e-12 && walk.worstPrice
    ? net + dir * rest * mult * (walk.worstPrice - entry) - feeRate * rest * mult * walk.worstPrice
    : net;
  return {
    side, qty, entry, best, limit,
    avgPrice: walk.avgPrice, worstPrice: walk.worstPrice, levelsUsed: walk.levelsUsed, levels: levels.length,
    filled: walk.filled, unfilled: walk.unfilled,
    // Why part would not fill: the IOC price bound, or the end of the book snapshot.
    unfilledReason: walk.unfilled > 1e-12 ? (walk.stoppedByLimit ? "bound" : "depth") : null,
    pnlAtBest, pnlWalked, bookWalkCost, exitFee, funding: fund,
    net, netFull,
  };
}
