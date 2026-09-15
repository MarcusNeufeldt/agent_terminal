import { useEffect, useState } from "react";
import { api, fmt } from "../api";
import useStore from "../store";

export default function OrderBook() {
  const symbol = useStore(s => s.symbol);
  const [book, setBook] = useState({ asks: [], bids: [] });

  useEffect(() => {
    let alive = true;
    setBook({ asks: [], bids: [] });
    const poll = async () => {
      try {
        const data = await api(`/api/orderbook?symbol=${encodeURIComponent(symbol)}`);
        if (alive) setBook(data.orderBook || {});
      } catch (e) { /* best-effort */ }
    };
    poll();
    const id = setInterval(poll, 2500);
    return () => { alive = false; clearInterval(id); };
  }, [symbol]);

  const asks = (book.asks || []).slice(0, 9);
  const bids = (book.bids || []).slice(0, 9);
  const val = (x) => (Array.isArray(x) ? { price: x[0], qty: x[1] } : { price: x.price, qty: x.qty });
  const maxQty = Math.max(
    ...asks.map(a => Number(val(a).qty)), ...bids.map(b => Number(val(b).qty)), 1
  );
  const row = (p, cls) => {
    const { price, qty } = val(p);
    const w = Math.min(100, (Number(qty) / maxQty) * 100);
    const bg = `linear-gradient(to left, ${cls === "ask" ? "rgba(239,83,80,0.13)" : "rgba(38,166,154,0.13)"} ${w}%, transparent ${w}%)`;
    return (
      <div className={"book-row " + cls} style={{ background: bg }} key={price}>
        <span className="price">{fmt(price)}</span>
        <span className="qty">{fmt(qty)}</span>
      </div>
    );
  };
  const bestAsk = asks.length ? val(asks[0]).price : null;
  const bestBid = bids.length ? val(bids[0]).price : null;

  return (
    <div id="book">
      <h3>Order book</h3>
      <div className="book-rows">{asks.slice().reverse().map(a => row(a, "ask"))}</div>
      <div className="book-mid">
        <span id="book-mid-price">{bestAsk && bestBid ? fmt((Number(bestAsk) + Number(bestBid)) / 2) : "–"}</span>
        <span className="spread" id="book-spread">{bestAsk && bestBid ? fmt(Number(bestAsk) - Number(bestBid)) : ""}</span>
      </div>
      <div className="book-rows">{bids.map(b => row(b, "bid"))}</div>
    </div>
  );
}
