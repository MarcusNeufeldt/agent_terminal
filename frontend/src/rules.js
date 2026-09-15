// Trading-discipline rules, derived from 221 closed round-trips of this account's
// own history (see evidence/trading-rules-2026-09-15.md in the kraken-futures-cli
// repo). Display only: nothing here places, cancels or modifies an order.
//
// Thresholds, and why they are these numbers:
//   takeProfitPct   0.03  taking profit at 3% of equity per position scored best of
//                         every rule tested (+4,986 against actual). Applied PER
//                         POSITION, not to the whole book: the basket variant lost
//                         to it at every threshold.
//   trailGiveback   0.30  secondary signal once a position has already cleared the
//                         target and is handing profit back.
//   sizeCapX        3     10 trades above 3x equity notional caused half of all
//                         losses; the other 211 averaged -7.
//   cooldownLossPct 0.03  a realized loss this size preceded the worst re-entries;
//   cooldownMs      2h    median re-entry after a loss was 4 minutes vs 47 after a
//                         win, and a 2h pause was worth ~+3,000.
export const RULES = {
  takeProfitPct: 0.03,
  trailGiveback: 0.30,
  sizeCapX: 3,
  cooldownLossPct: 0.03,
  cooldownMs: 2 * 60 * 60 * 1000,
  basketWarnPct: 0.10,
};

const EQUITY_KEYS = ["portfolioValue", "collateralValue", "balanceValue"];

// Equity backs every threshold, so an unusable account payload must yield null
// rather than a silent zero that would make each rule look satisfied.
export function accountEquity(account) {
  for (const key of EQUITY_KEYS) {
    const value = Number(account?.[key]);
    if (Number.isFinite(value) && value > 0) return value;
  }
  return null;
}

// PF_* (flexible): notional = size x contractSize x price.
// PI_* (inverse): 1 contract is contractSize USD, so notional is price-independent.
export function positionNotional(position, instrument, last) {
  if (!position || position.error || !instrument) return null;
  const size = Number(position.size);
  const mult = Number(instrument.contractSize ?? 1);
  if (![size, mult].every(value => Number.isFinite(value) && value > 0)) return null;
  if (instrument.type === "futures_inverse") return size * mult;
  const price = Number(last);
  if (!Number.isFinite(price) || price <= 0) return null;
  return size * mult * price;
}

// Adding to a position moves its average entry, which restarts the trail. Keying on
// entry price means a scaled-in position cannot keep a peak it never reached at its
// new basis.
export function peakKey(position) {
  if (!position?.symbol || position.error) return null;
  if (!["long", "short"].includes(String(position.side).toLowerCase())) return null;
  return `${position.symbol}|${String(position.side).toLowerCase()}|${position.price}`;
}

// Returns a fresh map so keys for closed positions are dropped rather than kept
// forever. A position with no readable uPnL keeps its previous peak.
export function nextPeaks(previous, entries) {
  const next = {};
  for (const entry of entries || []) {
    const key = entry?.key;
    if (!key) continue;
    const prior = Number(previous?.[key]);
    const upnl = Number(entry.upnl);
    const candidates = [Number.isFinite(prior) ? prior : 0];
    if (Number.isFinite(upnl)) candidates.push(upnl);
    next[key] = Math.max(...candidates);
  }
  return next;
}

export function evaluateTakeProfit({ upnl, equity, peak, config = RULES }) {
  if (!Number.isFinite(upnl) || !Number.isFinite(equity) || equity <= 0) return null;
  const target = equity * config.takeProfitPct;
  const peakValue = Number.isFinite(peak) ? Math.max(peak, upnl) : upnl;
  const armed = peakValue >= target && target > 0;
  const giveback = armed && peakValue > 0 ? (peakValue - upnl) / peakValue : null;
  let state = "hold";
  if (upnl >= target && target > 0) state = "take";
  else if (armed && giveback !== null && giveback >= config.trailGiveback) state = "trail";
  return {
    target,
    upnl,
    pct: upnl / equity,
    progress: target > 0 ? upnl / target : null,
    peak: peakValue,
    armed,
    giveback,
    state,
  };
}

export function evaluateSize({ notional, equity, config = RULES }) {
  if (!Number.isFinite(notional) || !Number.isFinite(equity) || equity <= 0) return null;
  const multiple = notional / equity;
  return { notional, multiple, cap: config.sizeCapX, breach: multiple > config.sizeCapX };
}

// /api/stats rows are ledger lines: {t, info, contract, pnl, funding, fee}. One
// close produces several, so lines for the same contract inside `windowSeconds` are
// summed into a single realized event. `fee` is a positive cost (verified against
// the executions endpoint), and contracts arrive lowercase.
// `sinceMs` bounds the scan: this ledger runs to tens of thousands of rows, and only
// the last couple of hours can ever matter to the cooldown.
export function realizedEvents(rows, { windowSeconds = 60, sinceMs = null } = {}) {
  if (!Array.isArray(rows) || windowSeconds <= 0) return [];
  const sinceSeconds = Number.isFinite(sinceMs) ? sinceMs / 1000 : null;
  const buckets = new Map();
  for (const row of rows) {
    const t = Number(row?.t);
    if (!Number.isFinite(t)) continue;
    if (sinceSeconds !== null && t < sinceSeconds) continue;
    const contract = String(row?.contract ?? "").toUpperCase();
    if (!contract || contract === "NULL") continue;
    const pnl = Number(row?.pnl);
    const funding = Number(row?.funding);
    const fee = Number(row?.fee);
    const parts = [pnl, funding, fee].map(value => (Number.isFinite(value) ? value : 0));
    if (!Number.isFinite(pnl) && !Number.isFinite(funding)) continue;
    const net = parts[0] + parts[1] - parts[2];
    if (!Number.isFinite(net)) continue;
    const key = `${contract}|${Math.floor(t / windowSeconds)}`;
    const existing = buckets.get(key);
    if (existing) {
      existing.net += net;
      existing.t = Math.max(existing.t, t);
    } else {
      buckets.set(key, { t, contract, net });
    }
  }
  return [...buckets.values()].sort((a, b) => a.t - b.t);
}

// `now` and event timestamps are both epoch milliseconds / seconds respectively;
// events carry seconds because that is what the ledger reports.
export function evaluateCooldown({ events, equity, now, config = RULES }) {
  if (!Number.isFinite(equity) || equity <= 0 || !Number.isFinite(now)) return null;
  const limit = -equity * config.cooldownLossPct;
  let trigger = null;
  for (const event of events || []) {
    const at = Number(event?.t) * 1000;
    if (!Number.isFinite(at) || at > now) continue;
    if (event.net <= limit && (!trigger || at > trigger.at)) trigger = { ...event, at };
  }
  if (!trigger) return { active: false, trigger: null, remainingMs: 0, limit };
  const remainingMs = trigger.at + config.cooldownMs - now;
  return {
    active: remainingMs > 0,
    trigger,
    remainingMs: Math.max(0, remainingMs),
    limit,
  };
}

export function formatCountdown(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "0m";
  const minutes = Math.ceil(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

// Single entry point for the panel. Never invents numbers: any input it cannot read
// yields null for that row so the UI can say so instead of showing a confident zero.
export function evaluateRules({
  positions = [],
  account = {},
  instruments = [],
  tickers = {},
  peaks = {},
  realized = [],
  upnlFor,
  now = Date.now(),
  config = RULES,
} = {}) {
  const equity = accountEquity(account);
  const live = positions.filter(p => p && !p.error && Number(p.size) > 0);
  // This runs on every ticker tick, so the instrument lookup is indexed once rather
  // than scanned per position.
  const bySymbol = new Map(instruments.map(i => [i?.symbol, i]));

  const rows = live.map(position => {
    const instrument = bySymbol.get(position.symbol) || null;
    const last = Number(tickers?.[position.symbol]?.last);
    const upnl = typeof upnlFor === "function" ? upnlFor(position) : null;
    const key = peakKey(position);
    const notional = positionNotional(position, instrument, last);
    return {
      symbol: position.symbol,
      side: position.side,
      key,
      upnl: Number.isFinite(upnl) ? upnl : null,
      takeProfit: evaluateTakeProfit({
        upnl, equity, peak: key ? peaks?.[key] : undefined, config,
      }),
      size: evaluateSize({ notional, equity, config }),
    };
  });

  const upnls = rows.map(row => row.upnl);
  const notionals = rows.map(row => row.size?.notional);
  const basketUpnl = upnls.every(Number.isFinite)
    ? upnls.reduce((sum, value) => sum + value, 0) : null;
  const basketNotional = notionals.every(Number.isFinite)
    ? notionals.reduce((sum, value) => sum + value, 0) : null;

  return {
    equity,
    rows,
    cooldown: evaluateCooldown({ events: realized, equity, now, config }),
    // Warning only. The basket take-profit variant was tested and lost to the
    // per-position rule at every threshold, so this never produces a "close" signal.
    basket: {
      upnl: basketUpnl,
      pct: basketUpnl !== null && equity ? basketUpnl / equity : null,
      notional: basketNotional,
      multiple: basketNotional !== null && equity ? basketNotional / equity : null,
      warn: basketUpnl !== null && equity
        ? basketUpnl <= -equity * config.basketWarnPct : false,
      positions: rows.length,
    },
  };
}
