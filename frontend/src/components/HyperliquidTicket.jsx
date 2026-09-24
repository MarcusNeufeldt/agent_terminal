/* Hyperliquid manual orders, Grid and Chase. AI remains disabled on this venue. */

import { useEffect, useState } from "react";
import { fmt } from "../api";
import useStore from "../store";
import { unresolvedReceipt } from "../hyperliquid-receipt.js";
import { contractsForNotional, normalizeContractSize } from "../size-precision";
import { linearExitPreview } from "../risk-preview";
import GridTicket from "./GridTicket";
import { OrderTypeSelect, SizeSlider } from "./TicketControls";
import ChaseStatus from "./ChaseStatus";
import { leverageChoices } from "../leverage";
import { TAKER_FEE } from "../close-preview";

const TYPES = [["mkt", "Market"], ["lmt", "Limit"], ["post", "Post-only"], ["chase", "Chase"], ["ioc", "IOC"], ["stp", "Stop"], ["take_profit", "Take profit"], ["grid", "Grid preview"]];
// The venue rejects these with a minimum order value, so warn before submitting.
const MIN_ORDER_VALUE = 10;
let capacityRefreshPending = false;
// Matches OPEN_CAPACITY_SHARE in terminal/hyperliquid_trading.py.
export const OPEN_CAPACITY_PERCENT = 97;

export function HyperliquidRiskPreview({ position, quantity, price, current }) {
  const preview = current ? linearExitPreview({ position, quantity, exitPrice: price }) : null;
  // A trigger fills as a market order, so the taker fee is certain.
  const net = preview ? preview.pnl - TAKER_FEE.hyperliquid * preview.coveredSize * Number(price) : null;
  return <div className="ticket-note" role="status">
    {preview ? <>
      <div>For a {preview.side.toUpperCase()} reduction, if filled at the trigger: gross PnL ≈ {preview.pnl >= 0 ? "+" : "-"}${Math.abs(preview.pnl).toFixed(2)},
        {" "}after the {(TAKER_FEE.hyperliquid * 100).toFixed(3)}% taker fee ≈ {net >= 0 ? "+" : "-"}${Math.abs(net).toFixed(2)}.</div>
      <div>{preview.coveredSize} contracts · {preview.coveragePct.toFixed(1)}% of the current position.</div>
      {preview.oversized && <div>Requested size exceeds this position. The venue may reject or reduce the order.</div>}
    </> : <div>Estimate unavailable. Current position data, a valid trigger price and size are required.</div>}
    <div>Excludes slippage and funding. This is not a loss cap. Execution may differ, and a trigger-limit order may remain unfilled.</div>
  </div>;
}

export default function HyperliquidTicket() {
  const symbol = useStore(s => s.symbol);
  const otype = useStore(s => s.otype);
  const setOtype = useStore(s => s.setOtype);
  const armed = useStore(s => s.armed);
  const canTrade = useStore(s => s.canTrade);
  const mode = useStore(s => s.signedTrading);
  const ticketBusy = useStore(s => s.ticketBusy);
  const receipt = useStore(s => s.hlReceipt);
  const recoveryKey = useStore(s => s.hlReceiptKey);
  const recoveryError = useStore(s => s.hlRecoveryError);
  const reconciling = useStore(s => s.hlReconciling);
  const fillBusy = useStore(s => s.hlFillBusy);
  const loadFills = useStore(s => s.loadHyperliquidOrderFills);
  const checkLifecycle = useStore(s => s.checkHyperliquidLifecycle);
  const reconcile = useStore(s => s.reconcileHyperliquidOrder);
  const recoveryLoaded = useStore(s => s.hlRecoveryLoaded);
  const serverUnresolved = useStore(s => s.hlServerUnresolved);
  const recoveryHasMore = useStore(s => s.hlRecoveryHasMore);
  const refreshRecovery = useStore(s => s.refreshHyperliquidRecovery);
  const restoreRequest = useStore(s => s.restoreHyperliquidRequest);
  const recoveryBlocked = !recoveryKey || !recoveryLoaded || !!recoveryError ||
    serverUnresolved.length > 0 || recoveryHasMore || unresolvedReceipt(receipt);
  const submitHyperliquidOrder = useStore(s => s.submitHyperliquidOrder);
  const submitHyperliquidChase = useStore(s => s.submitHyperliquidChase);
  const tickers = useStore(s => s.tickers);
  const instruments = useStore(s => s.instruments);
  const capacity = useStore(s => s.hlCapacity);
  const capacityError = useStore(s => s.hlCapacityError);
  const refreshCapacity = useStore(s => s.refreshHyperliquidCapacity);
  const applyLeverage = useStore(s => s.submitHyperliquidLeverage);
  const positions = useStore(s => s.positions);
  const positionState = useStore(s => s.dataStatus.positions?.state);
  const closeDraft = useStore(s => s.hlCloseDraft);
  const clearClose = useStore(s => s.clearHyperliquidClose);

  const [size, setSize] = useState(closeDraft?.size || "");
  const [sizingSource, setSizingSource] = useState("contracts");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [reduce, setReduce] = useState(false);
  const [slippage, setSlippage] = useState("0.5");
  const [percent, setPercent] = useState(0);
  const [stopLimit, setStopLimit] = useState(false);

  useEffect(() => {
    if (!recoveryKey) return;
    refreshCapacity();
    const timer = setInterval(() => { if (!useStore.getState().ticketBusy) refreshCapacity(); }, 10000);
    return () => clearInterval(timer);
  }, [symbol, recoveryKey, refreshCapacity]);

  const ticker = tickers[symbol] || {};
  const instrument = instruments.find(i => i.symbol === symbol) || {};
  const trigger = !closeDraft && ["stp", "take_profit"].includes(otype);
  const matchingPositions = positions.filter(p => !p.error && p.symbol === symbol);
  const market = !closeDraft && otype === "mkt";
  // A Chase prices itself from the book, so sizing uses the current quote.
  const chase = !closeDraft && otype === "chase";
  const explicitPrice = market || chase ? (ticker.ask || ticker.last || ticker.markPrice) : trigger ? stop : limit;
  const price = Number(explicitPrice) || Number(ticker.last ?? ticker.markPrice) || 0;
  const lotDecimals = instrument.contractValueTradePrecision ?? 0;
  const capacityCurrent = capacity?.scopeKey === recoveryKey && capacity?.symbol === symbol &&
    Number.isFinite(capacity.fetchedAt) && Date.now() - capacity.fetchedAt >= 0 && Date.now() - capacity.fetchedAt < 15000;
  const reducing = reduce || trigger || !!closeDraft;
  const percentQuantity = side => {
    const position = matchingPositions.length === 1 && positionState === "current" ? matchingPositions[0] : null;
    // Opening sizes use 97% of the venue maximum (as the server does): at exactly 100%
    // the order has no room for the fee or a tick and Hyperliquid rejects it for margin.
    const available = reducing ? position?.side === (side === "sell" ? "long" : "short") ? position.sizeExact : "0"
      : capacityCurrent ? contractsForNotional(capacity.maxTradeSizes[side], 1, 8, OPEN_CAPACITY_PERCENT) || "0" : "0";
    const quantity = contractsForNotional(available, 1, lotDecimals, percent);
    return Number(quantity) > 0 ? quantity : "";
  };
  const contracts = sizingSource === "percent" ? percentQuantity("buy") || percentQuantity("sell") : size;
  const estimate = Number(contracts) * price;
  const notional = Number(contracts) > 0 && price > 0 && Number.isFinite(estimate) ? estimate : null;
  const belowMinimum = notional !== null && notional < MIN_ORDER_VALUE;
  const maxLeverage = Number.isFinite(instrument.maxLeverage) && instrument.maxLeverage >= 1 ? instrument.maxLeverage : null;
  const quickLeverage = capacityCurrent ? capacity.leverage.value : null;
  const pctDisabled = !!closeDraft || (reducing ? positionState !== "current" || matchingPositions.length !== 1
    : !capacityCurrent || !Object.values(capacity.maxTradeSizes).some(value => Number(value) > 0));
  const sizePercent = pct => {
    if (pctDisabled || (!reducing && Date.now() - capacity.fetchedAt >= 15000)) {
      // A slider drag fires on every step, so ask for fresh capacity once.
      if (!capacityRefreshPending) { capacityRefreshPending = true; Promise.resolve(refreshCapacity()).finally(() => { capacityRefreshPending = false; }); }
      return;
    }
    setPercent(pct);
    setSizingSource(pct > 0 ? "percent" : "contracts");
    if (pct === 0) setSize("");
  };
  const levChoices = leverageChoices(maxLeverage);

  const submit = side => chase ? submitHyperliquidChase(side, {
    size: Number(sizingSource === "percent" ? percentQuantity(side) : contracts), reduceOnly: reduce,
  }) : submitHyperliquidOrder(side, {
    size: Number(sizingSource === "percent" ? percentQuantity(side) : contracts),
    quickPercent: sizingSource === "percent" ? percent : undefined,
    expectedLeverage: sizingSource === "percent" && capacityCurrent ? capacity.leverage.value : undefined,
    expectedMarginMode: sizingSource === "percent" && capacityCurrent ? capacity.leverage.type : undefined,
    // A market order's price comes from the server's fresh quote, so bound the
    // dollar value to what was on screen.
    maxNotional: market && notional !== null
        ? String(Number((notional * (1 + (Number(slippage) || 0) / 100)).toFixed(8)))
        : undefined,
    limitPrice: trigger ? (stopLimit ? Number(limit) : undefined) : market ? undefined : Number(limit),
    slippagePercent: market ? Number(slippage) : undefined,
    stopPrice: trigger ? Number(stop) : undefined,
    // Market trigger by default: a trigger-limit can rest unfilled and leave the
    // position unprotected, so the limit variant is an explicit opt-in.
    triggerMarket: trigger ? !stopLimit : undefined,
    // A trigger without reduce-only is refused by the venue, so it is forced here.
    reduceOnly: trigger || closeDraft ? true : reduce,
  });

  return (
    <div className="panel" id="ticket">
      <div className="ticket-header">
        <div><h3>{closeDraft ? "Reduce-only close" : "Order ticket"}</h3><span className="ticket-market">{symbol}</span></div>
        <span className={`ticket-mode${armed ? " live" : ""}`}>{armed ? `Live · ${mode}` : "Simulation"}</span>
      </div>
      <div className="ticket-body">
        {closeDraft && <div className="ticket-note">
          {closeDraft.side === "sell" ? "Sell limit = minimum execution price." : "Buy limit = maximum execution price."}
          {" IOC may fill only part of the position; it does not guarantee a flat account."}
          <button disabled={ticketBusy} onClick={clearClose}>Regular ticket</button>
        </div>}
        {!closeDraft && <OrderTypeSelect types={TYPES} value={otype} disabled={ticketBusy} onChange={setOtype} />}
        {otype === "grid" && !closeDraft ? <GridTicket symbol={symbol} /> : <>
        {chase ? <div className="ticket-note">
          Rests post-only at the best bid (buy) or ask (sell) and re-pegs as the book moves, so every fill pays the maker fee.
          {reduce ? " Reduce-only: after 5 minutes the unfilled rest closes with a reduce-only market order."
            : " After 5 minutes the unfilled rest is cancelled."} Stop cancels only and never sends a market order.
        </div> : market ? <div className="field">
          <label htmlFor="hl-slippage">Market slippage limit (%)</label>
          <input id="hl-slippage" type="number" min="0.01" max="5" step="0.01" value={slippage}
            onChange={e => setSlippage(e.target.value)} />
          <div className="ticket-note">Priced from a fresh exchange quote and sent as an IOC limit within this bound, so a partial or no fill is possible.</div>
        </div> : trigger ? (
          <div className="field" id="f-stop">
            <label htmlFor="hl-stop">Trigger price <span id="trigger-signal-note">(mark)</span></label>
            <input id="hl-stop" type="number" step="any" placeholder={fmt(ticker.markPrice)} value={stop}
              onChange={e => setStop(e.target.value)} />
            <div className="check-row">
              <input id="hl-stop-limit" type="checkbox" checked={stopLimit}
                onChange={e => setStopLimit(e.target.checked)} />
              <label htmlFor="hl-stop-limit">Stop-limit instead of market trigger</label>
            </div>
            {stopLimit ? (
              <>
                <label htmlFor="hl-trigger-limit">Limit price once triggered</label>
                <input id="hl-trigger-limit" type="number" step="any" placeholder={fmt(ticker.last)} value={limit}
                  onChange={e => setLimit(e.target.value)} />
                <div className="ticket-note">Rests as a limit at this price once the trigger fires. If the market moves past it the order stays unfilled and the position is left unprotected.</div>
              </>
            ) : <div className="ticket-note">Triggers at market, which is the variant most likely to actually fill.</div>}
          </div>
        ) : (
          <div className="field" id="f-limit">
            <label htmlFor="hl-limit">{closeDraft ? "Price bound (IOC limit)" : "Limit price"}</label>
            <input id="hl-limit" type="number" step="any" placeholder={fmt(ticker.last)} value={limit}
              onChange={e => setLimit(e.target.value)} />
          </div>
        )}
        <div className="field ticket-sizing">
          <label htmlFor="hl-size">Size <span className="ticket-unit">Contracts{lotDecimals ? ` · ${lotDecimals} dp` : ""}</span></label>
          <div className="size-input-row">
            <input id="hl-size" type="number" step="any" placeholder="0.0" value={contracts}
              onChange={e => { setSize(e.target.value); setSizingSource("contracts"); setPercent(0); }} />
            <span className={"usd-equiv" + (belowMinimum ? " warn" : "")}>{notional === null ? "" : `≈ $${notional.toFixed(2)}`}</span>
          </div>
          <SizeSlider value={sizingSource === "percent" ? percent : 0} disabled={pctDisabled} onChange={sizePercent}
            title={reducing ? "Percent of the current position" : "Percent of the exchange trading capacity at the current leverage"} />
          {sizingSource === "percent" && <div className="ticket-note">{percent}% of {reducing ? "the current position" : "exchange trading capacity"}: Buy {percentQuantity("buy") || "0"}, Sell {percentQuantity("sell") || "0"} contracts. Final size is rechecked before submission.</div>}
          <div className="ticket-sub-label">Exchange leverage <span>{quickLeverage ? `${quickLeverage}x · ${capacity.leverage.type}` : "Loading current setting"}{maxLeverage ? ` · pair max ${maxLeverage}x` : ""}</span></div>
          <div className="lev-chips" title="Changes actual exchange leverage. Margin mode stays unchanged. Live changes require ARM and confirmation.">
            {levChoices.map(value => (
              <button key={value} disabled={!!closeDraft || !maxLeverage || value > maxLeverage || !capacityCurrent || !canTrade || ticketBusy || reconciling || recoveryBlocked || value === quickLeverage} data-lev={value}
                className={quickLeverage === value ? "active" : ""} onClick={() => applyLeverage(value)}>{value}x</button>
            ))}
          </div>
          {!capacityCurrent && <div className="ticket-note">{capacityError || "Loading exchange trading capacity…"} <button type="button" disabled={ticketBusy} onClick={refreshCapacity}>Refresh capacity</button></div>}
          {capacityCurrent && <div className="ticket-note">Exchange maximum: Buy {capacity.maxTradeSizes.buy}, Sell {capacity.maxTradeSizes.sell} contracts. Already includes the current leverage. 100% uses {OPEN_CAPACITY_PERCENT}% of it so the order still fits the margin.</div>}
        </div>
        <div className="check-row">
          <input id="hl-reduce" type="checkbox" checked={trigger || closeDraft ? true : reduce} disabled={trigger || !!closeDraft}
            onChange={e => setReduce(e.target.checked)} />
          <label htmlFor="hl-reduce">Reduce-only{trigger ? " (required for triggers)" : ""}</label>
        </div>
        {(notional === null || belowMinimum) && <div className="ticket-note" style={{ color: belowMinimum ? "var(--red)" : "var(--muted)" }}>
          {belowMinimum ? `Below the $${MIN_ORDER_VALUE} venue minimum` : `Minimum order value $${MIN_ORDER_VALUE}`}
        </div>}
        {trigger && <HyperliquidRiskPreview position={matchingPositions[0]}
          quantity={normalizeContractSize(contracts, lotDecimals)} price={stop}
          current={positionState === "current" && matchingPositions.length === 1} />}
        <div className="side-btns">
          {closeDraft ? <button id="hl-btn-close"
            disabled={ticketBusy || fillBusy || reconciling || !canTrade || recoveryBlocked}
            onClick={() => submit(closeDraft.side)}>{ticketBusy ? "SUBMITTING…" : `${closeDraft.side.toUpperCase()} TO CLOSE`}</button> : <>
          <button id="hl-btn-buy" disabled={ticketBusy || fillBusy || reconciling || !canTrade || recoveryBlocked || (sizingSource === "percent" && !percentQuantity("buy"))} onClick={() => submit("buy")}>
            {ticketBusy ? "SUBMITTING…" : market ? "MARKET BUY / LONG" : chase ? "CHASE BUY" : "BUY / LONG"}</button>
          <button id="hl-btn-sell" disabled={ticketBusy || fillBusy || reconciling || !canTrade || recoveryBlocked || (sizingSource === "percent" && !percentQuantity("sell"))} onClick={() => submit("sell")}>
            {ticketBusy ? "SUBMITTING…" : market ? "MARKET SELL / SHORT" : chase ? "CHASE SELL" : "SELL / SHORT"}</button>
          </>}
        </div>
        </>}
        <ChaseStatus exchange="hyperliquid" showFinished={otype === "chase"} />
        <div className="ticket-note" style={{ color: armed ? "var(--red)" : "var(--muted)" }}>
          {!canTrade
            ? "Hyperliquid signed trading is off. Set HYPERLIQUID_TRADING and HYPERLIQUID_SECRET_KEY, then restart the backend."
            : armed
              ? "ARMED — a confirmed click signs and sends a real order to Hyperliquid."
              : "DISARMED — the exact order is validated and shown, but nothing is signed or sent."}
        </div>
        {recoveryError && <div className="ticket-note" role="alert">Recovery blocked: {recoveryError}</div>}
        {(!recoveryLoaded || serverUnresolved.length > 0) && (
          <div className="ticket-note" role="status">
            {!recoveryLoaded ? "Server recovery journal unavailable. New orders are blocked." : "Unresolved server submissions:"}
            {serverUnresolved.map(item => (
              <div key={item.requestId}>
                {item.body.symbol} · {item.requestId}
                <button disabled={ticketBusy || reconciling} onClick={() => restoreRequest(item)}>Inspect</button>
              </div>
            ))}
            {recoveryHasMore && <div>More unresolved submissions remain. Refresh after resolving these.</div>}
            <button onClick={refreshRecovery}>Refresh recovery</button>
          </div>
        )}
        {receipt && (
          <div className="ticket-note" role="status" title={JSON.stringify(receipt.action, null, 2)}>
            Last: <b>{receipt.outcome}</b>
            {receipt.status ? ` · ${receipt.status.orderStatus} · order ${receipt.status.order_id}` : ""}
            {receipt.error ? ` — ${receipt.error}` : ""}
            {receipt.action?.type === "order" && receipt.action.orders?.[0]
              ? ` · ${receipt.action.orders[0].b ? "buy" : "sell"} ${receipt.action.orders[0].s} @ ${receipt.action.orders[0].p}`
              : ""}
            {unresolvedReceipt(receipt) ? " · verify on Hyperliquid before retrying" : ""}
            {receipt.batch && <div>
              <div>Prepared batch: {receipt.cloids.length} orders for {receipt.body.symbol}. Status recovery never replaces orders.</div>
              {receipt.cloids.map(cloid => {
                const evidence = receipt.batchEvidence?.targets?.[cloid.toLowerCase()];
                return <div key={cloid}>{cloid}: {evidence?.state || "unresolved"}
                  {evidence?.error || evidence?.row?.error ? ` · ${evidence.error || evidence.row.error}` : ""}</div>;
              })}
              <button disabled={reconciling || fillBusy || ticketBusy || receipt.outcome === "reconciled"} onClick={reconcile}>
                {reconciling ? "Checking batch…" : "Check next unresolved identity"}
              </button>
            </div>}
            {receipt.kind === "leverage" && <div>
              <div>Exchange leverage: {receipt.body.leverage}x, {receipt.body.cross ? "cross" : "isolated"}. No order was requested.</div>
              {unresolvedReceipt(receipt) && <button disabled={reconciling || fillBusy || ticketBusy} onClick={reconcile}>
                Check leverage after request expiry
              </button>}
            </div>}
            {receipt.kind === "chart" && <div>Chart {receipt.body.kind.toUpperCase()} for {receipt.body.symbol}. Unknown requests must expire before status recovery; never drag again to retry.</div>}
            {receipt.closeObservation && <div>
              Position snapshot at {receipt.closeObservation.checkedAt}: {receipt.closeObservation.state === "flat"
                ? "flat" : `${receipt.closeObservation.side} ${receipt.closeObservation.sizeExact} contracts remain`}.
              This is a readback, not proof the close alone caused it. No automatic retry or order cancellation.
            </div>}
            {receipt.cloid && <div>Client ID: {receipt.cloid}</div>}
            {receipt.cloid && receipt.outcome !== "simulated" && (
              <button disabled={reconciling || fillBusy || ticketBusy} onClick={reconcile}>
                {reconciling ? "Checking…" : "Check order status"}
              </button>
            )}
            {!receipt.batch && !["leverage", "chart"].includes(receipt.kind) && receipt.requestId && receipt.outcome !== "simulated" && (
              <button disabled={fillBusy || reconciling || ticketBusy} onClick={checkLifecycle}>Check lifecycle</button>
            )}
            {receipt.lifecycleReport && (
              <div>
                Lifecycle snapshot: {receipt.lifecycleReport.lifecycle || receipt.lifecycleReport.state}
                {receipt.lifecycleReport.fillEvidence ? ` · fill evidence ${receipt.lifecycleReport.fillEvidence}` : ""}
                {receipt.lifecycleReport.checkedAt ? ` · ${receipt.lifecycleReport.checkedAt.slice(0, 19)} UTC` : ""}
                <div>{receipt.lifecycleReport.reason}</div>
              </div>
            )}
            {receipt.status?.order_id && (
              <button disabled={fillBusy || reconciling || ticketBusy} onClick={loadFills}>
                {fillBusy ? "Loading fills…" : "Load order fills"}
              </button>
            )}
            {receipt.fillSummary && (
              <div>
                Observed fills: {receipt.fillSummary.fillCount} · size {receipt.fillSummary.observedFilledSize}
                {receipt.fillSummary.averagePrice ? ` · average ${receipt.fillSummary.averagePrice}` : ""}
                <div>{receipt.fillSummary.scan.scanComplete ? "Available API window scanned." : "Scan incomplete."} History is retention-limited; totals may be incomplete.</div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
