import { fmt } from "../api";
import useStore from "../store";
import { hyperliquidProtection } from "../hyperliquid-protection";
import { isLastStale, valuationPrice } from "../pricing.js";

export default function BottomTabs() {
  const tab = useStore(s => s.tab);
  const setTab = useStore(s => s.setTab);
  const cancelAllForSymbol = useStore(s => s.cancelAllForSymbol);
  const cancelHyperliquidOrders = useStore(s => s.cancelHyperliquidOrders);
  const flattenAll = useStore(s => s.flattenAll);
  const bulkBusy = useStore(s => s.bulkBusy);
  const readOnly = useStore(s => s.readOnly);
  const canTrade = useStore(s => s.canTrade);
  const orderState = useStore(s => s.dataStatus.orders?.state);
  const cancelReceipt = useStore(s => s.hlCancelReceipt);
  const cancelHistory = useStore(s => s.hlCancelHistory);
  const cancelHistoryMore = useStore(s => s.hlCancelHistoryMore);
  const cancelHistoryNote = useStore(s => s.hlCancelHistoryNote);
  const cancelChecking = useStore(s => s.hlCancelChecking);
  const loadCancellations = useStore(s => s.loadHyperliquidCancellations);
  const inspectCancellation = useStore(s => s.inspectHyperliquidCancellation);
  const checkCancel = useStore(s => s.reconcileHyperliquidCancel);

  return (
    <div id="bottom">
      <div className="tab-bar">
        {[["positions", "Positions"], ["orders", "Orders"], ["fills", "Fills"], ["scanner", "Scanner"]].map(([id, label]) => (
          <button key={id} disabled={readOnly && id === "scanner"} className={"tab-btn" + (tab === id ? " active" : "")} onClick={() => setTab(id)}>{label}</button>
        ))}
        <div id="bottom-actions">
          <button disabled={bulkBusy || (readOnly && (!canTrade || orderState !== "current"))} onClick={cancelAllForSymbol}>Cancel all (symbol)</button>
          {readOnly && <button disabled={bulkBusy || !canTrade || orderState !== "current"}
            onClick={() => cancelHyperliquidOrders(true)}>Cancel all native perps</button>}
          <button
            className="bulk-icon emergency"
            disabled={bulkBusy || readOnly}
            title="Emergency: market-close every position, confirm each is flat, then cancel all orders"
            aria-label="Emergency market close all positions and cancel all orders"
            onClick={() => flattenAll("emergency")}
          >⏹</button>
          <button
            className="bulk-icon soft"
            disabled={bulkBusy || readOnly}
            title="Soft close: start reduce-only Chase exits for every position"
            aria-label="Soft close all positions with reduce-only Chase orders"
            onClick={() => flattenAll("chase")}
          >≫</button>
        </div>
      </div>
      <div className="tab-body" id="tab-body">
        {readOnly && tab === "orders" && <details className="ticket-note">
          <summary>Cancellation recovery</summary>
          <button disabled={cancelChecking || bulkBusy} onClick={() => loadCancellations()}>Load recent cancellation history</button>
          <form onSubmit={event => {
            event.preventDefault();
            loadCancellations(new FormData(event.currentTarget).get("requestId"));
          }}>
            <label htmlFor="hl-cancel-request-id">Find an older cancellation by request ID</label>
            <input id="hl-cancel-request-id" name="requestId" required minLength={8} maxLength={100} autoComplete="off" />
            <button disabled={cancelChecking || bulkBusy} type="submit">Find receipt</button>
          </form>
          {cancelHistoryNote && <div role="status">{cancelHistoryNote}</div>}
          {cancelHistory.map(item => <div key={item.requestId}>
            {item.body.symbol || "Multi-symbol batch"} · {item.requestId}
            <button disabled={cancelChecking || bulkBusy} onClick={() => inspectCancellation(item)}>Inspect</button>
          </div>)}
          {cancelHistoryMore && <div>Showing the latest 20 requests. Use the request ID lookup for an older receipt.</div>}
        </details>}
        {readOnly && cancelReceipt && tab === "orders" && (
          <details className="ticket-note">
            <summary>Hyperliquid cancellation receipt: {cancelReceipt.outcome} · {cancelReceipt.symbol} · {cancelReceipt.orderIds.length} orders</summary>
            <div>Request: {cancelReceipt.requestId}</div>
            <div>Status checks are read-only snapshots, not proof this cancellation caused the status. No retry or replacement is performed.</div>
            {cancelReceipt.recoveryError && <div>{cancelReceipt.recoveryError}</div>}
            <ul>{cancelReceipt.results.map(row => (
              <li key={row.orderId}>{cancelReceipt.symbols?.[row.orderId] ? `${cancelReceipt.symbols[row.orderId]} · ` : ""}{row.orderId}: {row.outcome}{row.error ? ` · ${row.error}` : ""}
                <button disabled={cancelChecking || bulkBusy || cancelReceipt.outcome === "simulated"} onClick={() => checkCancel(row.orderId)}>Check status</button>
                {cancelReceipt.readbacks?.[row.orderId] && <div>
                  Observed: {cancelReceipt.readbacks[row.orderId].status?.orderStatus || cancelReceipt.readbacks[row.orderId].state}
                  {" · "}{cancelReceipt.readbacks[row.orderId].checkedAt}
                </div>}
              </li>
            ))}</ul>
          </details>
        )}
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
  const native = useStore(s => s.exchange === "hyperliquid");
  const orders = useStore(s => s.orders);
  const orderState = useStore(s => s.dataStatus.orders?.state);
  const canTrade = useStore(s => s.canTrade);
  const readOnly = useStore(s => s.readOnly);
  const exchangeName = readOnly ? "Hyperliquid" : "Kraken";
  const status = useStore(s => s.dataStatus.positions);
  const tickers = useStore(s => s.tickers);
  const computeUpnl = useStore(s => s.computeUpnl);
  const openGrid = useStore(s => s.openGrid);
  const ticketBusy = useStore(s => s.ticketBusy);
  const closePosition = useStore(s => s.closePosition);
  const flattenAll = useStore(s => s.flattenAll);
  const bulkBusy = useStore(s => s.bulkBusy);
  const selectSymbol = useStore(s => s.selectSymbol);
  if (!positions.length) return <div className="empty">{status?.state === "current" ? "No open positions" : status?.error || "Position data unavailable"}</div>;
  return (
    <table className="data">
      {native && <caption className="muted">Stop snapshots only. Execution is not guaranteed; combined ladder coverage is not assessed.</caption>}
      <thead><tr>
        <th>Symbol</th><th>Side</th><th className="num">Size</th><th className="num">Entry</th>
        <th className="num" title={`${exchangeName} price this position closes into: the bid for a long, the ask for a short. Falls back to the last trade only if the book is unavailable.`}>Exit</th><th className="num">{readOnly ? "Liq (exchange)" : "Liq (est)"}</th><th className="num" title={`${exchangeName} price this position closes into, excluding fees and funding`}>Unrealized PnL · exit</th>
        <th className="num">{readOnly ? "Cum funding" : "Funding"}</th><th>Liq Δ</th>{native && <th>Stop observation</th>}<th></th>
      </tr></thead>
      <tbody>
        {positions.map(p => {
          if (p.error) return null;
          const t = tickers[p.symbol];
          // Same basis as the PnL beside it: the side this position closes into.
          const { price: quote, basis } = valuationPrice(t, { mode: "exit", side: p.side });
          const displayQuote = Number.isFinite(quote) && quote > 0 ? quote : null;
          const staleLast = isLastStale(t);
          const pnl = computeUpnl(p);
          const protection = native ? hyperliquidProtection(p, orders, status?.state, orderState) : null;
          return (
            <tr key={p.symbol}>
              <td><a href="#" className="sym-link" onClick={e => { e.preventDefault(); selectSymbol(p.symbol); }}>{p.symbol}</a></td>
              <td className={p.side === "long" ? "up" : "down"}>{p.side}</td>
              <td className="num">{fmt(p.size)}</td>
              <td className="num">{fmt(p.price)}</td>
              <td className="num" title={`Mark for risk: ${fmt(t?.markPrice)}`}>{fmt(displayQuote)}</td>
              <td className="num" style={{ color: "var(--warn)" }}>{p.liqPriceEstimate ? fmt(p.liqPriceEstimate) : "–"}</td>
              <td className={"num " + (pnl === null ? "muted" : pnl >= 0 ? "up" : "down")}
                title={pnl === null ? "Book PnL unavailable"
                  : `${readOnly ? "Hyperliquid" : "Kraken"} ${basis}: ${fmt(quote)} · last: ${fmt(t?.last)}${staleLast ? " (stale, outside book)" : ""} · mark: ${fmt(t?.markPrice)}`}>{pnl !== null && pnl >= 0 ? "+" : ""}{fmt(pnl, 2)}</td>
              <td className="num">{fmt(readOnly ? p.fundingSinceOpen : p.unrealizedFunding, 4)}</td>
              <LiqDistCell liq={p.liqPriceEstimate} mark={t && t.markPrice} atr={p.atr14d} />
              {protection && <td title={protection.detail} className={protection.state === "missing" ? "down" : "muted"}>
                {protection.label}
              </td>}
              <td className="num">
                <button className="row-btn sell" disabled={bulkBusy || (readOnly && (!canTrade || ticketBusy || status?.state !== "current"))}
                  onClick={() => closePosition(p.symbol)}>Close</button>{" "}
                <button className="row-btn" disabled={bulkBusy || readOnly}
                  title={`Soft close ${p.symbol} with a reduce-only Chase`}
                  aria-label={`Soft close ${p.symbol} with a reduce-only Chase`}
                  onClick={() => flattenAll("chase", p.symbol)}>≫</button>{" "}
                <button className="row-btn" disabled={bulkBusy || ticketBusy || (readOnly && status?.state !== "current")}
                  title={`Build a grid using ${p.symbol} position size`}
                  onClick={() => openGrid(p.symbol)}>Grid</button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function OrdersTable() {
  const orders = useStore(s => s.orders);
  const positions = useStore(s => s.positions);
  const native = useStore(s => s.exchange === "hyperliquid");
  const canProtect = useStore(s => s.canHyperliquidChart());
  const submitProtection = useStore(s => s.submitHyperliquidProtection);
  const canTrade = useStore(s => s.canTrade);
  const bulkBusy = useStore(s => s.bulkBusy);
  const status = useStore(s => s.dataStatus.orders);
  const cancelOrder = useStore(s => s.cancelOrder);
  const selectSymbol = useStore(s => s.selectSymbol);
  if (!orders.length) return <div className="empty">{status?.state === "current" ? "No open orders" : status?.error || "Order data unavailable"}</div>;
  return (
    <table className="data">
      <thead><tr>
        <th>Symbol</th><th>Side</th><th>Type</th><th className="num">Size</th><th className="num">Limit</th>
        <th className="num">Stop</th><th>Reduce-only</th><th>Time</th><th></th>
      </tr></thead>
      <tbody>
        {orders.map(o => {
          if (o.error) return null;
          const position = positions.find(p => p.symbol === o.symbol && !p.error);
          const size = o.size ?? ((Number(o.filledSize || 0) + Number(o.unfilledSize || 0)) || null);
          const convert = native && !o.positionTpsl && o.reduceOnly === true && ["tp", "sl"].includes(o.triggerKind);
          const soleExit = orders.filter(p => p.symbol === o.symbol && p.reduceOnly === true && p.triggerKind === o.triggerKind).length === 1;
          return (
            <tr key={o.cliOrdId || o.order_id || o.limitPrice}>
              <td><a href="#" className="sym-link" onClick={e => { e.preventDefault(); selectSymbol(o.symbol); }}>{o.symbol}</a></td>
              <td className={o.side === "buy" ? "up" : "down"}>{o.side}</td>
              <td>{o.orderType}</td>
              <td className="num">{o.positionTpsl ? `Full position${position ? ` · ${fmt(position.size)}` : ""}` : fmt(size)}</td>
              <td className="num">{fmt(o.limitPrice)}</td>
              <td className="num">{fmt(o.stopPrice)}</td>
              <td>{o.reduceOnly === true || String(o.reduceOnly).toLowerCase() === "true" ? "yes" : ""}</td>
              <td>{String(o.receivedTime || "").slice(0, 19).replace("T", " ")}</td>
              <td className="num">
                {convert && <button className="row-btn" disabled={!canProtect || !position || !soleExit}
                  title="Convert this exact TP/SL to native full-position sizing. Follows future position changes. Requires confirmation; partial ladders are preserved."
                  onClick={() => submitProtection(o.symbol, o.triggerKind, Number(o.stopPrice),
                    { snapshot: o, orderId: o.order_id }, null, true)}>Full position</button>}
                <button
                  className="row-btn"
                  disabled={bulkBusy || !canTrade || status?.state !== "current"}
                  onClick={() => cancelOrder({ symbol: o.symbol, cliOrdId: o.cliOrdId || null, orderId: o.order_id || null })}
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
  const status = useStore(s => s.dataStatus.fills);
  if (!fills.length) return <div className="empty">{status?.state === "current" ? "No recent fills" : status?.error || "Fill data unavailable"}</div>;
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
