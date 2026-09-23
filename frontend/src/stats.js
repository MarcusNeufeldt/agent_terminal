// Performance figures from the Kraken account log (/api/stats rows:
// {t, info, contract, pnl, funding, fee, liqFee}).
//
// "Net" is what actually moved the wallet: price PnL + funding - trading fees -
// liquidation penalties - interest. Kraken books each of those in its own column,
// and a figure built from realized_pnl alone omits every cost. Checked against the
// live log: on every trade row the collateral balance change equals
// pnl + funding - fee - liqFee to the cent.

export const TRADE_INFOS = new Set(["futures trade", "futures partial liquidation"]);
const LIQUIDATION = "futures partial liquidation";
const FUNDING = "funding rate change";
const INTEREST = "interest payment";

const num = v => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

// Wallet effect of one ledger row. Transfers and currency conversions are not
// trading results, so they contribute nothing.
export function ledgerNet(row) {
  if (TRADE_INFOS.has(row?.info)) return num(row.pnl) + num(row.funding) - num(row.fee) - num(row.liqFee);
  if (row?.info === FUNDING) return num(row.funding);
  if (row?.info === INTEREST) return -num(row.fee);
  return 0;
}

// Calendar windows in local time: 1D = since midnight, 1W = since Monday 00:00,
// 1M = since the 1st.
export function windowStart(tf, now = new Date()) {
  const startOfDay = new Date(now); startOfDay.setHours(0, 0, 0, 0);
  const monday = new Date(now); monday.setDate(now.getDate() - ((now.getDay() + 6) % 7)); monday.setHours(0, 0, 0, 0);
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
  const cutoffs = { "1d": startOfDay.getTime() / 1000, "1w": monday.getTime() / 1000, "1mo": monthStart.getTime() / 1000, all: 0 };
  return cutoffs[tf] ?? 0;
}

export function computeStats(allRows, tf, now = new Date()) {
  const cutoff = windowStart(tf, now);
  const rows = (Array.isArray(allRows) ? allRows : []).filter(r => r.t >= cutoff).sort((a, b) => a.t - b.t);

  let pricePnl = 0, fees = 0, liqPenalty = 0, funding = 0, interest = 0, net = 0;
  let liqNet = 0;
  const byTs = new Map();
  const perSymMap = new Map();
  const sym = contract => {
    const key = contract || "?";
    if (!perSymMap.has(key)) perSymMap.set(key, { symbol: key, n: 0, pnl: 0, fees: 0, liq: 0, fund: 0, net: 0 });
    return perSymMap.get(key);
  };
  const closes = [];

  for (const r of rows) {
    const rowNet = ledgerNet(r);
    if (TRADE_INFOS.has(r.info)) {
      const pnl = num(r.pnl);
      pricePnl += pnl;
      fees += num(r.fee);
      liqPenalty += num(r.liqFee);
      funding += num(r.funding);
      if (r.info === LIQUIDATION) liqNet += rowNet;
      if (pnl !== 0) closes.push(pnl);
      const s = sym(r.contract);
      s.pnl += pnl;
      s.fees += num(r.fee);
      s.liq += num(r.liqFee);
      s.fund += num(r.funding);
      s.net += rowNet;
      if (pnl !== 0) s.n += 1;
    } else if (r.info === FUNDING) {
      funding += num(r.funding);
      if (r.contract) {
        const s = sym(r.contract);
        s.fund += num(r.funding);
        s.net += rowNet;
      }
    } else if (r.info === INTEREST) {
      interest += num(r.fee);
    }
    if (rowNet !== 0) {
      net += rowNet;
      byTs.set(r.t, net);
    }
  }

  const wins = closes.filter(p => p > 0);
  const grossWin = wins.reduce((a, b) => a + b, 0);
  const grossLoss = -closes.filter(p => p < 0).reduce((a, b) => a + b, 0);
  const preWindow = cutoff
    ? (Array.isArray(allRows) ? allRows : []).filter(r => r.t < cutoff).reduce((a, r) => a + ledgerNet(r), 0)
    : null;
  return {
    net,
    pricePnl,
    fees,
    liqPenalty,
    funding,
    interest,
    liqNet,
    preWindow,
    curve: [...byTs.entries()].sort((a, b) => a[0] - b[0]),
    closes,
    wins,
    pf: grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? Infinity : null),
    best: closes.reduce((a, b) => Math.max(a, b), closes.length ? -Infinity : 0),
    worst: closes.reduce((a, b) => Math.min(a, b), closes.length ? Infinity : 0),
    perSym: [...perSymMap.values()].filter(s => s.n || s.fees || s.pnl).sort((a, b) => b.net - a.net),
  };
}
