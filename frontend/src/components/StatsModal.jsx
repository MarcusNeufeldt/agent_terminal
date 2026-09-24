import { useEffect, useMemo, useRef, useState } from "react";
import * as LightweightCharts from "lightweight-charts";
import { api, fmt } from "../api";
import { computeStats } from "../stats";

/* Performance modal: net trading result from the Kraken account log. Net is the
 * wallet change: price PnL + funding - fees - liquidation penalties - interest. */

const TIMEFRAMES = [["1d", "1D"], ["1w", "1W"], ["1mo", "1M"], ["all", "All"]]; // calendar: Mon-Sun week, month = 1st
const signed = (v, d = 2) => (v >= 0 ? "+" : "-") + fmt(Math.abs(v), d);
const cost = (v, d = 2) => (v > 0 ? "-" : "") + fmt(Math.abs(v), d);

export default function StatsModal({ onClose }) {
  const [rows, setRows] = useState(null);
  const [equity, setEquity] = useState(null);
  const [err, setErr] = useState("");
  const [tf, setTf] = useState("all");
  const boxRef = useRef(null);
  const eqRef = useRef(null);

  useEffect(() => {
    // A failed refresh arrives as {error, rows}: rows are the saved log (maybe empty).
    // Never show an empty result as a real zero.
    api("/api/stats").then(r => {
      setRows(Array.isArray(r.rows) ? r.rows : []);
      if (r.error) setErr(r.error);
    }).catch(e => setErr(e.message));
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
      lineColor: stats.net >= 0 ? "#26a69a" : "#ef5350",
      topColor: stats.net >= 0 ? "rgba(38,166,154,.25)" : "rgba(239,83,80,.25)",
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
          <h3>Performance — net after fees (Kraken account log)</h3>
          <button className="modal-x" title="Close (Esc)" onClick={onClose}>×</button>
        </div>
        <div className="tf-bar" style={{ padding: "0 0 8px" }}>
          {TIMEFRAMES.map(([id, label]) => (
            <button key={id} className={"tf-btn" + (tf === id ? " active" : "")} onClick={() => setTf(id)}>{label}</button>
          ))}
        </div>
        {stats.preWindow !== null && (
          <div style={{ fontSize: 11, color: "var(--muted)", margin: "-2px 0 8px" }}>
            Net before this window: <b className={stats.preWindow >= 0 ? "up" : "down"}>{signed(stats.preWindow)}</b>
            {" "}— all-time = this window + that.
          </div>
        )}
        {err && (rows && rows.length
          ? <div className="cp-warn">Showing the saved account log; the refresh from Kraken failed ({err}). The latest trades may be missing.</div>
          : <div className="empty">Account log unavailable: {err}</div>)}
        {!rows && !err && <div className="empty">Loading account log…</div>}
        {rows && (rows.length > 0 || !err) && (
          <>
            <div className="stat-tiles">
              <Tile k="Net after all costs" v={signed(stats.net)} cls={stats.net >= 0 ? "up" : "down"}
                title="Wallet change: price PnL + funding - trading fees - liquidation penalties - interest" />
              <Tile k="Price PnL (before costs)" v={signed(stats.pricePnl)} cls={stats.pricePnl >= 0 ? "up" : "down"} />
              <Tile k="Trading fees" v={cost(stats.fees)} cls={stats.fees > 0 ? "down" : ""} />
              <Tile k="Liquidation penalties" v={cost(stats.liqPenalty)} cls={stats.liqPenalty > 0 ? "down" : ""} />
              <Tile k="Funding" v={signed(stats.funding)} cls={stats.funding >= 0 ? "up" : "down"} />
              <Tile k="Interest" v={cost(stats.interest)} cls={stats.interest > 0 ? "down" : ""} />
              <Tile k="Liquidations, all-in" v={signed(stats.liqNet)} cls={stats.liqNet >= 0 ? "up" : "down"}
                title="Price loss + penalty + fees on liquidation fills" />
              <Tile k="Manual trading, net" v={signed(stats.net - stats.liqNet)} cls={stats.net - stats.liqNet >= 0 ? "up" : "down"}
                title="Net minus liquidations: your own entries and exits, after fees and funding" />
              <Tile k="Closing fills" v={String(stats.closes.length)} />
              <Tile k="Win rate (fills, before fees)" v={stats.closes.length ? (100 * stats.wins.length / stats.closes.length).toFixed(1) + "%" : "–"} />
              <Tile k="Profit factor (before fees)" v={stats.pf !== null ? stats.pf.toFixed(2) : "–"} />
              <Tile k="Best / worst fill" v={signed(stats.best) + " / " + signed(stats.worst)} />
            </div>
            {equity && equity.length >= 2 && (
              <>
                <div className="eq-label">Account equity — portfolio value, 30s snapshots (live, started {new Date(equity[0].t * 1000).toLocaleString()})</div>
                <div className="eq-chart small" ref={eqRef} />
              </>
            )}
            <div className="eq-label">Cumulative net after all costs</div>
            <div className="eq-chart" ref={boxRef} />
            <table className="data">
              <thead><tr>
                <th>Symbol</th><th className="num">Closing fills</th><th className="num">Price PnL</th><th className="num">Fees</th>
                <th className="num">Liq. penalty</th><th className="num">Funding</th><th className="num">Net</th>
              </tr></thead>
              <tbody>
                {stats.perSym.map(r => (
                  <tr key={r.symbol}>
                    <td>{r.symbol}</td>
                    <td className="num">{r.n}</td>
                    <td className={"num " + (r.pnl >= 0 ? "up" : "down")}>{signed(r.pnl)}</td>
                    <td className="num">{cost(r.fees)}</td>
                    <td className="num">{r.liq ? cost(r.liq) : "–"}</td>
                    <td className="num">{signed(r.fund)}</td>
                    <td className={"num " + (r.net >= 0 ? "up" : "down")}>{signed(r.net)}</td>
                  </tr>
                ))}
                {!stats.perSym.length && <tr><td colSpan={7} className="empty">No trades in the window</td></tr>}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function Tile({ k, v, cls = "", title }) {
  return (
    <div className="tile" title={title}>
      <div className="k">{k}</div>
      <div className={"v " + cls}>{v}</div>
    </div>
  );
}
