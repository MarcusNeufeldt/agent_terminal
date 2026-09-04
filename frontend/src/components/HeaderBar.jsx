import { useState } from "react";
import { fmt } from "../api";
import useStore from "../store";
import StatsModal from "./StatsModal";

export default function HeaderBar() {
  const [statsOpen, setStatsOpen] = useState(false);
  const symbol = useStore(s => s.symbol);
  const tickers = useStore(s => s.tickers);
  const soundOn = useStore(s => s.soundOn);
  const positions = useStore(s => s.positions);
  const chases = useStore(s => s.chases);
  const protectionAlerts = useStore(s => s.protectionAlerts);
  const dataStatus = useStore(s => s.dataStatus);
  const toggleSound = useStore(s => s.toggleSound);
  const t = tickers[symbol] || {};
  const ch = t.change24h !== undefined ? Number(t.change24h) : null;
  const chaseAlert = Object.values(chases || {}).find(chase => ["unknown", "orphaned"].includes(chase.status));
  const protectionAlert = Object.values(protectionAlerts || {})[0];
  const unavailableData = Object.entries(dataStatus || {}).filter(([, state]) => state?.state === "unavailable");
  // worst LIQ distance across open positions (percent)
  let risk = null;
  for (const p of positions) {
    if (p.error || !p.size) continue;
    const m = Number(tickers[p.symbol] && tickers[p.symbol].markPrice);
    const l = Number(p.liqPriceEstimate);
    if (!m || !l) continue;
    const dist = Math.abs(m - l) / m * 100;
    const atr = Number(p.atr14d);
    const mult = atr ? dist / (atr / m * 100) : null; // distance in daily-ATR units
    // worst = smallest ATR multiple (or % when ATR unavailable)
    const score = mult !== null ? mult : dist;
    if (!risk || score < risk.score) risk = { symbol: p.symbol, dist, mult };
  }

  return (
    <header id="header">
      <div className="h-sym">{symbol}</div>
      <div className="h-price">{t.last !== undefined ? fmt(t.last) : "–"}</div>
      <div className="h-item">
        <span className="k">24h change</span>
        <span className={"v " + (ch !== null ? (ch >= 0 ? "up" : "down") : "")}>{ch !== null ? (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%" : "–"}</span>
      </div>
      <div className="h-item"><span className="k">Mark</span><span className="v">{fmt(t.markPrice)}</span></div>
      <div className="h-item"><span className="k">Index</span><span className="v">{fmt(t.indexPrice)}</span></div>
      <div className="h-item"><span className="k">Bid</span><span className="v up">{fmt(t.bid)}</span></div>
      <div className="h-item"><span className="k">Ask</span><span className="v down">{fmt(t.ask)}</span></div>
      <div className="h-item"><span className="k">24h high</span><span className="v">{fmt(t.high24h)}</span></div>
      <div className="h-item"><span className="k">24h low</span><span className="v">{fmt(t.low24h)}</span></div>
      <div className="h-item"><span className="k">24h vol</span><span className="v">{fmtVol24(t.vol24h)}</span></div>
      <div className="h-item"><span className="k">Funding</span><span className="v">{t.fundingRate !== undefined ? (Number(t.fundingRate) * 100).toFixed(4) + "%" : "–"}</span></div>
      <div className="h-item"><span className="k">Open interest</span><span className="v">{fmtVol24(t.openInterest)}</span></div>
      <button id="bell-btn" className={"h-action" + (soundOn ? "" : " off")} title={soundOn ? "Fill sound on — click to mute" : "Fill sound muted — click to enable"} onClick={toggleSound}>
        {soundOn ? "🔔" : "🔕"}
      </button>
      {unavailableData.length > 0 && (() => {
        const [name, state] = unavailableData[0];
        const age = state.ageSeconds == null ? "no saved snapshot" : `${Number(state.ageSeconds).toFixed(1)}s old`;
        return (
          <button
            className="risk-chip crit"
            title={`${name} unavailable; showing last-known data (${age}). ${state.error || ""}`}
            onClick={() => useStore.getState().toast(`${name} unavailable. Displaying last-known data (${age}).`, "err", 12000)}
          >
            ⚠ DATA STALE {name.toUpperCase()} {age}
          </button>
        );
      })()}
      {protectionAlert && (
        <button
          className="risk-chip crit"
          title={protectionAlert.details?.message || "Protection could not be confirmed"}
          onClick={() => useStore.getState().toast(`${protectionAlert.kind} protection for ${protectionAlert.symbol} is not confirmed.`, "err", 15000)}
        >
          ⚠ UNPROTECTED {protectionAlert.symbol} {protectionAlert.kind}
        </button>
      )}
      {chaseAlert && (
        <button
          className="risk-chip crit"
          title={chaseAlert.unknownReason || "A Chase order needs manual reconciliation"}
          onClick={() => useStore.getState().toast(chaseAlert.unknownReason || "A Chase order needs manual reconciliation.", "err", 15000)}
        >
          ⚠ CHASE UNKNOWN {chaseAlert.symbol}
        </button>
      )}
      {risk && (() => {
        // ATR tiers when available: red < 0.5 daily ATR, yellow < 1 ATR ("one average day liquidates you")
        const cls = risk.mult !== null
          ? (risk.mult < 0.4 ? "crit" : risk.mult < 0.75 ? "warn" : "ok")
          : (risk.dist < 1.5 ? "crit" : risk.dist < 4 ? "warn" : "ok");
        const title = risk.mult !== null
          ? `Liquidation is ${risk.mult.toFixed(2)}x daily ATR away${cls === "crit" ? " — DANGER" : cls === "warn" ? " — one average day could liquidate" : ""} — click for positions`
          : `Worst liquidation distance across positions — click for positions`;
        return (
          <button
            className={"risk-chip " + cls}
            title={title}
            onClick={() => useStore.setState({ tab: "positions" })}
          >
            {cls === "crit" ? "⚠ " : ""}{risk.symbol.replace("PF_", "")} LIQ {risk.dist.toFixed(1)}%{risk.mult !== null ? ` (${risk.mult.toFixed(1)}×ATR)` : ""}
          </button>
        );
      })()}
      <button className="h-action" onClick={() => setStatsOpen(true)}>Stats</button>
      <button className="h-action" onClick={() => { location.href = "/volatility"; }}>Volatility Pairs</button>
      {statsOpen && <StatsModal onClose={() => setStatsOpen(false)} />}
    </header>
  );
}

function fmtVol24(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "–";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toFixed(1);
}
