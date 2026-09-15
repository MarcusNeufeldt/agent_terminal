import { useEffect, useRef, useState } from "react";
import { api, fmt } from "../api";
import useStore from "../store";

export function quickGridPreset(side, ticker, position, positionState) {
  if (!["buy", "sell"].includes(side) || positionState !== "current") return null;
  const start = Number(ticker?.[side === "buy" ? "bid" : "ask"]);
  const size = Number(position?.size);
  const end = start * (side === "buy" ? 0.95 : 1.05);
  if (![start, end, size].every(n => Number.isFinite(n) && n > 0)) return null;
  return { side, start, end, size, orders: 20, orderType: "post", reduceOnly: false };
}

export default function GridTicket({ symbol }) {
  const previewOnly = useStore(s => s.exchange === "hyperliquid");
  const seed = useStore(s => s.gridSeed);
  const position = useStore(s => s.positions.find(p => !p.error && p.symbol === symbol));
  const positionState = useStore(s => s.dataStatus.positions?.state);
  const ticker = useStore(s => s.tickers[symbol]);
  const armed = useStore(s => s.armed);
  const rightView = useStore(s => s.rightView);
  const env = useStore(s => s.env);
  const busy = useStore(s => s.ticketBusy || s.bulkBusy);
  const result = useStore(s => s.gridResult);
  const request = useStore(s => s.gridRequest);
  const setGridPreview = useStore(s => s.setGridPreview);
  const submitGrid = useStore(s => s.submitGrid);
  const [side, setSide] = useState(seed?.symbol === symbol ? seed.side : "buy");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [count, setCount] = useState(10);
  const [unit, setUnit] = useState("size");
  const [amount, setAmount] = useState(seed?.symbol === symbol ? String(seed.size) : "");
  const [orderType, setOrderType] = useState("post");
  const [reduceOnly, setReduceOnly] = useState(false);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const checkRequest = useRef(null);
  const action = { symbol, side, startPrice: Number(start), endPrice: Number(end), orders: Number(count),
    [unit]: Number(amount), orderType, reduceOnly };
  const key = JSON.stringify(action);
  const complete = Number(start) > 0 && Number(end) > 0 && Number(amount) > 0;

  useEffect(() => {
    const controller = new AbortController();
    setGridPreview(null);
    if (!complete || rightView !== "ticket") return () => controller.abort();
    const timer = setTimeout(async () => {
      try {
        const body = JSON.parse(key);
        const requested = checkRequest.current;
        checkRequest.current = null;
        if (previewOnly && requested?.key === key && requested.refresh === refresh) body.checkCurrentOrders = true;
        const data = await api("/api/grid/preview", { method: "POST", body, signal: controller.signal });
        if (controller.signal.aborted) return;
        setPreview({ ...data, key, refresh });
        setError("");
        setGridPreview(data.plan);
      } catch (err) {
        if (!controller.signal.aborted) { setPreview(null); setError(err.message); }
      }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); setGridPreview(null); };
  }, [key, complete, refresh, armed, rightView, setGridPreview, previewOnly, checkRequest]);

  const current = preview?.key === key && preview.refresh === refresh && preview.armed === armed ? preview : null;
  const shownResult = result?.symbol === symbol ? result : null;
  const alreadySent = current && shownResult?.previewHash === current.plan.previewHash;
  const quote = (kind) => {
    const ticker = useStore.getState().tickers[symbol] || {};
    const price = kind === "last" ? ticker.last : ticker[side === "buy" ? "bid" : "ask"];
    if (Number(price) > 0) setStart(String(price));
  };
  const positionSize = multiplier => {
    setUnit("size");
    setAmount(String(Number(position.size) * multiplier));
  };
  const applyPreset = next => {
    const s = useStore.getState();
    if (s.ticketBusy || s.bulkBusy) return;
    const preset = quickGridPreset(next, s.tickers[symbol],
      s.positions.find(p => !p.error && p.symbol === symbol), s.dataStatus.positions?.state);
    if (!preset) { s.toast("A current position and best quote are required for the 1× preset.", "warn"); return; }
    setSide(preset.side);
    setStart(String(preset.start));
    setEnd(String(preset.end));
    setCount(preset.orders);
    setUnit("size");
    setAmount(String(preset.size));
    setOrderType(preset.orderType);
    setReduceOnly(preset.reduceOnly);
    setError("");
    setRefresh(r => r + 1);
  };
  const changeSide = next => {
    if (next === side) return;
    setSide(next);
    if (start && end) { setStart(end); setEnd(start); }
  };

  return <div className="grid-ticket">
    <fieldset disabled={busy}>
      <legend className="grid-heading">Price ladder <span>Presets fill the ticket only</span></legend>
      <div className="grid-presets">
        {["buy", "sell"].map(value => <button key={value} type="button" className={`grid-preset ${value}`}
          disabled={!quickGridPreset(value, ticker, position, positionState)}
          title="Load a post-only entry grid. Requires a current position and quote. Review before placing."
          onClick={() => applyPreset(value)}>
          <strong>Quick {value}</strong>
          <span>{value === "buy" ? "Bid → −5%" : "Ask → +5%"} · 20 orders · 1×</span>
        </button>)}
      </div>
      <div className="grid-segment" aria-label="Grid direction">
        {["buy", "sell"].map(value => <button key={value} type="button" aria-pressed={side === value}
          className={side === value ? value : ""} onClick={() => changeSide(value)}>{value === "buy" ? "Buy ↓" : "Sell ↑"}</button>)}
      </div>
      <div className="grid-range">
        <label htmlFor="grid-start">Start price<input id="grid-start" type="number" min="0" step="any" value={start} onChange={e => setStart(e.target.value)} placeholder="From" /></label>
        <label htmlFor="grid-end">End price<input id="grid-end" type="number" min="0" step="any" value={end} onChange={e => setEnd(e.target.value)} placeholder="To" /></label>
      </div>
      <div className="grid-quick">
        <button type="button" onClick={() => quote("best")}>Best {side === "buy" ? "bid" : "ask"}</button>
        <button type="button" onClick={() => quote("last")}>Last price</button>
        {[1, 3, 5].map(depth => <button key={depth} type="button" disabled={!Number(start)}
          title={`Set end ${depth}% ${side === "buy" ? "below" : "above"} start`}
          onClick={() => setEnd(String(Number(start) * (1 + (side === "buy" ? -1 : 1) * depth / 100)))}>{side === "buy" ? "−" : "+"}{depth}%</button>)}
      </div>
      <div className="grid-count">
        <label htmlFor="grid-count">Orders<input id="grid-count" type="number" min="2" max="20" step="1" value={count} onChange={e => setCount(e.target.value)} /></label>
        <div className="grid-quick">{[5, 10, 20].map(n => <button key={n} type="button" aria-pressed={Number(count) === n} onClick={() => setCount(n)}>{n}</button>)}</div>
      </div>
      <div className="grid-range">
        <label htmlFor="grid-amount">Total size<input id="grid-amount" type="number" min="0" step="any" value={amount} onChange={e => setAmount(e.target.value)} placeholder="Across all orders" /></label>
        <label htmlFor="grid-unit">Unit<select id="grid-unit" value={unit} onChange={e => { setUnit(e.target.value); setAmount(""); }}>
          <option value="size">Contracts</option><option value="notional">USD notional</option>
        </select></label>
      </div>
      <div className="grid-quick" title="Copy a multiple of the current real position size">
        <span>Position</span>{[0.5, 1, 2].map(n => <button key={n} type="button"
          disabled={!position || positionState !== "current"} onClick={() => positionSize(n)}>{n === 0.5 ? "½" : n}×</button>)}
      </div>
      <div className="grid-options">
        <label htmlFor="grid-type">Order type<select id="grid-type" value={orderType} onChange={e => setOrderType(e.target.value)}>
          <option value="post">Post-only</option><option value="lmt">Limit</option>
        </select></label>
        <label className="grid-check"><input type="checkbox" checked={reduceOnly} onChange={e => {
          setReduceOnly(e.target.checked);
          if (e.target.checked && position) changeSide(position.side === "long" ? "sell" : "buy");
        }} /> Reduce-only exit</label>
      </div>
    </fieldset>
    <div className="grid-preview" aria-busy={complete && !current && !error}>
      <div className="grid-heading">Preview <button type="button" disabled={busy || !complete} onClick={() => { setError(""); setRefresh(r => r + 1); }}>Refresh</button></div>
      {previewOnly && <button type="button" disabled={busy || !complete || !current} onClick={() => {
        checkRequest.current = { key, refresh: refresh + 1 }; setError(""); setRefresh(r => r + 1);
      }}>Check current orders and quotes</button>}
      {!complete ? <p>Set a range and total size. Preview lines will appear on the chart.</p>
        : !current ? <p role="status">{error || "Calculating exact rungs…"}</p>
          : <>
            <div className="grid-totals"><b>{fmt(current.plan.totalSize)} contracts</b><span>${fmt(current.plan.notional, 2)} total</span><span>Average {fmt(current.plan.averagePrice)}</span></div>
            <div className="grid-rungs"><table><thead><tr><th>#</th><th>Price</th><th>Contracts</th></tr></thead>
              <tbody>{current.plan.orders.map((order, i) => <tr key={i}><td>{i + 1}</td><td>{order.limitPrice}</td><td>{order.size}</td></tr>)}</tbody></table></div>
            {current.plan.warnings.map(warning => <p className="grid-warning" key={warning}>{warning}</p>)}
            {typeof current.orderChecksPassed === "boolean" && <p role="status">
              Order/quote snapshot {current.orderChecksPassed ? "passed" : "failed"} at {current.orderCheckedAt}. Margin is not checked.
            </p>}
            {current.validationError && <p className="grid-error" role="alert">{current.validationError}</p>}
          </>}
    </div>
    {!previewOnly && <button type="button" className={`grid-submit ${side}`} disabled={busy || !current?.ready || !!alreadySent}
      onClick={() => submitGrid(action, current)}>
      {busy ? "Submitting grid…" : alreadySent ? "Submission recorded below" : `${armed ? "Place LIVE" : "Simulate"} ${count} ${side} orders`}
    </button>}
    {previewOnly ? <p className="ticket-note">Hyperliquid planning only. Grid placement is not implemented. No orders will be sent.</p>
      : <p className="ticket-note">{armed ? `LIVE · ${env}` : "DISARMED · no orders sent"}. Existing orders and TP/SL stay untouched. Stops at the first non-confirmed rung.</p>}
    {!previewOnly && shownResult && <div className="grid-result" role="status">
      <strong>{shownResult.simulated ? "Simulated" : `Grid ${shownResult.outcome}`}</strong>
      {shownResult.error && <p>{shownResult.error}</p>}
      {shownResult.responses?.length > 0 && <ol>{shownResult.responses.map((row, i) => <li key={i}>
        {row.order?.limitPrice}: {row.outcome}{row.error ? ` · ${row.error}` : ""}
      </li>)}</ol>}
      {shownResult.transportError && request && <button type="button" disabled={busy}
        onClick={() => submitGrid(request, { armed: request.expectedArmed, plan: { previewHash: request.previewHash } })}>Check submission · same request ID</button>}
      {!shownResult.transportError && <button type="button" disabled={busy} onClick={() => {
        useStore.setState({ gridRequest: null, gridResult: null }); setRefresh(r => r + 1);
      }}>New grid</button>}
    </div>}
  </div>;
}
