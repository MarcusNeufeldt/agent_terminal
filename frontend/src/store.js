/* Central store: all terminal state + actions. Components subscribe with selectors;
   the chart controller registers itself here so live data can drive it imperatively. */

import { create } from "zustand";
import { api, RES_SECONDS } from "./api";
import { formatContractSize, normalizeContractSize } from "./size-precision";

let chart = null; // chart controller (set by ChartPanel on mount)
let audioCtx = null;
let lastFillSig = null;
const chartCancelPending = new Set();

const useStore = create((set, get) => ({
  // ---- state ----
  symbol: localStorage.getItem("kt.symbol") || "PI_XBTUSD",
  res: localStorage.getItem("kt.res") || "1m",
  instruments: [],
  tickers: {},
  watchlist: [],
  armed: false,
  env: "live",
  hasKeys: true,
  pro: localStorage.getItem("kt.pro") === "1",
  lev: Number(localStorage.getItem("kt.lev")) || 10,
  soundOn: localStorage.getItem("kt.sound") !== "0",
  feed: "connecting",
  tab: "positions",
  otype: "mkt",
  candles: [],
  prevPrice: null,
  account: {},
  positions: [],
  orders: [],
  fills: [],
  scannerRows: [],
  scannerMeta: "",
  scannerLoading: false,
  chat: [],
  chatBusy: false,
  actionBusy: false,
  rightCollapsed: localStorage.getItem("kt.rightCollapsed") === "1",
  marketRows: [],
  volRank: {},
  minVol: Number(localStorage.getItem("kt.minvol")) || 0,
  sortBy: localStorage.getItem("kt.sortby") || "vol24",
  volLoading: false,
  chases: {},
  toasts: [],
  lastExecution: null,
  chartSource: "",

  // ---- chart binding ----
  bindChart(controller) {
    chart = controller;
    window.__chart = controller; // debug handle
    if (controller) {
      controller.onTpDrop = (symbol, price) => get().adjustProtection(symbol, "tp", price).then(() => get().applyOverlayLines());
      controller.onProtectionDrop = ({ symbol, kind, price, pnl }) => get().adjustProtection(symbol, kind, price, pnl).then(() => get().applyOverlayLines());
      controller.onOrderCancel = order => get().cancelChartOrder(order);
    }
  },
  unbindChart() { chart = null; },

  toggleRight() {
    const rightCollapsed = !get().rightCollapsed;
    localStorage.setItem("kt.rightCollapsed", rightCollapsed ? "1" : "0");
    set({ rightCollapsed });
  },

  // ---- toasts ----
  toast(msg, kind = "", ms = 5000) {
    const id = Math.random().toString(36).slice(2);
    set(s => ({ toasts: [...s.toasts, { id, msg, kind }] }));
    setTimeout(() => set(s => ({ toasts: s.toasts.filter(t => t.id !== id) })), ms);
  },

  // ---- market data ----
  onTicker(t) {
    set(s => ({ tickers: { ...s.tickers, [t.symbol]: t } }));
    get().updatePositionCells();
    const prev = get().prevPrice;
    if (t.symbol === get().symbol && chart) chart.onPrice(Number(t.last), prev);
  },

  onTrade(trade) {
    if (trade.symbol !== get().symbol) return;
    if (get().chartSource === "binance") return; // Binance klines own the bars; Kraken prints only feed the price line
    const sec = RES_SECONDS[get().res] || 60;
    const bucket = Math.floor(trade.time / sec) * sec;
    const candles = get().candles;
    let last = candles[candles.length - 1];
    if (!last) return;
    if (bucket < last[0]) return;
    if (bucket > last[0]) get().advanceBuckets(bucket);
    const s = get();
    last = s.candles[s.candles.length - 1];
    if (last[0] !== bucket) return;
    if (last[5] === 0 && last[1] === last[4]) {
      last[1] = last[2] = last[3] = trade.price; // filler candle starts here
    } else {
      last[2] = Math.max(last[2], trade.price);
      last[3] = Math.min(last[3], trade.price);
    }
    last[4] = trade.price;
    last[5] = (last[5] || 0) + trade.qty;
    set({ candles: [...s.candles] }); // new array so subscribers see the change
    if (chart) chart.updateBar(last);
  },

  currentBucket() {
    const sec = RES_SECONDS[get().res] || 60;
    return Math.floor(Date.now() / 1000 / sec) * sec;
  },

  advanceBuckets(toBucket) {
    const sec = RES_SECONDS[get().res] || 60;
    const candles = get().candles;
    let last = candles[candles.length - 1];
    if (!last || toBucket <= last[0]) return;
    let guard = 0;
    while (last[0] + sec <= toBucket) {
      if (++guard > 500) { get().loadChart(); return; }
      const flat = [last[0] + sec, last[4], last[4], last[4], last[4], 0];
      candles.push(flat);
      if (chart) chart.appendBar(flat);
      last = flat;
    }
    set({ candles: [...candles] });
  },

  selectSymbol(sym) {
    if (!sym) return;
    set({ symbol: sym, prevPrice: null });
    localStorage.setItem("kt.symbol", sym);
    api(`/api/tickers?symbols=${encodeURIComponent(sym)}`).catch(() => {});
    get().loadChart();
    get().refreshBook?.();
    get().refreshSignal();
  },

  async adjustProtection(symbol, kind, stopPrice, previewPnl = null) {
    const label = kind === "sl" ? "Stop loss" : "Take profit";
    let pnl = Number(previewPnl);
    if (!Number.isFinite(pnl)) {
      const s = get();
      const pos = s.positions.find(p => !p.error && p.symbol === symbol);
      const inst = s.instruments.find(i => i.symbol === symbol) || {};
      if (pos) {
        const dir = String(pos.side).toLowerCase() === "short" ? -1 : 1;
        pnl = dir * (Number(stopPrice) - Number(pos.price)) * Number(pos.size) * Number(inst.contractSize || 1);
      }
    }
    const pnlText = Number.isFinite(pnl) ? ` (${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })})` : "";
    if (get().armed && !confirm(`${label} ${symbol} at ${fmt(stopPrice)}${pnlText}?\n\nThis will change a LIVE reduce-only protection order.`)) return false;
    try {
      const actionType = kind === "sl" ? "replace_sl" : "replace_tp";
      const r = await api("/api/action", { method: "POST", body: { actions: [{ type: actionType, symbol, stopPrice }] } });
      const res = (r.results || [])[0] || {};
      if (res.error) { get().toast(`${label} failed: ${res.error}`, "err"); return false; }
      if (res.simulated) { get().toast(`${label} simulated — terminal is disarmed, nothing sent.`, "warn"); return false; }
      get().toast(`${label} set at ${fmt(stopPrice)}${pnlText} — live on book.`, "ok");
      await get().refreshTables();
      return true;
    } catch (e) {
      get().toast(`${label} failed: ${e.message}`, "err");
      return false;
    }
  },

  async adjustTp(symbol, stopPrice) {
    return get().adjustProtection(symbol, "tp", stopPrice);
  },

  pollMarketList() {
    api("/api/marketlist").then(r => set({ marketRows: r.rows || [] })).catch(() => {});
  },

  setMinVol(v) {
    localStorage.setItem("kt.minvol", String(v));
    set({ minVol: Number(v) || 0 });
  },

  setSortBy(s) {
    localStorage.setItem("kt.sortby", s);
    set({ sortBy: s });
  },

  async rankByVol() {
    const s = get();
    if (s.volLoading) return;
    set({ volLoading: true });
    try {
      const r = await api(`/api/volatility?limit=400&minVolume=${encodeURIComponent(s.minVol || 0)}`);
      const rank = {};
      for (const row of r.rows || []) rank[row.symbol] = row.realizedVolatilityPercent;
      set({ volRank: rank, sortBy: "realized" });
      get().toast(`Volatility ranked — ${r.rows.length} markets (realized vol, ${r.windowMinutes}m window${r.cached ? ", cached" : ""}).`, "ok");
    } catch (e) {
      get().toast(`Volatility scan failed: ${e.message}`, "err");
    } finally {
      set({ volLoading: false });
    }
  },

  setTab(tab) {
    set({ tab });
    if (tab === "scanner") get().refreshScanner(); // legacy behavior: scan kicks off when the tab opens
  },
  setOtype(otype) { set({ otype }); },

  setRes(res) {
    set({ res });
    localStorage.setItem("kt.res", res);
    get().loadChart();
  },

  async loadChart() {
    const { symbol, res } = get();
    set({ overlayPrices: [] }); // drop previous symbol's overlays before setData
    if (chart) chart.applyPriceFormat(symbol, get().instruments);
    try {
      const data = await api(`/api/candles?symbol=${encodeURIComponent(symbol)}&res=${res}`);
      if (get().symbol !== symbol) return; // switched away mid-fetch
      const candles = data.candles || [];
      set({
        candles,
        chartSource: data.source || "none",
        overlayPrices: [],
        _bacc: null, // fresh accumulator per load
      });
      if (chart) chart.setData(candles);
      get().applyOverlayLines();
    } catch (e) {
      set({ chartNote: `Chart error: ${e.message}` });
    }
  },

  applyOverlayLines() {
    const { positions, orders, symbol } = get();
    const overlays = [];
    const pos = positions.find(p => !p.error && p.symbol === symbol && p.size);
    const inst = get().instruments.find(i => i.symbol === symbol) || {};
    const tick = Number(inst.tickSize) || 0.01;
    const mult = Number(inst.contractSize || 1);
    for (const p of positions) {
      if (!p.error && p.symbol === symbol && p.size) {
        const dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
        overlays.push({
          price: p.price, color: p.side === "long" ? "#26a69a" : "#ef5350",
          title: `${p.side} ${Number(p.size)}`, dashed: false,
          position: { symbol, entry: Number(p.price), size: Number(p.size), mult, dir, tick, side: p.side },
        });
        if (p.liqPriceEstimate) overlays.push({ price: p.liqPriceEstimate, color: "#f0b90b", title: `LIQ ${Number(p.liqPriceEstimate)}`, dashed: true });
      }
    }
    for (const o of orders) {
      if (o.error || o.symbol !== symbol) continue;
      const orderId = o.order_id || o.orderId || null;
      const order = (o.cliOrdId || orderId) ? {
        symbol: o.symbol, side: o.side, orderType: o.orderType,
        cliOrdId: o.cliOrdId || null, orderId,
        price: Number(o.stopPrice || o.limitPrice),
      } : null;
      if (o.limitPrice) {
        const size = o.size !== null && o.size !== undefined ? ` ${Number(o.size)}` : "";
        overlays.push({
          price: o.limitPrice, color: "#4f8cff", title: `${o.side} ${o.orderType}${size}`, dashed: true,
          ...(!o.stopPrice && order ? { order } : {}),
        });
      }
      if (o.stopPrice) {
        const isTp = String(o.orderType).toLowerCase() === "take_profit";
        const type = isTp ? "TP" : "SL";
        if (pos) {
          const dir = String(pos.side).toLowerCase() === "short" ? -1 : 1;
          const positionSize = Number(pos.size);
          const orderSize = Number(o.unfilledSize ?? o.size ?? positionSize);
          const coveredSize = Math.min(positionSize, orderSize);
          const coverage = positionSize > 0 ? Math.round(orderSize / positionSize * 100) : 0;
          const pnl = dir * (Number(o.stopPrice) - Number(pos.price)) * coveredSize * mult;
          overlays.push({
            price: o.stopPrice, color: isTp ? "#26a69a" : "#ef5350", dashed: true,
            title: `${type} ${Number(o.stopPrice)} (${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}) · ${coverage}%`,
            ...(isTp ? { tp: { symbol, entry: Number(pos.price), size: positionSize, mult, dir, tick, fullPosition: orderSize === positionSize } } : {}),
            ...(order ? { order } : {}),
          });
        } else {
          overlays.push({
            price: o.stopPrice, color: isTp ? "#26a69a" : "#ef5350", title: `${type} ${Number(o.stopPrice)}`, dashed: true,
            ...(order ? { order } : {}),
          });
        }
      }
    }
    set({ overlayPrices: overlays.map(o => o.price) });
    if (chart) chart.setOverlays(overlays);
  },

  // ---- account / tables ----
  proAdj(v) { return get().pro ? Number(v || 0) + 5600 : Number(v || 0); },

  async refreshAccount() {
    try {
      const a = await api("/api/account");
      if (a.error) { set({ account: {} }); return; }
      set({ account: a });
    } catch (e) {}
  },

  computeUpnl(p) {
    const s = get();
    const t = s.tickers[p.symbol];
    const mark = t && Number(t.markPrice);
    const inst = s.instruments.find(i => i.symbol === p.symbol) || {};
    const mult = Number(inst.contractSize || 1);
    const size = Number(p.size), entry = Number(p.price);
    if (!size || !entry || !mark) return Number(p.unrealizedPnl || 0);
    const dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
    return inst.type === "futures_inverse"
      ? dir * size * mult * (1 / entry - 1 / mark)
      : dir * size * mult * (mark - entry);
  },

  totalUpnl() {
    const live = get().positions.filter(p => p.symbol && !p.error);
    if (!live.length) return null;
    return live.reduce((acc, p) => acc + get().computeUpnl(p), 0);
  },

  async refreshTables() {
    try {
      const [pos, ord] = await Promise.all([api("/api/positions"), api("/api/orders")]);
      const positions = pos.positions || [];
      const orders = ord.orders || [];
      set({ positions, orders });
      const posSymbols = positions.filter(p => p.symbol && !p.error).map(p => p.symbol);
      if (posSymbols.length) api(`/api/tickers?symbols=${encodeURIComponent(posSymbols.join(","))}`).catch(() => {});
      get().updatePositionCells();
      if (!(chart && chart._drag)) get().applyOverlayLines(); // keep TP/LIQ lines in sync (skip mid-drag)
    } catch (e) {}
  },

  async refreshFills() {
    try {
      const f = await api("/api/fills");
      set({ fills: (f.fills || []).slice().sort((a, b) => new Date(b.fillTime) - new Date(a.fillTime)) });
    } catch (e) {}
  },

  // ---- scanner ----
  async refreshScanner() {
    if (get().tab !== "scanner" || get().scannerLoading) return;
    set({ scannerLoading: true });
    try {
      const data = await api("/api/volatility?limit=20");
      set({ scannerRows: data.rows || [], scannerMeta: `realized vol over ${data.windowMinutes}m closed 1m mark candles · ${data.marketsScanned} markets scanned${data.cached ? " · cached" : ""}` });
    } catch (e) {
      set({ scannerRows: [], scannerMeta: `Scan failed: ${e.message}` });
    } finally {
      set({ scannerLoading: false });
    }
  },

  // ---- signals / book ----
  async refreshSignal() { /* handled by components fetching /api/signal */ },
  refreshBook() { /* orderbook component polls itself */ },

  // ---- orders ----
  async submitOrder(side) {
    const s = get();
    if (s.otype === "chase") return get().submitChase(side);
    const body = get().ticketPayload(side);
    if (!body) return;
    try {
      const r = await api("/api/order", { method: "POST", body });
      if (r.simulated) {
        get().toast(`<b>Simulated order</b> — ${body.side} ${fmt(body.size)} ${body.symbol}. Terminal is disarmed.`, "warn", 9000);
      } else if (r.error) {
        get().toast(`<b>Order rejected by Kraken:</b> ${r.error}`, "err", 10000);
      } else {
        const st = r.response && r.response.sendStatus;
        get().toast(`Order sent: ${body.side} ${fmt(body.size)} ${body.symbol}. Status: ${String((st && st.orderEvents && st.orderEvents[0] && st.orderEvents[0].orderEvent) || "accepted")}`, "ok");
      }
      get().refreshTables(); get().refreshAccount();
    } catch (e) {
      get().toast(`Order failed: ${e.message}`, "err", 8000);
    }
  },

  ticketPayload(side) {
    const s = get();
    const sizeEl = document.getElementById("in-size");
    const rawSize = Number(sizeEl ? sizeEl.value : NaN);
    const inst = s.instruments.find(i => i.symbol === s.symbol) || {};
    const size = normalizeContractSize(rawSize, inst.contractValueTradePrecision ?? 0);
    if (!Number.isFinite(size) || size <= 0) { get().toast("Enter a size that meets the contract lot size.", "err"); return null; }
    if (sizeEl) sizeEl.value = formatContractSize(size, inst.contractValueTradePrecision ?? 0);
    const body = { symbol: s.symbol, side, orderType: s.otype, size };
    if (["lmt", "post", "ioc"].includes(s.otype)) {
      const lp = Number(document.getElementById("in-limit") ? document.getElementById("in-limit").value : NaN);
      if (!Number.isFinite(lp) || lp <= 0) { get().toast("Enter a limit price.", "err"); return null; }
      body.limitPrice = lp;
    }
    if (["stp", "take_profit"].includes(s.otype)) {
      const sp = Number(document.getElementById("in-stop") ? document.getElementById("in-stop").value : NaN);
      if (!Number.isFinite(sp) || sp <= 0) { get().toast("Enter a trigger price.", "err"); return null; }
      body.stopPrice = sp;
      body.triggerSignal = "mark";
    }
    const reduceEl = document.getElementById("in-reduce");
    if (reduceEl && reduceEl.checked) body.reduceOnly = true;
    return body;
  },

  async submitChase(side) {
    const body = get().ticketPayload(side);
    if (!body) return;
    if (!get().armed) { get().toast("CHASE requires an armed terminal — arm it first.", "warn"); return; }
    try {
      const r = await api("/api/chase", {
        method: "POST",
        body: { symbol: body.symbol, side: body.side, size: body.size },
      });
      const chase = r.chase;
      get().onChaseEvent(chase);
      get().toast(`Chase ${chase.id} running: ${side} ${fmt(body.size)} ${body.symbol}.`, "ok", 9000);
    } catch (e) {
      get().toast(`Chase failed: ${e.message}`, "err");
    }
  },

  async closePosition(symbol) {
    const s = get();
    const p = s.positions.find(x => x.symbol === symbol);
    if (!p) return;
    if (!confirm(`Close ${p.side} position on ${symbol} (${fmt(p.size)} contracts) with a reduce-only market order?`)) return;
    try {
      const r = await api("/api/order", {
        method: "POST",
        body: { symbol, orderType: "mkt", size: Number(p.size), side: p.side === "long" ? "sell" : "buy", reduceOnly: true },
      });
      if (r.simulated) s.toast("Close simulated — terminal is disarmed.", "warn");
      else s.toast("Close order sent.", r.response && r.response.result === "success" ? "ok" : "err");
      s.refreshTables(); s.refreshAccount();
    } catch (e) { s.toast(`Close failed: ${e.message}`, "err"); }
  },

  async cancelChartOrder(order) {
    const s = get();
    const key = order.cliOrdId || order.orderId;
    const target = order.cliOrdId ? { cliOrdId: order.cliOrdId } : order.orderId ? { orderId: order.orderId } : null;
    if (!target) { s.toast("Cannot cancel this chart order: missing order ID.", "err"); return false; }
    if (chartCancelPending.has(key)) return false;
    const live = s.armed ? " LIVE" : "";
    if (!confirm(`Cancel${live} ${order.orderType || "order"} ${order.side || ""} on ${order.symbol} at ${fmt(order.price)}?`)) return false;
    chartCancelPending.add(key);
    try { return await s.cancelOrder(target); }
    finally { chartCancelPending.delete(key); }
  },

  async cancelOrder(payload) {
    const s = get();
    try {
      const body = payload.cliOrdId ? { cliOrdId: payload.cliOrdId } : { orderId: payload.orderId };
      const r = await api("/api/cancel", { method: "POST", body });
      let ok = false;
      if (r.simulated) s.toast("Cancel simulated — terminal is disarmed.", "warn");
      else if (r.error) s.toast(`Cancel failed: ${r.error}`, "err");
      else {
        ok = r.response && r.response.result === "success";
        s.toast(ok ? "Order canceled." : `Cancel rejected: ${JSON.stringify((r.response && (r.response.cancelStatus || r.response)) || "").slice(0, 140)}`, ok ? "ok" : "err");
      }
      await s.refreshTables();
      return ok;
    } catch (e) {
      s.toast(`Cancel failed: ${e.message}`, "err");
      return false;
    }
  },

  async cancelAllForSymbol() {
    const s = get();
    const mine = s.orders.filter(o => o.symbol === s.symbol);
    if (!mine.length) { s.toast(`No open orders on ${s.symbol}.`); return; }
    if (!confirm(`Cancel ${mine.length} open order(s) on ${s.symbol}?`)) return;
    for (const o of mine) {
      try {
        await api("/api/cancel", { method: "POST", body: { cliOrdId: o.cliOrdId || undefined, orderId: o.order_id || undefined } });
      } catch (e) { s.toast(`Cancel failed: ${e.message}`, "err"); }
    }
    s.toast(`Cancel-all done for ${s.symbol}.`, "ok");
    s.refreshTables();
  },

  // ---- action cards ----
  async executeActions(acts, msgIdx, blockIdx = 0) {
    const s = get();
    if (s.actionBusy) return;
    if (s.armed && !confirm(`Send ${acts.length} LIVE action(s) to Kraken (${s.env})?`)) return;
    set({ actionBusy: true });
    const execStart = Date.now();
    let r;
    try {
      r = await api("/api/action", {
        method: "POST",
        body: { actions: acts, messageId: s.chat[msgIdx]?.id, blockIndex: blockIdx },
      });
    } catch (e) {
      s.toast(`Execute failed: ${e.message}`, "err");
      set({ actionBusy: false });
      return;
    }
    const sim = !r.armed;
    const executed = Array.isArray(r.actions) ? r.actions : acts;
    const results = r.results || [];
    const allFailed = results.length > 0 && results.every(res => res.error || res.ok === false);
    const lines = results.map(res => res.error ? `${res.type || "??"}: ${res.error}` : `${res.type || "??"} ok`);
    const toastLead = sim ? "<b>Simulated</b> (disarmed)" : allFailed ? "<b>Failed</b>" : "<b>Sent</b>";
    s.toast(`${toastLead} — ${lines.join(" · ")}`, sim ? "warn" : allFailed ? "err" : "ok", 9000);

    await new Promise(res => setTimeout(res, 900));
    await get().refreshTables();
    const freshOrders = get().orders;
    const afterIds = new Set(freshOrders.map(o => o.cliOrdId || o.order_id || o.orderId).filter(Boolean));
    const orderType = value => String(value || "").toLowerCase() === "stop" ? "stp" : String(value || "").toLowerCase();
    const sigOf = o => JSON.stringify([o.symbol, o.side, orderType(o.orderType), o.limitPrice ?? null, o.stopPrice ?? null]);
    const afterSigs = new Set(freshOrders.filter(o => !o.error).map(sigOf));
    const rows = [];
    const needsFills = !sim && results.some((res, i) => {
      const a = res.order ? { ...res.order, type: res.type } : (executed[i] || {});
      return !res.error && a.type === "order" && a.orderType !== "mkt";
    });
    let fills = [];
    if (needsFills) {
      try { fills = (await api("/api/fills")).fills || []; } catch (e) { fills = []; }
    }
    results.forEach((res, i) => {
      const a = res.order ? { ...res.order, type: res.type } : (executed[i] || {});
      if (res.error) { rows.push({ status: "error", primary: describeAction(a), secondary: String(res.error).slice(0, 80) }); return; }
      if (res.simulated) { rows.push({ status: "sim", primary: describeAction(a), secondary: "simulated only (disarmed)" }); return; }
      if (a.type === "order") {
        if (a.orderType === "mkt") { rows.push({ status: "ok", primary: describeAction(a), secondary: "market order sent" }); return; }
        if (afterSigs.has(sigOf(a))) { rows.push({ status: "ok", primary: describeAction(a), secondary: "live on book" }); return; }
        const fill = fills.find(x => x.symbol === a.symbol && x.side === a.side && new Date(x.fillTime).getTime() >= execStart - 3000);
        rows.push(fill
          ? { status: "ok", primary: `filled immediately: ${a.side} ${fmt(a.size)} ${a.symbol}`, secondary: `@ ${fmt(fill.price)}` }
          : { status: "warn", primary: describeAction(a), secondary: "not on book, no fill — post-only likely crossed and was rejected" });
      } else if (a.type === "ladder") {
        const want = res.orders || [];
        const got = want.filter(o => afterSigs.has(sigOf(o))).length;
        rows.push(got === want.length
          ? { status: "ok", primary: `grid live: ${got} rungs on book` }
          : { status: "warn", primary: `grid partially on book: ${got}/${want.length}` });
      } else if (a.type === "cancel" || a.type === "cancel_all") {
        const targets = (res.results || []).map(x => x.target).filter(Boolean);
        if (!targets.length) { rows.push({ status: "ok", primary: "nothing to cancel" }); return; }
        const gone = targets.every(t => !afterIds.has(t.cliOrdId || t.order_id));
        rows.push(gone
          ? { status: "ok", primary: `canceled ${targets.length} order(s) — confirmed off book` }
          : { status: "warn", primary: "some canceled orders still on book" });
      } else if (a.type === "replace_tp" || a.type === "replace_sl") {
        const protection = res.order || {};
        const label = a.type === "replace_sl" ? "SL" : "TP";
        const cancelOk = (res.cancelResults || []).every(x => !x.error);
        rows.push(cancelOk && afterSigs.has(sigOf(protection))
          ? { status: "ok", primary: `${label} replaced @ ${fmt(protection.stopPrice)}` }
          : { status: "warn", primary: `${label} replace incomplete — check Orders tab` });
      } else if (a.type === "close") {
        rows.push({ status: "ok", primary: "reduce-only market close sent" });
      } else if (a.type === "chase") {
        const chase = res.chase || {};
        rows.push({ status: "ok", primary: `maker chase ${chase.id || ""} started`, secondary: "running asynchronously; fills arrive through chase events" });
      }
    });

    const hadIssues = rows.some(x => x.status === "error" || x.status === "warn");
    const chaseStarted = !sim && results.some((res, i) => !res.error && (executed[i] || {}).type === "chase");
    const doneLabel = sim
      ? `Simulated ${executed.length} action(s) — nothing sent to Kraken`
      : allFailed
        ? `Failed ${executed.length} action(s) — nothing was accepted by Kraken`
        : chaseStarted
          ? `${hadIssues ? "Started chase with warnings" : "Maker chase started"} — monitoring asynchronously`
          : `${hadIssues ? "Completed with warnings" : "Executed"} ${executed.length} action(s) — verified on live book`;
    const trace = { working: false, doneLabel, rows, variant: "Steps" };
    set(s => {
      const chat = [...s.chat];
      const m = { ...chat[msgIdx] };
      if (m && Array.isArray(m.actionBlocks)) m.actionBlocks = m.actionBlocks.filter((_, bi) => bi !== blockIdx);
      chat[msgIdx] = m;
      chat.push({ role: "assistant", kind: "trace", trace });
      return { chat, actionBusy: false, lastExecution: { mode: sim ? "SIMULATED (terminal was disarmed — nothing was sent to Kraken)" : allFailed ? "LIVE ATTEMPT FAILED (nothing accepted)" : "LIVE (sent to Kraken)", results: lines, verification: rows, when: new Date().toISOString() } };
    });
    api("/api/chat/note", { method: "POST", body: { role: "assistant", content: doneLabel, meta: { trace } } }).catch(() => {});
    s.refreshTables(); s.refreshAccount();
  },

  // ---- chat ----
  async loadChatHistory() {
    try {
      const h = await api("/api/chat/history");
      set({
        chat: (h.messages || []).map(m => ({
          id: m.id,
          role: m.role,
          content: m.content,
          proposals: m.meta && m.meta.orderProposals,
          actionBlocks: m.meta && m.meta.actionProposals,
        })),
      });
    } catch (e) { /* fresh start */ }
  },

  async sendChat(text) {
    const s = get();
    if (!text.trim()) return;
    s.chat.push({ role: "user", content: text });
    set({ chat: [...s.chat], chatBusy: true });
    try {
      const r = await api("/api/chat", {
        method: "POST",
        body: { message: text, symbol: s.symbol, lastExecution: s.lastExecution || null },
      });
      set({ lastExecution: null });
      s.chat.push({ id: r.messageId, role: "assistant", content: r.text || "(empty response)", proposals: r.orderProposals, actionBlocks: r.actionProposals });
      set({ chat: [...s.chat] });
    } catch (e) {
      s.chat.push({ role: "assistant", content: `⚠ ${e.message}` });
      set({ chat: [...s.chat] });
    } finally {
      set({ chatBusy: false });
    }
  },

  // ---- binance live candle (SSE) ----
  onBinanceCandle(k) {
    const { symbol, res, candles } = get();
    if (k.symbol !== symbol || !candles.length) return;
    const sec = RES_SECONDS[res] || 60;
    const bucket = Math.floor(k.t / sec) * sec;
    let last = candles[candles.length - 1];
    if (bucket < last[0]) return;
    if (bucket > last[0]) get().advanceBuckets(bucket); // flat-fill idle gaps
    const cur = get().candles[get().candles.length - 1];
    const acc = get()._bacc;
    const fresh = !acc || acc.sym !== symbol || acc.bucket !== bucket || acc.res !== res;
    const existed = cur[0] === bucket;
    let bar;
    if (fresh) {
      const vBase = existed ? Math.max(0, cur[5] - k.v) : 0; // REST volume outside this minute
      bar = {
        bucket,
        res,
        sym: symbol,
        o: existed ? cur[1] : k.o,
        h: Math.max(existed ? cur[2] : k.h, k.h),
        l: Math.min(existed ? cur[3] : k.l, k.l),
        c: k.c,
        vBase,
        v: vBase + k.v,
        mins: { [k.t]: k.v },
      };
    } else {
      acc.mins[k.t] = k.v;
      bar = {
        ...acc,
        h: Math.max(acc.h, k.h),
        l: Math.min(acc.l, k.l),
        c: k.c,
        v: (acc.vBase || 0) + Object.values(acc.mins).reduce((a, b) => a + b, 0),
      };
    }
    const out = [...get().candles];
    out[out.length - 1] = [bar.bucket, bar.o, bar.h, bar.l, bar.c, bar.v];
    set({ candles: out, _bacc: bar });
    if (chart) chart.updateBar(out[out.length - 1]);
  },

  // ---- chase events (SSE) ----
  onChaseEvent(c) {
    if (!c || !c.id) return;
    set(s => ({ chases: { ...s.chases, [c.id]: { ...c, updated: Date.now() } } }));
  },

  async resetChat() {
    if (!confirm("Wipe the AI conversation? Your memory file is kept and reloads on your next message.")) return;
    try { await api("/api/chat/reset", { method: "POST", body: {} }); } catch (e) {}
    set(s => ({
      chat: [{ role: "assistant", content: "Conversation wiped. Your memory file reloaded — ask me anything." }],
      chatBusy: false,
      lastExecution: null,
    }));
    get().toast("AI conversation reset — memory kept.", "ok");
  },

  // ---- fills alert ----
  async checkFills() {
    try {
      const f = await api("/api/fills");
      const fills = (f.fills || []).filter(x => x && x.fillTime && !x.error);
      if (!fills.length) return;
      const key = (x) => JSON.stringify([x.fillTime, x.symbol, x.side, x.price, x.size || x.qty]);
      const sig = JSON.stringify(fills.map(key));
      const prev = get()._fillSig;
      if (prev === null || prev === undefined) { set({ _fillSig: sig }); return; }
      if (sig === prev) return;
      const known = new Set(JSON.parse(prev).map(JSON.stringify));
      const cutoff = Date.now() - 10 * 60 * 1000;
      const fresh = fills.filter(x => !known.has(key(x)) && new Date(x.fillTime).getTime() > cutoff);
      for (const x of fresh.slice(-3))
        get().toast(`<b>Fill:</b> ${x.side} ${fmt(x.size || x.qty)} ${x.symbol} @ ${fmt(x.price)}`, x.side === "buy" ? "ok" : "err", 8000);
      if (get().soundOn && fresh.length) get().playChime();
      set({ _fillSig: sig });
    } catch (e) {}
  },

  playChime() {
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      const now = audioCtx.currentTime;
      for (const [freq, offset] of [[880, 0], [1318.51, 0.12]]) {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(0.2, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.35);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now + offset);
        osc.stop(now + offset + 0.4);
      }
    } catch (e) {}
  },

  // ---- arm / sound / pro ----
  async armToggle() {
    const s = get();
    if (s.armed) {
      const r = await api("/api/arm", { method: "POST", body: { armed: false } }).catch(() => null);
      if (r) { set({ armed: !!r.armed }); s.toast("Disarmed — orders are simulated again.", ""); }
      return;
    }
    if (s.env === "live") {
      const answer = prompt(`This enables LIVE order entry on your ${s.env.toUpperCase()} Kraken Futures account.\nOrders you place will be real.\n\nType ARM to continue:`);
      if (answer !== "ARM") { s.toast("Arm cancelled.", ""); return; }
    }
    try {
      const r = await api("/api/arm", { method: "POST", body: { armed: true, confirm: "yes" } });
      set({ armed: !!r.armed });
      if (r.armed) s.toast("Order entry ARMED. Orders are now live.", "warn");
    } catch (e) { s.toast(`Arm failed: ${e.message}`, "err"); }
  },

  togglePro() {
    const pro = !get().pro;
    set({ pro });
    localStorage.setItem("kt.pro", pro ? "1" : "0");
    get().refreshAccount();
    get().toast(pro ? "Pro-Mode on: Balance and Avail margin shown +$5,600 (display only — orders use real margin)." : "Pro-Mode off: balances are real.", "warn", 6000);
  },

  toggleSound() {
    const soundOn = !get().soundOn;
    set({ soundOn });
    localStorage.setItem("kt.sound", soundOn ? "1" : "0");
    if (soundOn) get().playChime();
  },

  setLev(lev) {
    set({ lev });
    localStorage.setItem("kt.lev", String(lev));
  },

  sizeFromPct(pct) {
    const s = get();
    const t = s.tickers[s.symbol];
    const avail = Number((s.account.availableMargin ?? s.account.collateralValue) || 0);
    if (!t || !t.last || !avail) { s.toast("No price or margin data for sizing.", "err"); return; }
    const inst = s.instruments.find(i => i.symbol === s.symbol) || {};
    const mult = Number(inst.contractSize || 1);
    const isInverse = inst.type === "futures_inverse";
    const notional = avail * (pct / 100) * s.lev;
    const size = isInverse ? notional / mult : notional / (mult * Number(t.last));
    const el = document.getElementById("in-size");
    if (el) el.value = formatContractSize(size, inst.contractValueTradePrecision ?? 2);
  },

  updatePositionCells() { /* positions render live from the store in React */ },
}));

// describeAction used by execution reports
function fmt(x, digits) {
  if (x === null || x === undefined || Number.isNaN(Number(x))) return "–";
  const n = Number(x);
  if (digits !== undefined) return n.toFixed(digits);
  if (Math.abs(n) >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (Math.abs(n) >= 1) return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

function describeAction(a) {
  switch (a.type) {
    case "order": return `${a.side} ${fmt(a.size)} ${a.symbol} ${a.orderType}${a.limitPrice ? " @ " + fmt(a.limitPrice) : ""}${a.stopPrice ? " trg " + fmt(a.stopPrice) : ""}${a.reduceOnly ? " reduce-only" : ""}`;
    case "ladder": return `${a.side} grid $${fmt(a.notional)} × ${a.orders} orders over ${a.depthPercent}% (${a.orderType || "post"}) on ${a.symbol}`;
    case "chase": return `chase ${a.side} ${fmt(a.size)} ${a.symbol} post-only @ best ${a.side === "buy" ? "bid" : "ask"}${a.timeoutSec ? `, max ${a.timeoutSec}s` : ""}`;
    case "close": return `close ${a.percent != null ? a.percent + "%" : fmt(a.size) + " ctr"} of ${a.symbol}`;
    case "replace_tp": return `replace TP on ${a.symbol} → ${fmt(a.stopPrice)} (mark)`;
    case "replace_sl": return `replace SL on ${a.symbol} → ${fmt(a.stopPrice)} (mark)`;
    case "cancel_all": return `cancel all ${a.symbol}`;
    case "cancel": return `cancel ${String(a.cliOrdId || a.orderId || "").slice(0, 12)}`;
    default: return JSON.stringify(a).slice(0, 80);
  }
}

export default useStore;
