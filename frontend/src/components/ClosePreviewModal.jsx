import { useEffect, useRef, useState } from "react";
import { api, fmt } from "../api";
import useStore from "../store";
import { closePreview, TAKER_FEE, HL_CLOSE_SLIPPAGE } from "../close-preview";

/* Live "what would I really get" for a market close: re-reads the book every second,
 * walks it for the full position, subtracts the taker fee. Never blocks the close:
 * a missing book only hides the estimate. */

const POLL_MS = 1000;
const signed = v => (v >= 0 ? "+$" : "-$") + fmt(Math.abs(v), 2);
const cost = v => (v > 0.005 ? "-$" + fmt(v, 2) : "$0.00");

function bookTime(book) {
  const t = Number(book?.time) || Date.parse(book?.serverTime || "");
  return Number.isFinite(t) && t > 0 ? t : null;
}

function Row({ label, detail, value, tone = "" }) {
  return (
    <div className="cp-row">
      <div className="cp-label">{label}{detail && <span className="cp-detail">{detail}</span>}</div>
      <div className={"cp-value " + tone}>{value}</div>
    </div>
  );
}

export function ClosePreviewBody({ preview, venue, book, now = Date.now() }) {
  if (!preview) return <div className="cp-empty">Book unavailable — no estimate. The close still works.</div>;
  const fee = TAKER_FEE[venue];
  const age = bookTime(book) === null ? null : Math.max(0, (now - bookTime(book)) / 1000);
  const into = preview.side === "long" ? "bid" : "ask";
  const hidden = preview.net === null ? null : preview.pnlAtBest - preview.net;
  return (
    <>
      <div className="cp-hero">
        <div className="cp-hero-label">You'd get closing now</div>
        <div className={"cp-hero-value " + (preview.net === null ? "" : preview.net >= 0 ? "up" : "down")}>
          {preview.net === null ? "–" : signed(preview.net)}
        </div>
        <div className="cp-hero-sub">
          Screen shows <b>{signed(preview.pnlAtBest)}</b>
          {hidden !== null && Math.abs(hidden) >= 0.005 && <> · <span className="down">{signed(-hidden)} in costs</span></>}
        </div>
      </div>

      <div className="cp-list">
        <Row label="Screen value" detail={`all at best ${into} ${fmt(preview.best)}`} value={signed(preview.pnlAtBest)} />
        <Row label="Walking the book"
          detail={`→ avg ${fmt(preview.avgPrice)} over ${preview.levelsUsed} level${preview.levelsUsed === 1 ? "" : "s"}`
            + (preview.worstPrice ? `, worst ${fmt(preview.worstPrice)}` : "")}
          value={cost(preview.bookWalkCost)} tone={preview.bookWalkCost > 0.005 ? "down" : ""} />
        <Row label="Taker fee" detail={`${(fee * 100).toFixed(3)}%`} value={cost(preview.exitFee)} tone="down" />
        {venue === "kraken" && (
          <Row label="Funding" detail="settled on close" value={signed(preview.funding)}
            tone={preview.funding >= 0 ? "up" : "down"} />
        )}
      </div>

      {preview.unfilled > 1e-9 && (
        <div className="cp-warn" role="alert">
          {preview.unfilledReason === "bound"
            ? `Only ${fmt(preview.filled)} of ${fmt(preview.qty)} fills within the ${(HL_CLOSE_SLIPPAGE * 100).toFixed(1)}% price bound; the rest would stay open.`
            : `The visible book only covers ${fmt(preview.filled)} of ${fmt(preview.qty)}${venue === "hyperliquid" ? " (Hyperliquid shows 20 levels)" : ""}. `
              + "The rest fills beyond it, at worse prices than shown."}
        </div>
      )}

      <div className="cp-foot">
        <span className={"cp-live" + (age !== null && age < 5 ? " on" : "")} />
        {age === null ? "Waiting for the book" : `Book ${age.toFixed(1)}s ago`} · updates every second.
        {" "}Excludes the entry fee already paid{venue === "hyperliquid" ? "; funding settles hourly, none due on close" : ""}.
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
  // Closing a long sells, closing a short buys: colour the action by the order it sends.
  const sells = position?.side !== "short";

  return (
    <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal close-modal" role="dialog" aria-label={`Market close ${symbol}`}>
        <div className="cp-head">
          <div>
            <div className="cp-title">Market close</div>
            <div className="cp-market">
              <span className="cp-symbol">{symbol}</span>
              {position && <span className={"cp-side " + (position.side === "short" ? "short" : "long")}>{position.side}</span>}
              {position && <span className="cp-size">{fmt(position.size)} @ {fmt(position.price)}</span>}
            </div>
          </div>
          <button className="modal-x" title="Close (Esc)" onClick={onClose}>×</button>
        </div>
        {!position ? <div className="cp-empty">Position no longer open.</div> : <>
          <ClosePreviewBody preview={preview} venue={venue} book={book} now={now} />
          {error && <div className="cp-warn">Book read failed: {error}. Retrying every second.</div>}
        </>}
        <div className="cp-actions">
          <button type="button" className="cp-cancel" onClick={onClose}>Cancel</button>
          <button type="button" className={"cp-confirm " + (sells ? "sell" : "buy")} disabled={!position || ticketBusy}
            onClick={close}>{sells ? "Sell" : "Buy"} to close at market</button>
        </div>
      </div>
    </div>
  );
}
