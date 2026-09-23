import { useEffect, useRef, useState } from "react";
import { api, fmt } from "../api";
import useStore from "../store";
import { closePreview, TAKER_FEE, HL_CLOSE_SLIPPAGE } from "../close-preview";

/* Live "what would I really get" for a market close: re-reads the book every second,
 * walks it for the full position, subtracts the taker fee. Never blocks the close:
 * a missing book only hides the estimate. */

const POLL_MS = 1000;
const signed = v => (v >= 0 ? "+$" : "-$") + fmt(Math.abs(v), 2);

function bookTime(book) {
  const t = Number(book?.time) || Date.parse(book?.serverTime || "");
  return Number.isFinite(t) && t > 0 ? t : null;
}

export function ClosePreviewBody({ preview, venue, book, now = Date.now() }) {
  if (!preview) return <div className="empty">Book unavailable — no estimate. The close still works.</div>;
  const fee = TAKER_FEE[venue];
  const age = bookTime(book) === null ? null : Math.max(0, (now - bookTime(book)) / 1000);
  const into = preview.side === "long" ? "bid" : "ask";
  const rows = [
    ["Screen value (all at best " + into + " " + fmt(preview.best) + ")", signed(preview.pnlAtBest), ""],
    ["Walking the book → avg " + fmt(preview.avgPrice) + " over " + preview.levelsUsed + " level" + (preview.levelsUsed === 1 ? "" : "s")
      + (preview.worstPrice ? ", worst " + fmt(preview.worstPrice) : ""), preview.bookWalkCost > 0 ? "-$" + fmt(preview.bookWalkCost, 2) : "$0.00", "down"],
    ["Taker fee " + (fee * 100).toFixed(3) + "%", "-$" + fmt(preview.exitFee, 2), "down"],
  ];
  if (venue === "kraken") rows.push(["Unrealized funding, settled on close", signed(preview.funding), preview.funding >= 0 ? "up" : "down"]);
  return (
    <>
      <table className="data close-preview">
        <tbody>
          {rows.map(([label, value, cls]) => (
            <tr key={label}><td>{label}</td><td className={"num " + cls}>{value}</td></tr>
          ))}
          <tr className="total">
            <td><b>You'd get closing now</b></td>
            <td className={"num " + (preview.net >= 0 ? "up" : "down")}><b>{preview.net === null ? "–" : signed(preview.net)}</b></td>
          </tr>
        </tbody>
      </table>
      {preview.unfilled > 1e-9 && (
        <div className="ticket-note" role="alert">
          {preview.unfilledReason === "bound"
            ? `Only ${fmt(preview.filled)} of ${fmt(preview.qty)} fills within the ${(HL_CLOSE_SLIPPAGE * 100).toFixed(1)}% price bound; the rest would stay open.`
            : `The visible book only covers ${fmt(preview.filled)} of ${fmt(preview.qty)}${venue === "hyperliquid" ? " (Hyperliquid shows 20 levels)" : ""}. `
              + "The rest fills beyond it, at worse prices than shown."}
        </div>
      )}
      <div className="ticket-note">
        Estimate from the book {age === null ? "" : `${age.toFixed(1)}s ago`}, updated every second. Excludes the entry fee already paid.
        {venue === "hyperliquid" ? " Funding is settled hourly, so none is due on close." : ""} Actual fills can differ as the book moves.
      </div>
    </>
  );
}

export default function ClosePreviewModal({ symbol, onClose }) {
  const venue = useStore(s => s.exchange);
  const position = useStore(s => s.positions.find(p => p.symbol === symbol && !p.error));
  const instrument = useStore(s => s.instruments.find(i => i.symbol === symbol));
  const closePosition = useStore(s => s.closePosition);
  const ticketBusy = useStore(s => s.ticketBusy);
  const [book, setBook] = useState(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const inflight = useRef(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      if (inflight.current) return; // a slow response never stacks requests
      inflight.current = true;
      try {
        const r = await api(`/api/orderbook?symbol=${encodeURIComponent(symbol)}`);
        if (!alive) return;
        if (!r?.orderBook) throw new Error(r?.error || "no book");
        setBook({ ...r.orderBook, time: r.time, serverTime: r.serverTime });
        setError("");
      } catch (e) {
        if (alive) setError(e.message);
      } finally {
        inflight.current = false;
        if (alive) setNow(Date.now());
      }
    };
    load();
    const timer = setInterval(load, POLL_MS);
    const onKey = e => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { alive = false; clearInterval(timer); window.removeEventListener("keydown", onKey); };
  }, [symbol]);

  const preview = position && book ? closePreview({
    position, book, contractSize: instrument?.contractSize ?? 1, inverse: instrument?.type === "futures_inverse",
    feeRate: TAKER_FEE[venue], slippageBound: venue === "hyperliquid" ? HL_CLOSE_SLIPPAGE : null,
    funding: venue === "kraken" ? position.unrealizedFunding : 0,
  }) : null;

  const close = async () => {
    onClose();
    await closePosition(symbol);
  };

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal">
        <div className="modal-head">
          <h3>Market close {symbol}</h3>
          <button className="modal-x" title="Close (Esc)" onClick={onClose}>×</button>
        </div>
        {!position ? <div className="empty">Position no longer open.</div> : <>
          <div className="ticket-note">{position.side} {fmt(position.size)} @ {fmt(position.price)}</div>
          <ClosePreviewBody preview={preview} venue={venue} book={book} now={now} />
          {error && <div className="ticket-note">Book read failed: {error}. Retrying every second.</div>}
        </>}
        <div className="side-btns">
          <button className="btn-sell" disabled={!position || ticketBusy} onClick={close}>Close at market</button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
