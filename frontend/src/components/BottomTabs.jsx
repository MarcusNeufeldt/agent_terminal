import { fmt } from "../api";
import useStore from "../store";

export default function BottomTabs() {
  const tab = useStore(s => s.tab);
  const setTab = useStore(s => s.setTab);
  const cancelAllForSymbol = useStore(s => s.cancelAllForSymbol);
  const flattenAll = useStore(s => s.flattenAll);
  const bulkBusy = useStore(s => s.bulkBusy);

  return (
    <div id="bottom">
      <div className="tab-bar">
        {[["positions", "Positions"], ["orders", "Orders"], ["fills", "Fills"], ["scanner", "Scanner"]].map(([id, label]) => (
          <button key={id} className={"tab-btn" + (tab === id ? " active" : "")} onClick={() => setTab(id)}>{label}</button>
        ))}
        <div id="bottom-actions">
          <button disabled={bulkBusy} onClick={cancelAllForSymbol}>Cancel all (symbol)</button>
          <button
            className="bulk-icon emergency"
            disabled={bulkBusy}
            title="Emergency: market-close every position, confirm each is flat, then cancel all orders"
            aria-label="Emergency market close all positions and cancel all orders"
            onClick={() => flattenAll("emergency")}
          >⏹</button>
          <button
            className="bulk-icon soft"
            disabled={bulkBusy}
            title="Soft close: start reduce-only Chase exits for every position"
            aria-label="Soft close all positions with reduce-only Chase orders"
            onClick={() => flattenAll("chase")}
          >≫</button>
        </div>
      </div>
      <div className="tab-body" id="tab-body">
        {tab === "positions" && <PositionsTable />}
        {tab === "orders" && <OrdersTable />}
        {tab === "fills" && <FillsTable />}
        {tab === "scanner" && <Scanner />}
      </div>
    </div>
  );
}

function PositionsTable() {
  const positions = useStore(s => s.positions);
  const tickers = useStore(s => s.tickers);
  const computeUpnl = useStore(s => s.computeUpnl);
  const closePosition = useStore(s => s.closePosition);
  const selectSymbol = useStore(s => s.selectSymbol);
  if (!positions.length) return <div className="empty">No open positions</div>;
  return (
    <table className="data">
      <thead><tr>
        <th>Symbol</th><th>Side</th><th className="num">Size</th><th className="num">Entry</th>
        <th className="num">Mark</th><th className="num">Liq (est)</th><th className="num">Unrealized PnL</th>
        <th className="num">Funding</th><th>Liq Δ</th><th></th>
      </tr></thead>
      <tbody>
        {positions.map(p => {
          if (p.error) return null;
          const t = tickers[p.symbol];
          const pnl = computeUpnl(p);
          return (
            <tr key={p.symbol}>
              <td><a href="#" className="sym-link" onClick={e => { e.preventDefault(); selectSymbol(p.symbol); }}>{p.symbol}</a></td>
              <td className={p.side === "long" ? "up" : "down"}>{p.side}</td>
              <td className="num">{fmt(p.size)}</td>
              <td className="num">{fmt(p.price)}</td>
              <td className="num">{fmt(t && t.markPrice)}</td>
              <td className="num" style={{ color: "var(--warn)" }}>{p.liqPriceEstimate ? fmt(p.liqPriceEstimate) : "–"}</td>
              <td className={"num " + (pnl >= 0 ? "up" : "down")}>{pnl >= 0 ? "+" : ""}{fmt(pnl, 2)}</td>
              <td className="num">{fmt(p.unrealizedFunding, 4)}</td>
              <LiqDistCell liq={p.liqPriceEstimate} mark={t && t.markPrice} atr={p.atr14d} />
              <td className="num"><button className="row-btn sell" onClick={() => closePosition(p.symbol)}>Close</button></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function OrdersTable() {
  const orders = useStore(s => s.orders);
  const cancelOrder = useStore(s => s.cancelOrder);
  const selectSymbol = useStore(s => s.selectSymbol);
  if (!orders.length) return <div className="empty">No open orders</div>;
  return (
    <table className="data">
      <thead><tr>
        <th>Symbol</th><th>Side</th><th>Type</th><th className="num">Size</th><th className="num">Limit</th>
        <th className="num">Stop</th><th>Reduce-only</th><th>Time</th><th></th>
      </tr></thead>
      <tbody>
        {orders.map(o => {
          if (o.error) return null;
          const size = o.size ?? ((Number(o.filledSize || 0) + Number(o.unfilledSize || 0)) || null);
          return (
            <tr key={o.cliOrdId || o.order_id || o.limitPrice}>
              <td><a href="#" className="sym-link" onClick={e => { e.preventDefault(); selectSymbol(o.symbol); }}>{o.symbol}</a></td>
              <td className={o.side === "buy" ? "up" : "down"}>{o.side}</td>
              <td>{o.orderType}</td>
              <td className="num">{fmt(size)}</td>
              <td className="num">{fmt(o.limitPrice)}</td>
              <td className="num">{fmt(o.stopPrice)}</td>
              <td>{o.reduceOnly === true || String(o.reduceOnly).toLowerCase() === "true" ? "yes" : ""}</td>
              <td>{String(o.receivedTime || "").slice(0, 19).replace("T", " ")}</td>
              <td className="num">
                <button
                  className="row-btn"
                  onClick={() => cancelOrder({ cliOrdId: o.cliOrdId || null, orderId: o.order_id || null })}
                >Cancel</button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function FillsTable() {
  const fills = useStore(s => s.fills);
  if (!fills.length) return <div className="empty">No recent fills</div>;
  return (
    <table className="data">
      <thead><tr>
        <th>Time</th><th>Symbol</th><th>Side</th><th className="num">Price</th><th className="num">Size</th>
      </tr></thead>
      <tbody>
        {fills.slice(0, 80).map((f, i) => (
          <tr key={i}>
            <td>{String(f.fillTime || "").slice(0, 19).replace("T", " ")}</td>
            <td>{f.symbol}</td>
            <td className={f.side === "buy" ? "up" : "down"}>{f.side}</td>
            <td className="num">{fmt(f.price)}</td>
            <td className="num">{fmt(f.size || f.qty)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Scanner() {
  const rows = useStore(s => s.scannerRows);
  const meta = useStore(s => s.scannerMeta);
  const loading = useStore(s => s.scannerLoading);
  const selectSymbol = useStore(s => s.selectSymbol);
  if (loading && !rows.length) return <div className="empty">Scanning perpetuals… (first scan fetches 1m mark candles per market)</div>;
  return (
    <>
      <div className="scanner-note">{meta} — click a row to open its chart</div>
      {rows.length ? (
        <table className="data">
          <thead><tr>
            <th>Symbol</th><th className="num">Mark</th><th className="num">Realized vol</th>
            <th className="num">Range</th><th className="num">Move</th><th className="num">Spread</th><th className="num">24h volume $</th>
          </tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.symbol} className="sym-link" style={{ cursor: "pointer" }} onClick={() => selectSymbol(r.symbol)}>
                <td>{r.symbol}</td>
                <td className="num">{fmt(r.markPrice)}</td>
                <td className={"num" + (r.realizedVolatilityPercent >= 0.3 ? " up" : "")}>{fmt(r.realizedVolatilityPercent, 3)}%</td>
                <td className="num">{fmt(r.rangePercent, 2)}%</td>
                <td className={"num " + (r.movePercent >= 0 ? "up" : "down")}>{r.movePercent >= 0 ? "+" : ""}{fmt(r.movePercent, 2)}%</td>
                <td className="num">{fmt(r.spreadPercent, 3)}%</td>
                <td className="num">${fmt(r.volumeQuote)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="empty">{loading ? "Scanning…" : "No markets passed the filters"}</div>
      )}
    </>
  );
}

function LiqDistCell({ liq, mark, atr }) {
  const m = Number(mark), l = Number(liq);
  if (!m || !l) return <td className="num"><span className="liq-dist muted">–</span></td>;
  const dist = Math.abs(m - l) / m * 100;
  const mult = Number(atr) ? dist / (Number(atr) / m * 100) : null;
  const cls = mult !== null
    ? (mult < 0.4 ? "crit" : mult < 0.75 ? "warn" : "ok")
    : (dist < 1.5 ? "crit" : dist < 4 ? "warn" : "ok");
  const width = Math.max(4, Math.min(100, (mult !== null ? Math.min(mult / 3, 1) : dist / 10) * 100));
  const label = mult !== null ? `${dist.toFixed(1)}% · ${mult.toFixed(1)}×ATR` : `${dist.toFixed(1)}%`;
  return (
    <td className="num">
      <span className={"liq-dist " + cls}>
        <span className="liq-track"><span className="liq-fill" style={{ width: width + "%" }} /></span>
        {label}
      </span>
    </td>
  );
}
