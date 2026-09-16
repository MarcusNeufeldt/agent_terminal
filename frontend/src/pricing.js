/* Which price values an open position.
 *
 * Three candidates, each with a real failure mode on Kraken Futures:
 *
 *   last  - a trade that already happened. On thin pairs the tape goes quiet and
 *           last drifts outside the live book, so PnL shows movement that is not
 *           there. Measured across 13 subscribed pairs, last sat outside the
 *           bid/ask on 7 of them, a mean 0.18% from mid and up to 1.5%.
 *   mark  - Kraken's index-plus-basis valuation. Tracks the book closely in calm
 *           conditions (mean 0.009% from mid) but is not a price you can trade,
 *           and can dislocate on a thin pair during a fast move, which shows a
 *           freshly opened position as instantly deep in the red.
 *   book  - bid/ask. Updates continuously even when nothing trades, and is the
 *           only candidate that is an actually tradeable price.
 *
 * So the book wins, in two flavours:
 *   mid  - (bid+ask)/2, for display. No phantom loss the moment you open.
 *   exit - bid for a long, ask for a short: what you would really get closing now.
 *          Used to evaluate rules, so a take-profit only fires on profit you
 *          could actually bank.
 *
 * Every function returns null rather than a fallback guess when inputs are
 * unusable, so callers can say "unavailable" instead of showing a confident zero.
 */

const positive = value => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
};

export function midPrice(ticker) {
  const bid = positive(ticker?.bid);
  const ask = positive(ticker?.ask);
  if (bid === null || ask === null) return null;
  return (bid + ask) / 2;
}

export function exitPrice(ticker, side) {
  const normalized = String(side).toLowerCase();
  if (normalized === "long") return positive(ticker?.bid);
  if (normalized === "short") return positive(ticker?.ask);
  return null;
}

// True when the tape has gone quiet and the last trade no longer sits inside the
// live book. Only meaningful with a two-sided book.
export function isLastStale(ticker) {
  const bid = positive(ticker?.bid);
  const ask = positive(ticker?.ask);
  const last = positive(ticker?.last);
  if (bid === null || ask === null || last === null) return false;
  return last < Math.min(bid, ask) || last > Math.max(bid, ask);
}

// A trade this old is no longer evidence of the current price. Only Hyperliquid
// tickers carry a trade timestamp today, so where none is present a last trade is
// taken at face value and Kraken behaviour is unchanged.
export const STALE_LAST_MS = 60000;

export function isLastQuiet(ticker, now = Date.now()) {
  const at = Number(ticker?.lastTime);
  return Number.isFinite(at) && at > 0 && now - at > STALE_LAST_MS;
}

/* Resolve the price to value a position at.
 *
 * mode "mid"  -> book mid, then last, then mark
 * mode "exit" -> exit side of the book, then mid, then last, then mark
 *
 * Mark is never allowed to displace a live book, and never a recent trade: it is
 * not tradeable and can dislocate on a thin pair, showing a freshly opened
 * position as instantly deep in the red. It is reached only when there is no book
 * and the tape has gone quiet past STALE_LAST_MS.
 *
 * That case is common on Hyperliquid, where a ticker only carries bid/ask once its
 * book has been fetched — so a position in a symbol you are not looking at has no
 * book, and its last trade can be minutes old on a thin coin. markPx is what
 * Hyperliquid's own interface shows, so the two agree instead of drifting apart.
 *
 * `basis` reports which one was actually used so the UI can label it honestly
 * rather than implying a book price it did not have.
 */
export function valuationPrice(ticker, { mode = "mid", side, now = Date.now() } = {}) {
  if (mode === "exit") {
    const exit = exitPrice(ticker, side);
    if (exit !== null) return { price: exit, basis: "exit" };
  }
  const mid = midPrice(ticker);
  if (mid !== null) return { price: mid, basis: "mid" };
  const last = positive(ticker?.last);
  if (last !== null && !isLastQuiet(ticker, now)) return { price: last, basis: "last" };
  const mark = positive(ticker?.markPrice);
  if (mark !== null) return { price: mark, basis: "mark" };
  if (last !== null) return { price: last, basis: "last" };
  return { price: null, basis: "none" };
}
