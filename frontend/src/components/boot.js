/* Boot sequence + SSE wiring. Non-React glue: runs once on mount. */

import { api, fmt } from "../api";
import useStore from "../store";

export async function bootTerminal() {
  const s = useStore.getState();
  try {
    const h = await api("/api/health");
    useStore.setState({ armed: h.armed, env: h.env, hasKeys: h.hasKeys });
    window.__env = h.env;
    if (!h.hasKeys) s.toast("No API keys found — private data (account, positions, trading) is unavailable.", "warn", 10000);
  } catch (e) {
    s.toast(`Server health check failed: ${e.message}`, "err");
  }

  try {
    const inst = await api("/api/instruments");
    useStore.setState({ instruments: (inst.instruments || []).filter(i => i.tradeable !== false) });
  } catch (e) {}

  try {
    const t = await api(`/api/tickers?symbols=${encodeURIComponent(s.symbol)}`);
    useStore.setState({ watchlist: t.watchlist || [], tickers: t.tickers || {} });
  } catch (e) {}

  await s.loadChart();
  await s.loadChatHistory();

  if (!useStore.getState().chat.length) {
    useStore.setState({ chat: [{ role: "assistant", content: "Live account and market context is loaded. Ask about prices, your positions or risk — or ask me to draft an order and I'll give you a fill-in ticket." }] });
  }

  s.refreshAccount();
  s.refreshTables();
  s.refreshFills();
  s.refreshSignal();

  setInterval(() => useStore.getState().advanceBuckets(useStore.getState().currentBucket()), 5000);
  setInterval(() => useStore.getState().refreshAccount(), 5000);
  setInterval(() => useStore.getState().refreshTables(), 5000);
  setInterval(() => useStore.getState().checkFills(), 5000);
  setInterval(() => useStore.getState().refreshScanner(), 30000);
  useStore.getState().pollMarketList();
  setInterval(() => useStore.getState().pollMarketList(), 10000);
  setInterval(() => useStore.getState().refreshSignal(), 60000);

  const es = new EventSource("/api/stream");
  es.addEventListener("ticker", e => { try { useStore.getState().onTicker(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("trade", e => { try { useStore.getState().onTrade(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("status", e => {
    try {
      const st = JSON.parse(e.data);
      const status = typeof st.status === "string" ? st.status : st.status && st.status.status;
      useStore.setState({ feed: status || "unknown" });
    } catch (err) {}
  });
  es.addEventListener("armed", e => { try { useStore.setState({ armed: !!JSON.parse(e.data).armed }); } catch (err) {} });
  es.addEventListener("bcandle", e => { try { useStore.getState().onBinanceCandle(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("protection_sync", e => {
    try {
      const sync = JSON.parse(e.data);
      useStore.getState().toast(
        sync.ok
          ? `${sync.kind} ${sync.symbol} auto-sized ${fmt(sync.fromSize)} → ${fmt(sync.toSize)}`
          : `${sync.kind} ${sync.symbol} auto-size failed: ${sync.error || "unknown error"}`,
        sync.ok ? "ok" : "err", 9000,
      );
      useStore.getState().refreshTables();
    } catch (err) {}
  });

  es.addEventListener("chase", e => {
    try {
      const c = JSON.parse(e.data);
      useStore.getState().onChaseEvent(c);
      if (c.status && c.status !== "running") {
        useStore.getState().toast(
          `Chase ${c.id} ${c.status}: filled ${fmt(c.filled)}/${fmt(c.size)} ${c.symbol}`,
          c.status === "filled" ? "ok" : "warn", 9000,
        );
        if (c.status === "filled" && useStore.getState().soundOn) useStore.getState().playChime();
      }
    } catch (err) {}
  });
  es.onerror = () => useStore.setState({ feed: "reconnecting" });

  // keep every open terminal tab on the same symbol — stale-tab orders are a live-money hazard
  const followSymbol = (sym, note) => {
    if (!sym || sym === useStore.getState().symbol) return;
    useStore.getState().selectSymbol(sym);
    if (note) useStore.getState().toast(`Switched to ${sym.replace("PF_", "")} — selected in another tab.`, "warn", 5000);
  };
  window.addEventListener("storage", e => { if (e.key === "kt.symbol") followSymbol(e.newValue, true); });
  window.addEventListener("focus", () => followSymbol(localStorage.getItem("kt.symbol"), false));

  // order proposals from the chat can prefill the ticket
  useStore.setState({
    fillTicket: (p) => {
      const st = useStore.getState();
      if (p.symbol && p.symbol !== st.symbol) st.selectSymbol(p.symbol.toUpperCase());
      if (p.orderType && ["mkt", "lmt", "post", "stp", "take_profit", "ioc"].includes(p.orderType)) st.setOtype(p.orderType);
      setTimeout(() => {
        const limit = document.getElementById("in-limit");
        const stop = document.getElementById("in-stop");
        const size = document.getElementById("in-size");
        if (p.limitPrice && limit) limit.value = p.limitPrice;
        if (p.stopPrice && stop) stop.value = p.stopPrice;
        if (p.size && size) size.value = p.size;
        st.toast("Proposal loaded into the order ticket. Review, then click BUY or SELL.", "", 6000);
      }, 400);
    },
  });
}
