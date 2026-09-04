import { useEffect } from "react";
import { fmt } from "../api";
import { formatContractSize } from "../size-precision";
import useStore from "../store";

export default function Ticket() {
  const symbol = useStore(s => s.symbol);
  const otype = useStore(s => s.otype);
  const setOtype = useStore(s => s.setOtype);
  const armed = useStore(s => s.armed);
  const lev = useStore(s => s.lev);
  const setLev = useStore(s => s.setLev);
  const tickers = useStore(s => s.tickers);
  const ticketBusy = useStore(s => s.ticketBusy);
  const submitOrder = useStore(s => s.submitOrder);
  const sizeFromPct = useStore(s => s.sizeFromPct);
  const t = tickers[symbol] || {};

  useEffect(() => {
    const limit = document.getElementById("in-limit");
    const stop = document.getElementById("in-stop");
    if (t && ["lmt", "post", "ioc"].includes(otype) && limit && !limit.value) limit.placeholder = fmt(t.last);
    if (t && ["stp", "take_profit"].includes(otype) && stop && !stop.value) stop.placeholder = fmt(t.markPrice);
  }, [otype, t, symbol]);

  // live $ notional <-> contracts sync (source of truth stays contracts)
  useEffect(() => {
    const sizeEl = document.getElementById("in-size");
    const usdEl = document.getElementById("in-usd");
    const equiv = document.getElementById("usd-equiv");
    if (!sizeEl || !usdEl || !equiv) return;
    const price = () => {
      const lv = Number(document.getElementById("in-limit") && document.getElementById("in-limit").value);
      return lv > 0 ? lv : Number(t.last || t.markPrice || 0);
    };
    const mult = () => {
      const inst = useStore.getState().instruments.find(i => i.symbol === symbol) || {};
      const isInverse = inst.type === "futures_inverse";
      return isInverse ? Number(inst.contractSize || 1) : Number(inst.contractSize || 1) * price();
    };
    const prec = () => Number((useStore.getState().instruments.find(i => i.symbol === symbol) || {}).contractValueTradePrecision ?? 2);
    const syncEquiv = () => {
      const n = Number(sizeEl.value || 0);
      const m = mult();
      equiv.textContent = n > 0 && m > 0 ? `≈ $${(n * m).toLocaleString("en-US", { maximumFractionDigits: 2 })}` : "–";
    };
    sizeEl.addEventListener("input", syncEquiv);
    const syncSize = () => {
      const usd = Number(usdEl.value || 0);
      const m = mult();
      if (usd > 0 && m > 0) sizeEl.value = formatContractSize(usd / m, prec());
      syncEquiv();
    };
    usdEl.addEventListener("input", syncSize);
    const iv = setInterval(syncEquiv, 1000); // keep ≈ label fresh as price moves
    return () => { sizeEl.removeEventListener("input", syncEquiv); usdEl.removeEventListener("input", syncSize); clearInterval(iv); };
  }, [symbol, t]);

  const isLimit = ["lmt", "post", "ioc"].includes(otype);
  const isTrigger = ["stp", "take_profit"].includes(otype);
  const isChase = otype === "chase";

  return (
    <div className="panel" id="ticket">
      <h3>Order ticket <span style={{ color: "var(--text)", textTransform: "none" }}>{symbol}</span></h3>
      <div className="ticket-body">
        <div className="ord-tabs">
          {[["mkt", "Market"], ["lmt", "Limit"], ["post", "Post-only"], ["stp", "Stop"], ["take_profit", "Take profit"], ["chase", "Chase"]].map(([v, label]) => (
            <button key={v} className={"ord-tab" + (otype === v ? " active" : "")} data-otype={v} onClick={() => setOtype(v)}>{label}</button>
          ))}
        </div>
        {isLimit && (
          <div className="field" id="f-limit">
            <label>Limit price</label>
            <input id="in-limit" type="number" step="any" placeholder="0.00" />
          </div>
        )}
        {isTrigger && (
          <div className="field" id="f-stop">
            <label>Trigger price <span id="trigger-signal-note">(mark)</span></label>
            <input id="in-stop" type="number" step="any" placeholder="0.00" />
          </div>
        )}
        <div className="field">
          <label>Size <span style={{ color: "var(--muted)", textTransform: "none" }}>(contracts or $)</span></label>
          <input id="in-size" type="number" step="any" placeholder="0.0" />
          <div className="size-quick">
            {[25, 50, 75, 100].map(p => (
              <button key={p} data-pct={p} onClick={() => sizeFromPct(p)}>{p}%</button>
            ))}
          </div>
          <div className="size-usd-row" title="Type a dollar notional — converts to contracts at the current mark">
            <span className="usd-prefix">$</span>
            <input id="in-usd" type="number" step="any" placeholder="size in USD" />
            <span id="usd-equiv" className="usd-equiv">–</span>
          </div>
          <div className="size-quick" id="lev-quick" title="Leverage applied to the % size buttons">
            {[1, 2, 3, 5, 10].map(l => (
              <button key={l} data-lev={l} className={lev === l ? "active" : ""} onClick={() => setLev(l)}>{l}x</button>
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
        <div className="ticket-note" style={{ color: isChase ? (armed ? "var(--accent)" : "var(--muted)") : (armed ? "var(--red)" : "var(--muted)") }}>
          {isChase
            ? (armed ? "CHASE: reconciled post-only orders at best bid/ask, re-pegged only after confirmed cancellation." : "CHASE requires an armed terminal.")
            : (armed ? `LIVE: orders go straight to Kraken (${window.__env || "live"}).` : "SIMULATION: arm the terminal to send real orders.")}
        </div>
      </div>
    </div>
  );
}
