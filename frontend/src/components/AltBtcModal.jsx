import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmtVol } from "../api";
import useStore from "../store";

import { selectAltBtcRows } from "../alt-btc";

const pct = value => `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
const tone = value => value > 0 ? "up" : value < 0 ? "down" : "muted";

export default function AltBtcModal({ onClose }) {
  const dialog = useRef(null);
  const [snapshot, setData] = useState(null);
  const [period, setPeriod] = useState("24h");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("best");
  const refresh = useCallback(() => { setLoading(true); setError(""); setReload(n => n + 1); }, []);

  useEffect(() => {
    const node = dialog.current;
    const previous = document.activeElement;
    node.showModal();
    node.querySelector("input")?.focus();
    return () => {
      node.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    api(`/api/alt-btc?window=${period}`, { signal: controller.signal }).then(result => {
      if (result.state !== "current" || !Array.isArray(result.rows) || result.window !== period) {
        throw new Error("Selected time-frame data unavailable; the backend may need a restart.");
      }
      if (!controller.signal.aborted) setData(result);
    }).catch(err => {
      if (!controller.signal.aborted) { setError(err.message); setData(null); }
    }).finally(() => {
      if (!controller.signal.aborted) {
        setLoading(false);
        timer = setTimeout(refresh, 60_000);
      }
    });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [reload, period, refresh]);

  const data = snapshot?.window === period ? snapshot : null;
  const rows = selectAltBtcRows(data?.rows || [], query, filter, sort);
  const outperforming = data?.rows.filter(row => row.changeVsBtcPct > 0).length || 0;
  const underperforming = data?.rows.filter(row => row.changeVsBtcPct < 0).length || 0;

  function changePeriod(next) {
    if (next === period) return;
    setPeriod(next);
    setData(null);
    setError("");
    setLoading(true);
  }

  function openChart(symbol) {
    useStore.getState().selectSymbol(symbol);
    onClose();
  }

  return (
    <dialog ref={dialog} className="modal altbtc-modal" aria-labelledby="altbtc-title"
      onCancel={e => { e.preventDefault(); onClose(); }}
      onClick={e => {
        if (e.target !== e.currentTarget) return;
        const box = e.currentTarget.getBoundingClientRect();
        if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) onClose();
      }}>
      <div className="modal-head">
        <h3 id="altbtc-title">Altcoins vs Bitcoin</h3>
        <button className="modal-x" aria-label="Close Alt/BTC modal" title="Close (Esc)" onClick={onClose}>×</button>
      </div>
      <div className="tf-bar" role="group" aria-label="Comparison time frame">
        {["1h", "6h", "24h"].map(value => <button key={value} className={`tf-btn${period === value ? " active" : ""}`}
          aria-pressed={period === value} onClick={() => changePeriod(value)}>{value.toUpperCase()}</button>)}
      </div>
      <p className="modal-help">{period === "24h" ? "Rolling 24h" : `Last ${period}, ending at the latest completed five-minute candle`} relative performance from Binance USDT-M. Only tradable Kraken Futures altcoins.</p>
      {data && <div className="altbtc-summary">
        <div className="tile"><div className="k">Bitcoin / USDT · {period}</div><div className={`v ${tone(data.btcChangePct)}`}>{pct(data.btcChangePct)}</div></div>
        <div className="tile"><div className="k">Outperforming BTC</div><div className="v up">{outperforming}</div></div>
        <div className="tile"><div className="k">Underperforming BTC</div><div className="v down">{underperforming}</div></div>
      </div>}
      <div className="altbtc-controls">
        <input aria-label="Search altcoins" placeholder="Search coin or Kraken pair…" value={query} onChange={e => setQuery(e.target.value)} />
        <select aria-label="Relative performance filter" value={filter} onChange={e => setFilter(e.target.value)}>
          <option value="all">All coins</option><option value="up">Outperforming BTC</option><option value="down">Underperforming BTC</option>
        </select>
        <select aria-label="Sort relative performance" value={sort} onChange={e => setSort(e.target.value)}>
          <option value="best">Best first</option><option value="worst">Worst first</option>
        </select>
        <button className="row-btn" disabled={loading} onClick={refresh}>{loading ? "Refreshing…" : "Refresh"}</button>
      </div>
      <div className="altbtc-status modal-help" role="status">
        {error || (data ? `${rows.length} of ${data.rows.length} coins · As of ${new Date(data.asOfEpochMs).toLocaleTimeString()} · ${period === "24h" ? "Refreshes every minute" : "Closed 5m candles; checks for updates every minute"}` : period === "24h" ? "Loading Binance comparison…" : "Loading Binance candle history… The first load may take a minute.")}
      </div>
      {data && <div className="altbtc-table" aria-busy={loading}>
        <table className="data">
          <thead><tr><th>Coin</th><th className="num">vs BTC · {period}</th><th className="num">vs USDT · {period}</th><th className="num">Price in BTC</th><th className="num">Volume · {period} USDT</th></tr></thead>
          <tbody>{rows.map(row => <tr key={row.symbol}>
            <td><button className="altbtc-coin" title={`Open ${row.symbol} USD chart`} onClick={() => openChart(row.symbol)}>{row.asset}/BTC</button><small>{row.symbol}</small></td>
            <td className={`num altbtc-strength ${tone(row.changeVsBtcPct)}`}>{pct(row.changeVsBtcPct)}</td>
            <td className={`num ${tone(row.changeUsdtPct)}`}>{pct(row.changeUsdtPct)}</td>
            <td className="num">{row.priceBtc.toLocaleString("en-US", { maximumSignificantDigits: 6 })}</td>
            <td className="num">{fmtVol(row.volumeUsdt)}</td>
          </tr>)}{!rows.length && <tr><td colSpan={5} className="empty">No comparable coins match this filter.</td></tr>}</tbody>
        </table>
      </div>}
      <p className="modal-help altbtc-footnote">Alt/BTC is calculated as ALT/USDT ÷ BTC/USDT, not a BTC trading pair. Green means gaining against BTC, even if USD price falls. Click a coin to open its Kraken USD chart.</p>
      {data?.excludedCount > 0 && <p className="modal-help">{data.excludedCount} Kraken contracts omitted because Binance has no fresh, complete quote for this window.</p>}
    </dialog>
  );
}
