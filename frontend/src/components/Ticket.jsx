import { useEffect, useState } from "react";
import { fmt } from "../api";
import { leverageChoices, pairMaxLeverage } from "../leverage";
import useStore from "../store";
import GridTicket from "./GridTicket";
import { OrderTypeSelect, SizeSlider } from "./TicketControls";
import ChaseStatus from "./ChaseStatus";

const TYPES = [["mkt", "Market"], ["lmt", "Limit"], ["post", "Post-only"], ["stp", "Stop"], ["take_profit", "Take profit"], ["chase", "Chase"], ["grid", "Grid"]];

export default function Ticket() {
  const symbol = useStore(s => s.symbol);
  const otype = useStore(s => s.otype);
  const setOtype = useStore(s => s.setOtype);
  const armed = useStore(s => s.armed);
  const lev = useStore(s => s.lev);
  const setLev = useStore(s => s.setLev);
  const tickers = useStore(s => s.tickers);
  const ticketBusy = useStore(s => s.ticketBusy);
  const gridSeed = useStore(s => s.gridSeed);
  const submitOrder = useStore(s => s.submitOrder);
  const sizeFromPct = useStore(s => s.sizeFromPct);
  const instruments = useStore(s => s.instruments);
  const t = tickers[symbol] || {};
  // The slider position belongs to the symbol it sized, so it resets on a switch.
  const [sized, setSized] = useState({ symbol, pct: 0 });
  const pct = sized.symbol === symbol ? sized.pct : 0;
  const setPct = value => setSized({ symbol, pct: value });
  // The % sizing multiplier never exceeds 10x or the pair's own maximum.
  const maxLev = pairMaxLeverage(instruments.find(i => i.symbol === symbol));
  const levChoices = leverageChoices(Math.min(maxLev || 10, 10), [1, 2, 3, 5, 10]);

  useEffect(() => {
    const limit = document.getElementById("in-limit");
    const stop = document.getElementById("in-stop");
    if (t && ["lmt", "post", "ioc"].includes(otype) && limit && !limit.value) limit.placeholder = fmt(t.last);
    if (t && ["stp", "take_profit"].includes(otype) && stop && !stop.value) stop.placeholder = fmt(t.markPrice);
  }, [otype, t, symbol]);

  // live notional readout for the typed contract size
  useEffect(() => {
    const sizeEl = document.getElementById("in-size");
    const equiv = document.getElementById("usd-equiv");
    if (!sizeEl || !equiv) return;
    const price = () => {
      const lv = Number(document.getElementById("in-limit") && document.getElementById("in-limit").value);
      return lv > 0 ? lv : Number(t.last || t.markPrice || 0);
    };
    const mult = () => {
      const inst = useStore.getState().instruments.find(i => i.symbol === symbol) || {};
      const isInverse = inst.type === "futures_inverse";
      return isInverse ? Number(inst.contractSize || 1) : Number(inst.contractSize || 1) * price();
    };
    const syncEquiv = () => {
      const n = Number(sizeEl.value || 0);
      const m = mult();
      equiv.textContent = n > 0 && m > 0 ? `≈ $${(n * m).toLocaleString("en-US", { maximumFractionDigits: 2 })}` : "";
    };
    sizeEl.addEventListener("input", syncEquiv);
    syncEquiv(); // ticker updates re-run this effect, so refresh now rather than wait for the interval
    const iv = setInterval(syncEquiv, 1000); // keep the readout fresh as price moves and after % sizing
    return () => { sizeEl.removeEventListener("input", syncEquiv); clearInterval(iv); };
  }, [symbol, t, otype]);

  const isLimit = ["lmt", "post", "ioc"].includes(otype);
  const isTrigger = ["stp", "take_profit"].includes(otype);
  const isChase = otype === "chase";

  return (
    <div className={`panel${otype === "grid" ? " grid-active" : ""}`} id="ticket">
      <div className="ticket-header">
        <div><h3>Order ticket</h3><span className="ticket-market">{symbol}</span></div>
        <span className={`ticket-mode${armed ? " live" : ""}`}>{armed ? "Live trading" : "Simulation"}</span>
      </div>
      <div className="ticket-body">
        <OrderTypeSelect types={TYPES} value={otype} disabled={ticketBusy} onChange={setOtype} />
        {otype === "grid" ? <GridTicket key={`${symbol}:${gridSeed?.id || ""}`} symbol={symbol} /> : <>
        {isLimit && (
          <div className="field" id="f-limit">
            <label htmlFor="in-limit">Limit price</label>
            <input id="in-limit" type="number" step="any" placeholder="0.00" />
          </div>
        )}
        {isTrigger && (
          <div className="field" id="f-stop">
            <label htmlFor="in-stop">Trigger price <span id="trigger-signal-note">(mark)</span></label>
            <input id="in-stop" type="number" step="any" placeholder="0.00" />
          </div>
        )}
        <div className="field ticket-sizing">
          <label htmlFor="in-size">Size <span className="ticket-unit">Contracts</span></label>
          <div className="size-input-row">
            <input id="in-size" type="number" step="any" placeholder="0.0" onInput={() => setPct(0)} />
            <span id="usd-equiv" className="usd-equiv"></span>
          </div>
          <SizeSlider value={pct} onChange={p => { setPct(p); if (p > 0) sizeFromPct(p); }}
            title={`Percent of available margin at ${lev}x`} />
          <div className="ticket-sub-label">Sizing leverage <span>{maxLev ? `pair max ${maxLev}x · ` : ""}for the % sizing only</span></div>
          <div className="lev-chips" id="lev-quick" title="Leverage applied to the % sizing. It does not change anything on Kraken.">
            {levChoices.map(l => (
              <button key={l} data-lev={l} className={lev === l ? "active" : ""} onClick={() => { setLev(l); if (pct > 0) setTimeout(() => useStore.getState().sizeFromPct(pct)); }}>{l}x</button>
            ))}
          </div>
        </div>
        <div className="check-row">
          <input id="in-reduce" type="checkbox" />
          <label htmlFor="in-reduce">Reduce-only</label>
        </div>
        <div className="side-btns">
          <button id="btn-buy" disabled={ticketBusy} onClick={() => submitOrder("buy")}>{ticketBusy ? "SUBMITTING…" : otype === "mkt" ? "BUY / LONG" : "BUY"}</button>
          <button id="btn-sell" disabled={ticketBusy} onClick={() => submitOrder("sell")}>{ticketBusy ? "SUBMITTING…" : otype === "mkt" ? "SELL / SHORT" : "SELL"}</button>
        </div>
        <ChaseStatus exchange="kraken" showFinished={otype === "chase"} />
        <div className="ticket-note" style={{ color: isChase ? (armed ? "var(--accent)" : "var(--muted)") : (armed ? "var(--red)" : "var(--muted)") }}>
          {isChase
            ? (armed ? "CHASE: reconciled post-only orders at best bid/ask, re-pegged only after confirmed cancellation." : "CHASE requires an armed terminal.")
            : (armed ? `LIVE: orders go straight to Kraken (${window.__env || "live"}).` : "SIMULATION: arm the terminal to send real orders.")}
        </div>
        </>}
        {otype === "grid" && <ChaseStatus exchange="kraken" showFinished={otype === "chase"} />}
      </div>
    </div>
  );
}
