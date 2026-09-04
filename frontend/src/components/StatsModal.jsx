import { useEffect, useMemo, useRef, useState } from "react";
import * as LightweightCharts from "lightweight-charts";
import { api, fmt } from "../api";

/* Performance modal: realized-PnL equity curve + trade stats, built from the
 * Kraken fills feed (closing fills carry realized_pnl / realized_funding). */

const TIMEFRAMES = [["1d", "1D"], ["1w", "1W"], ["1mo", "1M"], ["all", "All"]]; // calendar: Mon-Sun week, month = 1st

export default function StatsModal({ onClose }) {
  const [rows, setRows] = useState(null);
  const [equity, setEquity] = useState(null);
  const [err, setErr] = useState("");
  const [tf, setTf] = useState("all");
  const boxRef = useRef(null);
  const eqRef = useRef(null);

  useEffect(() => {
    api("/api/stats").then(r => setRows(r.rows || [])).catch(e => setErr(e.message));
    api("/api/equity").then(r => setEquity(r.rows || [])).catch(() => {});
    const onKey = e => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const stats = useMemo(() => computeStats(rows || [], tf), [rows, tf]);

  useEffect(() => {
    if (!stats || !stats.curve.length || !boxRef.current) return;
    const chart = LightweightCharts.createChart(boxRef.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#787b86", fontSize: 11 },
      grid: { vertLines: { color: "rgba(42,50,66,.4)" }, horzLines: { color: "rgba(42,50,66,.4)" } },
      timeScale: { borderColor: "#1e2430" },
      rightPriceScale: { borderColor: "#1e2430" },
    });
    const series = chart.addSeries(LightweightCharts.AreaSeries, {
      lineColor: stats.totalPnl >= 0 ? "#26a69a" : "#ef5350",
      topColor: stats.totalPnl >= 0 ? "rgba(38,166,154,.25)" : "rgba(239,83,80,.25)",
      bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    series.setData(stats.curve.map(c => ({ time: c[0], value: c[1] })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [stats]);

  useEffect(() => {
    if (!equity || equity.length < 2 || !eqRef.current) return;
    const chart = LightweightCharts.createChart(eqRef.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#787b86", fontSize: 11 },
      grid: { vertLines: { color: "rgba(42,50,66,.4)" }, horzLines: { color: "rgba(42,50,66,.4)" } },
      timeScale: { borderColor: "#1e2430" },
      rightPriceScale: { borderColor: "#1e2430" },
    });
    const series = chart.addSeries(LightweightCharts.AreaSeries, {
      lineColor: "#4f8cff",
      topColor: "rgba(79,140,255,.22)",
      bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    series.setData(equity.map(r => ({ time: r.t, value: r.equity })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [equity]);

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="modal-head">
          <h3>Performance — realized PnL (Kraken account log)</h3>
          <button className="modal-x" title="Close (Esc)" onClick={onClose}>×</button>
        </div>
        <div className="tf-bar" style={{ padding: "0 0 8px" }}>
          {TIMEFRAMES.map(([id, label]) => (
            <button key={id} className={"tf-btn" + (tf === id ? " active" : "")} onClick={() => setTf(id)}>{label}</button>
          ))}
        </div>
        {stats.preWindow !== null && (
          <div style={{ fontSize: 11, color: "var(--muted)", margin: "-2px 0 8px" }}>
            Realized before this window: <b className={stats.preWindow >= 0 ? "up" : "down"}>{(stats.preWindow >= 0 ? "+" : "") + fmt(stats.preWindow, 2)}</b>
            {" "}— all-time = this window + that.
          </div>
        )}
        {err && <div className="empty">Account log unavailable: {err}</div>}
        {!rows && !err && <div className="empty">Loading account log…</div>}
        {rows && (
          <>
            <div className="stat-tiles">
              <Tile k="Realized PnL (net)" v={(stats.totalPnl >= 0 ? "+" : "") + fmt(stats.totalPnl, 2)} cls={stats.totalPnl >= 0 ? "up" : "down"} />
              <Tile k="Funding" v={(stats.funding >= 0 ? "+" : "") + fmt(stats.funding, 3)} cls={stats.funding >= 0 ? "up" : "down"} />
              <Tile k="Closing fills" v={String(stats.closes.length)} />
              <Tile k="Win rate" v={stats.closes.length ? (100 * stats.wins.length / stats.closes.length).toFixed(1) + "%" : "–"} />
              <Tile k="Profit factor" v={stats.pf !== null ? stats.pf.toFixed(2) : "–"} />
              <Tile k="Avg per closer" v={stats.closes.length ? (stats.totalPnl >= 0 ? "+" : "") + fmt(stats.totalPnl / stats.closes.length, 2) : "–"} cls={stats.totalPnl >= 0 ? "up" : "down"} />
              <Tile k="Best fill" v={(stats.best >= 0 ? "+" : "") + fmt(stats.best, 2)} cls="up" />
              <Tile k="Worst fill" v={(stats.worst >= 0 ? "+" : "") + fmt(stats.worst, 2)} cls="down" />
              <Tile k="Interest paid" v={"-" + fmt(stats.interest, 3)} cls="down" />
              <Tile k="Liquidations (auto)" v={(stats.liqPnl >= 0 ? "+" : "") + fmt(stats.liqPnl, 2)} cls={stats.liqPnl >= 0 ? "up" : "down"} />
              <Tile k="Manual trades" v={(stats.totalPnl - stats.liqPnl >= 0 ? "+" : "") + fmt(stats.totalPnl - stats.liqPnl, 2)} cls={stats.totalPnl - stats.liqPnl >= 0 ? "up" : "down"} />
            </div>
            {equity && equity.length >= 2 && (
              <>
                <div className="eq-label">Account equity — portfolio value, 30s snapshots (live, started {new Date(equity[0].t * 1000).toLocaleString()})</div>
                <div className="eq-chart small" ref={eqRef} />
              </>
            )}
            <div className="eq-label">Cumulative realized PnL (closers only)</div>
            <div className="eq-chart" ref={boxRef} />
            <table className="data">
              <thead><tr>
                <th>Symbol</th><th className="num">Closing fills</th><th className="num">Funding</th><th className="num">Realized PnL</th>
              </tr></thead>
              <tbody>
                {stats.perSym.map(r => (
                  <tr key={r.symbol}>
                    <td>{r.symbol}</td>
                    <td className="num">{r.n}</td>
                    <td className="num">{(r.fund >= 0 ? "+" : "") + fmt(r.fund, 4)}</td>
                    <td className={"num " + (r.pnl >= 0 ? "up" : "down")}>{(r.pnl >= 0 ? "+" : "") + fmt(r.pnl, 2)}</td>
                  </tr>
                ))}
                {!stats.perSym.length && <tr><td colSpan={4} className="empty">No closing fills in the window</td></tr>}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function Tile({ k, v, cls = "" }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={"v " + cls}>{v}</div>
    </div>
  );
}

function computeStats(allRows, tf) {
  // calendar windows in local time: 1D = since midnight, 1W = since Monday 00:00, 1M = since the 1st
  const now = new Date();
  const startOfDay = new Date(now); startOfDay.setHours(0, 0, 0, 0);
  const monday = new Date(now); monday.setDate(now.getDate() - ((now.getDay() + 6) % 7)); monday.setHours(0, 0, 0, 0);
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
  const cutoffs = { "1d": startOfDay.getTime() / 1000, "1w": monday.getTime() / 1000, "1mo": monthStart.getTime() / 1000, all: 0 };
  const cutoff = cutoffs[tf] ?? 0;
  const rows = allRows.filter(r => r.t >= cutoff).sort((a, b) => a.t - b.t);

  const tradeRows = rows.filter(r => (r.info === "futures trade" || r.info === "futures partial liquidation") && Number(r.pnl || 0) !== 0);
  let cum = 0, grossWin = 0, grossLoss = 0;
  const byTs = new Map();
  const perSymMap = new Map();
  let funding = 0, interest = 0;
  for (const r of rows) {
    if (r.info === "funding rate change") funding += Number(r.funding || 0);
    if (r.info === "interest payment") interest -= Number(r.fee || 0);
  }
  for (const r of tradeRows) {
    const pnl = Number(r.pnl || 0);
    cum += pnl;
    byTs.set(r.t, cum);
    const sym = r.contract || "?";
    const ps = perSymMap.get(sym) || { symbol: sym, pnl: 0, n: 0, fund: 0 };
    ps.pnl += pnl;
    ps.n += 1;
    perSymMap.set(sym, ps);
    if (pnl > 0) grossWin += pnl;
    else grossLoss += -pnl;
  }
  const fundingBySym = new Map();
  for (const r of rows) {
    if (r.info === "funding rate change" && r.contract) {
      fundingBySym.set(r.contract, (fundingBySym.get(r.contract) || 0) + Number(r.funding || 0));
    }
  }
  for (const ps of perSymMap.values()) ps.fund = fundingBySym.get(ps.symbol) || 0;

  const pnls = tradeRows.map(r => Number(r.pnl || 0));
  const totalPnl = pnls.reduce((a, b) => a + b, 0);
  const liqPnl = rows
    .filter(r => r.info === "futures partial liquidation")
    .reduce((a, r) => a + Number(r.pnl || 0), 0);
  const preWindow = cutoff ? allRows
    .filter(r => r.t < cutoff && (r.info === "futures trade" || r.info === "futures partial liquidation"))
    .reduce((a, r) => a + Number(r.pnl || 0), 0) : null;
  return {
    preWindow,
    liqPnl,
    curve: [...byTs.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => [t, v]),
    closes: tradeRows,
    wins: pnls.filter(p => p > 0),
    totalPnl,
    funding,
    interest,
    pf: grossLoss > 0 ? grossWin / grossLoss : (grossWin > 0 ? Infinity : null),
    best: pnls.length ? Math.max(...pnls) : 0,
    worst: pnls.length ? Math.min(...pnls) : 0,
    perSym: [...perSymMap.values()].sort((a, b) => b.pnl - a.pnl),
  };
}
