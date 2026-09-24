/* Boot sequence + SSE wiring. Non-React glue: runs once on mount. */

import { api, apiUrl, checkExchangeSession, fmt } from "../api";
import { EXCHANGE, venueKey, reloadExchange } from "../exchange.js";
import { publishBinanceCandle } from "../market-events";
import useStore from "../store";

export async function bootTerminal(signal) {
  if (signal?.aborted) return;
  const s = useStore.getState();
  try {
    const h = await api("/api/health", { signal });
    useStore.setState({ armed: h.armed, env: h.env, hasKeys: h.hasKeys,
      exchangeRouting: h.exchangeRouting === 1, accountConfigured: h.accountConfigured === true });
    // The venue decides whether writes are possible, so read the gate before anything
    // can be submitted, and mirror it into the request layer.
    const mode = h.signedTrading && typeof h.signedTrading === "object" ? h.signedTrading.mode : "off";
    useStore.getState().setSignedTradingMode(mode);
    if (s.readOnly) {
      useStore.getState().loadHyperliquidRecovery(h.network, h.accountAddress);
      await useStore.getState().refreshHyperliquidRecovery();
    }
    window.__env = h.env;
    if (!h.hasKeys && !s.readOnly) s.toast("No API keys found — private data (account, positions, trading) is unavailable.", "warn", 10000);
  } catch (e) {
    if (signal?.aborted) return;
    s.toast(`Server health check failed: ${e.message}`, "err");
  }

  // Both venues: a Chase started before a reload keeps its status card and chart line.
  try {
    const data = await api("/api/chase", { signal });
    for (const chase of data.chases || []) useStore.getState().onChaseEvent(chase);
  } catch (e) {}

  if (!s.readOnly) try {
    const data = await api("/api/protection/alerts", { signal });
    for (const alert of data.alerts || []) useStore.getState().onProtectionAlert(alert);
    if (data.alerts?.length) s.toast(`${data.alerts.length} protection order${data.alerts.length === 1 ? "" : "s"} require reconciliation.`, "err", 15000);
  } catch {}

  try {
    const inst = await api("/api/instruments", { signal });
    useStore.setState({ instruments: (inst.instruments || []).filter(i => i.tradeable !== false) });
  } catch (e) {}

  try {
    const t = await api(`/api/tickers?symbols=${encodeURIComponent(s.symbol)}`, { signal });
    useStore.setState({ watchlist: t.watchlist || [], tickers: t.tickers || {} });
  } catch (e) {}

  if (signal?.aborted) return;
  await s.loadChart();
  if (signal?.aborted) return;
  if (!s.readOnly) await s.loadChatHistory();
  if (signal?.aborted) return;

  if (!s.readOnly && !useStore.getState().chat.length) {
    useStore.setState({ chat: [{ role: "assistant", content: "Live account and market context is loaded. Ask about prices, your positions or risk — or ask me to draft an order and I'll give you a fill-in ticket." }] });
  }

  s.refreshAccount();
  s.refreshTables();
  s.refreshFills();
  if (!s.readOnly) s.refreshSignal();

  const pollers = [];
  const every = (callback, ms) => pollers.push(setInterval(callback, ms));
  every(() => useStore.getState().advanceBuckets(useStore.getState().currentBucket()), 5000);
  every(() => useStore.getState().refreshAccount(), 5000);
  every(() => useStore.getState().refreshTables(), 5000);
  every(() => s.readOnly ? useStore.getState().refreshFills() : useStore.getState().checkFills(), 5000);
  // Position books drive net-if-closed PnL on both venues.
  useStore.getState().refreshPositionBooks();
  every(() => useStore.getState().refreshPositionBooks(), 2000);
  if (!s.readOnly) {
    every(() => useStore.getState().updateRulePeaks(), 5000);
    useStore.getState().refreshStats();
    every(() => useStore.getState().refreshStats(), 30000);
  }
  if (!s.readOnly) every(() => useStore.getState().refreshScanner(), 30000);
  useStore.getState().pollMarketList();
  every(() => useStore.getState().pollMarketList(), 10000);
  if (!s.readOnly) every(() => useStore.getState().refreshSignal(), 60000);
  if (!s.readOnly) every(() => {
    const current = useStore.getState();
    if (Object.keys(current.protectionAlerts).length && current.soundOn) current.playChime();
  }, 30000);

  const es = new EventSource(apiUrl("/api/stream"));
  es.addEventListener("exchange", e => { try { checkExchangeSession(JSON.parse(e.data)); } catch {} });
  es.addEventListener("ticker", e => { try { useStore.getState().onTicker(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("trade", e => { try { useStore.getState().onTrade(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("status", e => {
    try {
      const st = JSON.parse(e.data);
      checkExchangeSession(st);
      const status = typeof st.status === "string" ? st.status : st.status && st.status.status;
      useStore.setState({ feed: status || "unknown" });
    } catch (err) {}
  });
  es.addEventListener("armed", e => { try { useStore.setState({ armed: !!JSON.parse(e.data).armed }); } catch (err) {} });
  es.addEventListener("bcandle", e => {
    try {
      const candle = JSON.parse(e.data);
      useStore.getState().onBinanceCandle(candle);
      publishBinanceCandle(candle);
    } catch (err) {}
  });
  es.addEventListener("tp_cleanup", e => {
    try {
      const cleanup = JSON.parse(e.data);
      useStore.getState().toast(
        cleanup.status === "completed"
          ? `TP ${cleanup.symbol}: ${cleanup.cancelledCount || 0} remaining orders cancelled.`
          : `TP ${cleanup.symbol} cleanup ${cleanup.status}: ${cleanup.error || "waiting for ARM or confirmed cancellation"}`,
        cleanup.status === "completed" ? "ok" : "warn", 12000,
      );
      useStore.getState().refreshTables();
    } catch {}
  });
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
  es.addEventListener("protection_alert", e => {
    try {
      const alert = JSON.parse(e.data);
      useStore.getState().onProtectionAlert(alert);
      if (alert.status === "UNPROTECTED") {
        const current = useStore.getState();
        current.toast(`UNPROTECTED: ${alert.symbol} ${alert.kind} could not be confirmed.`, "err", 15000);
        if (current.soundOn) current.playChime();
      }
    } catch {}
  });

  es.addEventListener("chase", e => {
    try {
      const c = JSON.parse(e.data);
      useStore.getState().onChaseEvent(c);
      if (c.status && c.status !== "running") {
        useStore.getState().toast(
          `Chase ${c.id} ${c.status}: filled ${fmt(c.filled)}/${fmt(c.size)} ${c.symbol}`,
          c.status === "filled" ? "ok" : ["unknown", "orphaned"].includes(c.status) ? "err" : "warn", 9000,
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
  const followVenue = () => {
    const selected = localStorage.getItem("kt.exchange") || "kraken";
    if (selected !== EXCHANGE) { reloadExchange(selected); return false; }
    return true;
  };
  const onStorage = e => {
    if (e.key === "kt.exchange") followVenue();
    if (e.key === venueKey("kt.symbol") && followVenue()) followSymbol(e.newValue, true);
  };
  const onFocus = () => { if (followVenue()) followSymbol(localStorage.getItem(venueKey("kt.symbol")), false); };
  window.addEventListener("storage", onStorage);
  window.addEventListener("focus", onFocus);
  signal?.addEventListener("abort", () => {
    pollers.forEach(clearInterval);
    es.close();
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("focus", onFocus);
  }, { once: true });

  // order proposals from the chat can prefill the ticket
  useStore.setState({
    fillTicket: (p) => {
      const st = useStore.getState();
      st.showRight("ticket");
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
