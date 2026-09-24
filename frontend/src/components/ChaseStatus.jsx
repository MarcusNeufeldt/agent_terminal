/* Live Chase status under the Buy/Sell buttons, for both venues. */

import { useEffect, useState } from "react";
import { fmt } from "../api";
import useStore from "../store";
import { chaseCards, chaseProgress } from "../chase-view";

const STATUS_LABEL = {
  running: "Chasing", filled: "Filled", partial: "Partly filled", cancelled: "Cancelled", timeout: "Timed out",
  aborted: "Stopped", rejected: "Rejected", unknown: "Needs check", orphaned: "Needs check", acknowledged: "Checked",
  max_repegs: "Stopped", post_only_rejections: "Stopped",
};

function clock(seconds) {
  if (seconds === null) return "–";
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export default function ChaseStatus({ exchange }) {
  const chases = useStore(s => s.chases);
  const tickers = useStore(s => s.tickers);
  const abortChase = useStore(s => s.abortChase);
  const [now, setNow] = useState(() => Date.now());
  const [stopping, setStopping] = useState({});
  const cards = chaseCards(chases, exchange, now);
  const running = cards.some(c => c.status === "running");

  // A one-second clock only while something is on screen, for the countdown and fade-out.
  useEffect(() => {
    if (!cards.length) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [cards.length, running]);

  if (!cards.length) return null;
  const venue = exchange === "hyperliquid" ? "Hyperliquid" : "Kraken";
  return (
    <div className="chase-cards" aria-live="polite">
      {cards.map(c => {
        const p = chaseProgress(c, now / 1000);
        const live = c.status === "running";
        const check = ["unknown", "orphaned"].includes(c.status);
        const t = tickers[c.symbol] || {};
        const best = c.side === "buy" ? t.bid : t.ask;
        const lastEvent = String((c.events || []).at(-1) || "").replace(/^\d\d:\d\d:\d\d\s*/, "");
        return (
          <div key={c.id} className={`chase-card ${c.side} status-${c.status}`}>
            <div className="chase-card-head">
              <span className="chase-card-title">Chase {c.side === "buy" ? "BUY" : "SELL"} {fmt(c.size)} <span className="muted">{c.symbol}</span></span>
              <span className={`chase-pill ${live ? "live" : check ? "warn" : c.status === "filled" ? "ok" : ""}`}>{STATUS_LABEL[c.status] || c.status}</span>
            </div>
            <div className="chase-bar" title={`${fmt(c.filled)} of ${fmt(c.size)} filled`}>
              <div className="chase-bar-fill" style={{ width: `${p.filledPct}%` }} />
            </div>
            <div className="chase-card-grid">
              <span>Filled</span><b>{fmt(c.filled)} / {fmt(c.size)}</b>
              {live && <><span>Resting at</span><b>{c.activePrice ? fmt(c.activePrice) : "placing…"}{best ? <span className="muted"> · best {c.side === "buy" ? "bid" : "ask"} {fmt(best)}</span> : null}</b></>}
              <span>Pegs</span><b>{c.pegs ?? 0}</b>
              {live && <><span>Time left</span><b>{clock(p.left)}{c.spec?.reduceOnly ? <span className="muted"> · then market</span> : <span className="muted"> · then cancel</span>}</b></>}
            </div>
            {lastEvent && <div className="chase-card-event" title={(c.events || []).join("\n")}>{lastEvent}</div>}
            {check && <div className="chase-card-event warn">{c.unknownReason || `Needs a manual check on ${venue}.`} New orders and exchange switching are blocked until it is checked.</div>}
            {live && <button type="button" className="chase-stop" disabled={!!stopping[c.id]}
              title="Cancels the resting order. Never sends a market order."
              onClick={async () => { setStopping(s => ({ ...s, [c.id]: true })); try { await abortChase(c.id); } finally { setStopping(s => ({ ...s, [c.id]: false })); } }}>
              {stopping[c.id] ? "Stopping…" : "Stop chase"}</button>}
            {check && <button type="button" className="chase-stop" onClick={() => abortChase(c.id, { acknowledge: true })}>Mark checked</button>}
          </div>
        );
      })}
    </div>
  );
}
