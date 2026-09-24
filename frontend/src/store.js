/* Central store: all terminal state + actions. Components subscribe with selectors;
   the chart controller registers itself here so live data can drive it imperatively. */

import { create } from "zustand";
import { receiptKey, readReceipt, writeReceipt, unresolvedReceipt, newCloid, hasRecoveryIdentity } from "./hyperliquid-receipt.js";
import { api, newRequestId, RES_SECONDS, setSignedTrading } from "./api";
import { formatContractSize, normalizeContractSize, compareContractSizes } from "./size-precision";
import { buildProtectionAction } from "./protection-action";
import { buildChartOverlays } from "./chart-overlays";
import { RULES, nextPeaks, peakKey, realizedEvents } from "./rules.js";
import { valuationPrice } from "./pricing.js";
import { closePreview, exitAfterFee, TAKER_FEE } from "./close-preview.js";
import { toVelaTimeframe } from "./vela-provider";
import { EXCHANGE, EXCHANGE_NAME, READ_ONLY, venueKey, isVenueSymbol, reloadExchange } from "./exchange.js";

let chart = null; // chart controller (set by ChartPanel on mount)
let audioCtx = null;
let lastFillSig = null;
const chartCancelPending = new Set();

// Peaks follow the displayed PnL, so a change to what it includes starts them fresh:
// older, higher peaks would read as an instant give-back on every open position.
const PEAKS_KEY = "kt.rulePeaks.trade";
const MAX_POSITION_BOOKS = 8;
const BOOK_STALE_MS = 15000;

// The whole trade's result if `p` were closed at market now: the book walked for the
// full size, the exit taker fee, funding settled on close, and the entry fee already
// paid (estimated at the taker rate, exact for market entries). Without a fresh book it
// uses the best bid/ask, which ignores depth but not cost.
function netIfClosed(s, p, inst) {
  const side = String(p.side).toLowerCase();
  const size = Number(p.size), entry = Number(p.price), mult = Number(inst.contractSize ?? 1);
  if (!["long", "short"].includes(side) || ![size, entry, mult].every(v => Number.isFinite(v) && v > 0)) return null;
  const feeRate = TAKER_FEE[s.exchange] ?? TAKER_FEE.kraken;
  // Kraken settles accrued funding into realized PnL on close; Hyperliquid pays it hourly.
  const funding = s.exchange === "kraken" && Number.isFinite(Number(p.unrealizedFunding)) ? Number(p.unrealizedFunding) : 0;
  const inverse = inst.type === "futures_inverse";
  const book = s.books?.[p.symbol];
  if (!inverse && book && Date.now() - book.at < BOOK_STALE_MS) {
    const preview = closePreview({ position: p, book, contractSize: mult, feeRate, funding, entryFeeRate: feeRate });
    if (preview && Number.isFinite(preview.netFull)) return preview.netFull;
  }
  const { price } = valuationPrice(s.tickers[p.symbol], { mode: "exit", side });
  if (!(Number.isFinite(price) && price > 0)) return null;
  const dir = side === "short" ? -1 : 1;
  if (inverse) return dir * size * mult * (1 / entry - 1 / price);
  const net = dir * size * mult * (price - entry) - feeRate * size * mult * (price + entry) + funding;
  return Number.isFinite(net) ? net : null;
}

const useStore = create((set, get) => ({
  // ---- state ----
  exchange: EXCHANGE,
  exchangeName: EXCHANGE_NAME,
  exchangeRouting: false,
  exchangeBusy: false,
  readOnly: READ_ONLY,
  signedTrading: "off",
  canTrade: !READ_ONLY,
  hlReceipt: null,
  hlCloseDraft: null,
  hlCancelReceipt: null,
  hlCancelHistory: [],
  hlCancelHistoryMore: false,
  hlCancelHistoryNote: null,
  hlCancelChecking: false,
  hlReceiptKey: null,
  hlRecoveryError: null,
  hlReconciling: false,
  hlFillBusy: false,
  hlRecoveryLoaded: false,
  hlServerUnresolved: [],
  hlRecoveryHasMore: false,
  accountConfigured: false,
  symbol: localStorage.getItem(venueKey("kt.symbol")) || (READ_ONLY ? "HL_BTC" : "PF_XBTUSD"),
  res: localStorage.getItem(venueKey("kt.res")) || "1m",
  instruments: [],
  tickers: {},
  watchlist: [],
  armed: false,
  env: "live",
  hasKeys: true,
  pro: !READ_ONLY && localStorage.getItem("kt.pro") === "1",
  lev: Number(localStorage.getItem(venueKey("kt.lev"))) || 10,
  soundOn: localStorage.getItem("kt.sound") !== "0",
  feed: "connecting",
  tab: "positions",
  otype: READ_ONLY ? "lmt" : "mkt",
  candles: [],
  prevPrice: null,
  account: {},
  positions: [],
  // Full order books for open positions: {symbol: {bids, asks, at}}. They value a
  // position at what a market close would really return, not at the best price.
  books: {},
  orders: [],
  dataStatus: {},
  ticketBusy: false,
  bulkBusy: false,
  gridPreview: null,
  gridSeed: null,
  gridRequest: null,
  gridResult: null,
  fills: [],
  realizedRecent: [],
  statsState: "loading",
  rulePeaks: (() => {
    try {
      // Net peaks. The old "kt.rulePeaks" held gross values that net PnL can never
      // reach, which would read as an instant give-back on every open position.
      const stored = JSON.parse(localStorage.getItem(PEAKS_KEY) || "{}");
      return stored && typeof stored === "object" ? stored : {};
    } catch { return {}; }
  })(),
  scannerRows: [],
  scannerMeta: "",
  scannerLoading: false,
  chat: [],
  chatBusy: false,
  actionBusy: false,
  rightCollapsed: localStorage.getItem("kt.rightCollapsed") === "1",
  rightView: "ticket",
  marketRows: [],
  volRank: {},
  minVol: Number(localStorage.getItem("kt.minvol")) || 0,
  sortBy: localStorage.getItem("kt.sortby") || "vol24",
  volLoading: false,
  chases: {},
  protectionAlerts: {},
  toasts: [],
  lastExecution: null,
  chartSource: READ_ONLY ? "hyperliquid" : "",

  // Single writer for the trading gate: the request layer and the UI must never
  // disagree about whether this venue can be written to.
  setSignedTradingMode(mode) {
    const resolved = setSignedTrading(mode);
    set({ signedTrading: resolved, canTrade: !READ_ONLY || resolved !== "off" });
    return resolved;
  },

  async switchExchange(exchange) {
    const s = get();
    if (!s.exchangeRouting || s.exchangeBusy || exchange === s.exchange) return;
    if (s.ticketBusy || s.bulkBusy || s.chatBusy || s.actionBusy || s.hlFillBusy || s.hlReconciling) {
      s.toast("Wait for the current request to finish before switching exchanges.", "warn");
      return;
    }
    const name = exchange === "kraken" ? "Kraken Futures" : "Hyperliquid";
    if (!confirm(`Switch to ${name}?\n\nThe terminal will be DISARMED and ticket drafts cleared. Open ${s.exchangeName} orders and positions stay live. Automatic protection resizing and TP cleanup pause while disarmed. Active Chase workers must finish first.`)) return;
    set({ exchangeBusy: true });
    try {
      const result = await api("/api/exchange", { method: "POST", body: { exchange } });
      if (result.exchange !== exchange || result.armed !== false || result.exchangeRouting !== 1) {
        throw new Error("Exchange switch was not confirmed. Reload to reconcile the current selection.");
      }
      reloadExchange(exchange);
    } catch (error) {
      s.toast(`Exchange switch failed: ${error.message}`, "err", 12000);
    } finally {
      set({ exchangeBusy: false });
    }
  },

  // ---- chart binding ----
  bindChart(controller) {
    chart = controller;
    window.__chart = controller; // debug handle
    if (controller) {
      controller.onTpDrop = ({ symbol, price, order }) => get().adjustProtection(symbol, "tp", price, null, order).then(() => get().applyOverlayLines());
      controller.onProtectionDrop = ({ symbol, kind, price, pnl, order, positionSnapshot }) => get().adjustProtection(symbol, kind, price, pnl, order, positionSnapshot).then(() => get().applyOverlayLines());
      controller.onOrderCancel = order => get().cancelChartOrder(order);
    }
  },
  unbindChart() { chart = null; },

  showRight(rightView) {
    localStorage.setItem("kt.rightCollapsed", "0");
    set({ rightView, rightCollapsed: false });
  },

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
    if (!isVenueSymbol(t?.symbol) || t.exchange && t.exchange !== EXCHANGE) return;
    set(s => ({ tickers: { ...s.tickers, [t.symbol]: t } }));
    get().updatePositionCells();
    const prev = get().prevPrice;
    const price = Number(t.last);
    if (t.symbol === get().symbol && chart && Number.isFinite(price) && price > 0) chart.onPrice(price, prev);
  },

  onTrade(trade) {
    if (trade.symbol !== get().symbol) return;
    if (["binance", "hyperliquid"].includes(get().chartSource)) return; // Source candles own bars; trades must not double-count volume.
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
    if (!isVenueSymbol(sym)) return;
    set({ symbol: sym, prevPrice: null, hlCloseDraft: sym === get().symbol ? get().hlCloseDraft : null });
    localStorage.setItem(venueKey("kt.symbol"), sym);
    api(`/api/tickers?symbols=${encodeURIComponent(sym)}`).catch(() => {});
    if (chart?.ownsData) chart.setMarket({ symbol: sym }).catch(e => get().toast(`Chart error: ${e.message}`, "err"));
    else get().loadChart();
    get().refreshBook?.();
    get().refreshSignal();
  },

  async adjustProtection(symbol, kind, stopPrice, previewPnl = null, order = null, positionSnapshot = null) {
    if (get().exchange === "hyperliquid") return get().submitHyperliquidProtection(symbol, kind, stopPrice, order, positionSnapshot);
    const label = kind === "sl" ? "Stop loss" : "Take profit";
    let pnl = Number(previewPnl);
    if (!Number.isFinite(pnl)) {
      const s = get();
      const pos = s.positions.find(p => !p.error && p.symbol === symbol);
      const inst = s.instruments.find(i => i.symbol === symbol) || {};
      if (pos) {
        pnl = exitAfterFee({ dir: String(pos.side).toLowerCase() === "short" ? -1 : 1, entry: pos.price, price: stopPrice,
          size: pos.size, mult: inst.contractSize || 1, feeRate: TAKER_FEE.kraken })?.net;
      }
    }
    // Chart drags pass the after-fee value; slippage past the trigger is not included.
    const pnlText = Number.isFinite(pnl) ? ` (${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} after fee, before slippage)` : "";
    if (get().armed && !confirm(`${label} ${symbol} at ${fmt(stopPrice)}${pnlText}?\n\nThis will change a LIVE reduce-only protection order.`)) return false;
    try {
      const action = buildProtectionAction(kind, symbol, stopPrice, order);
      const r = await api("/api/action", { method: "POST", body: { actions: [action], requestId: newRequestId() } });
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

  canHyperliquidChart() {
    const s = get();
    return s.exchange === "hyperliquid" && s.canTrade && !s.ticketBusy && !s.bulkBusy && !s.hlReconciling &&
      !s.hlFillBusy && s.dataStatus.orders?.state === "current" && s.dataStatus.positions?.state === "current" &&
      s.hlRecoveryLoaded && !s.hlRecoveryError && !!s.hlReceiptKey && !s.hlRecoveryHasMore &&
      !s.hlServerUnresolved.length && !unresolvedReceipt(s.hlReceipt);
  },

  async submitHyperliquidProtection(symbol, kind, price, order = null, positionSnapshot = null,
    fullPosition = !order || order.snapshot?.positionTpsl === true) {
    const s = get();
    if (!s.canHyperliquidChart() || !isVenueSymbol(symbol) || !["tp", "sl"].includes(kind) || !(price > 0) || !Number.isFinite(price)) {
      s.toast("Chart protection needs current data, an enabled trading gate and no unresolved request.", "warn"); return false;
    }
    const matches = s.positions.filter(p => p.symbol === symbol && !p.error);
    if (matches.length !== 1) { s.toast("A current unambiguous position is required.", "warn"); return false; }
    const position = positionSnapshot || order?.positionSnapshot || matches[0];
    if (position.symbol !== symbol) return false;
    const target = order ? order.snapshot : null;
    if (order && (!target || String(target.order_id) !== String(order.orderId) || target.symbol !== symbol ||
        target.reduceOnly !== true || target.triggerKind !== kind || typeof target.triggerMarket !== "boolean")) {
      s.toast("Refresh the chart before moving this exact trigger.", "warn"); return false;
    }
    const existing = s.orders.filter(o => o.symbol === symbol && o.reduceOnly === true && o.orderType === (kind === "sl" ? "stp" : "take_profit"));
    if (!target && existing.length) {
      s.toast("Protection already exists. Drag its individual TP/SL line; partial ladders are not replaced together.", "warn"); return false;
    }
    if (fullPosition && target && !target.positionTpsl && existing.length !== 1) {
      s.toast("Multiple same-kind exits exist. Partial ladders are not converted automatically.", "warn"); return false;
    }
    const quantity = fullPosition ? position.sizeExact : target?.unfilledSizeExact || position.sizeExact;
    if (!(Number(quantity) > 0)) return false;
    const risk = target ? `Hyperliquid ALWAYS places the replacement even if the original fills or disappears during this request. Prior fills may therefore leave an additional reduce-only exit ${fullPosition ? "covering the position at trigger time" : "for this quantity"}. It cannot open/increase a position.`
      : "Creates a full-size reduce-only market trigger. Fills are not guaranteed.";
    const sizing = fullPosition ? " Entire-position mode: Hyperliquid follows future position increases and decreases, even while this terminal is closed. The displayed quantity and profit are current estimates, not a fixed exit size." : " Fixed-size protection; future position increases are not covered.";
    const limit = (target && !target.triggerMarket ? ` Stop-limit price stays ${target.limitPrice}.` : "") + sizing;
    const result = exitAfterFee({ dir: position.side === "short" ? -1 : 1, entry: position.price, price, size: quantity,
      feeRate: TAKER_FEE.hyperliquid });
    const outcome = result && Number.isFinite(result.net)
      ? `\n\nIf filled at ${price}: ${result.net >= 0 ? "+" : "-"}$${Math.abs(result.net).toFixed(2)} after the ${(TAKER_FEE.hyperliquid * 100).toFixed(3)}% taker fee ($${result.fee.toFixed(2)}), before slippage.`
      : "";
    if (!confirm(`${s.armed ? "LIVE" : "SIMULATED"} ${kind.toUpperCase()} ${symbol}: ${quantity} contracts at ${price}. ${target ? `Move exact order ${target.order_id}.` : "Create protection."}${outcome}\n\n${risk}${limit}\n\nContinue?`)) return false;
    const requestId = newRequestId(), cloid = newCloid();
    const body = { requestId, cloid, symbol, kind, price, expectedArmed: s.armed,
      position: { side: position.side, sizeExact: position.sizeExact, price: position.price },
      target: target ? { ...target } : null, acknowledgeReplacement: !!target,
      fullPosition, acknowledgeFullPosition: fullPosition };
    const pending = { version: 1, kind: "chart", requestId, cloid, body, outcome: "pending", uncertain: true, createdAt: new Date().toISOString() };
    try { writeReceipt(s.hlReceiptKey, pending); }
    catch (error) { set({ hlRecoveryError: error.message }); s.toast("Cannot save chart recovery identity. Nothing sent.", "err"); return false; }
    set({ ticketBusy: true, hlReceipt: pending });
    get().applyOverlayLines();
    try {
      const result = await api("/api/chart-order", { method: "POST", body });
      const receipt = { ...pending, ...result, uncertain: !["confirmed", "rejected", "simulated"].includes(result.outcome) };
      writeReceipt(s.hlReceiptKey, receipt);
      set({ hlReceipt: receipt });
      s.toast(result.simulated ? "Chart protection simulated; no exchange change."
        : result.outcome === "confirmed" ? "Chart protection accepted. Refreshing authoritative orders."
        : `Chart result ${result.outcome || "unknown"}. ${result.error || "Check the saved client ID after the request expires; do not retry."}`,
        result.outcome === "confirmed" ? "ok" : "warn", 12000);
      return result.outcome === "confirmed";
    } catch (error) {
      const rejected = error.data?.outcome === "rejected";
      const receipt = { ...pending, outcome: rejected ? "rejected" : "unknown", uncertain: !rejected, error: error.message };
      set({ hlReceipt: receipt });
      try { writeReceipt(s.hlReceiptKey, receipt); } catch {}
      s.toast(`Chart ${receipt.outcome}: ${error.message}. No automatic retry.`, "err", 12000);
      return false;
    } finally {
      set({ ticketBusy: false });
      await get().refreshHyperliquidRecovery();
      await get().refreshTables();
      get().applyOverlayLines();
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

  openGrid(symbol) {
    if (get().ticketBusy) return;
    const position = get().positions.find(p => !p.error && p.symbol === symbol);
    get().selectSymbol(symbol);
    get().showRight("ticket");
    set({ otype: "grid", hlCloseDraft: null, gridSeed: position ? {
      id: Date.now(), symbol, side: position.side === "short" ? "sell" : "buy", size: position.size,
    } : null });
  },

  setGridPreview(plan) {
    set({ gridPreview: plan });
    get().applyOverlayLines();
  },

  async submitGrid(action, preview) {
    const s = get();
    if (s.ticketBusy || s.bulkBusy || s.symbol !== action.symbol) return;
    const prior = s.gridRequest;
    const replay = prior?.previewHash === preview.plan.previewHash && prior.expectedArmed === preview.armed;
    if (!replay && s.armed !== preview.armed) { s.toast("ARM state changed. Refresh the grid preview.", "warn"); return; }
    let body = replay
      ? prior
      : { ...action, previewHash: preview.plan.previewHash, expectedArmed: preview.armed, requestId: newRequestId() };
    if (s.readOnly) {
      // Hyperliquid signs the whole grid as one batch, so every rung's client id has
      // to be durable before anything leaves: a lost response is resolved by looking
      // those identities up, never by sending the grid a second time. A replay keeps
      // the ids it already recorded, for the same reason.
      if (!replay && unresolvedReceipt(s.hlReceipt)) {
        s.toast("Resolve the unfinished Hyperliquid submission before placing a grid.", "err", 12000);
        return;
      }
      if (!replay) {
        const rungs = preview.plan.orders?.length ?? 0;
        if (rungs < 2 || rungs > 20) { s.toast("A Hyperliquid grid needs 2 to 20 rungs.", "err"); return; }
        body = { ...body, cloids: Array.from({ length: rungs }, () => newCloid()) };
      }
      const pending = { version: 1, requestId: body.requestId, batch: true, cloids: body.cloids,
        body, outcome: "pending", uncertain: true, createdAt: new Date().toISOString() };
      try {
        writeReceipt(s.hlReceiptKey, pending);
      } catch (error) {
        set({ hlRecoveryError: error.message });
        s.toast(`Grid not sent: ${error.message}`, "err", 12000);
        return;
      }
      set({ hlReceipt: pending });
    }
    set({ ticketBusy: true, gridRequest: body, gridResult: null });
    try {
      const response = await api("/api/grid", { method: "POST", body });
      const result = response.results?.[0] || { outcome: "unknown", error: response.error || "No execution result returned." };
      if (get().readOnly) get().settleGridReceipt(body, result);
      set({ gridResult: { ...result, previewHash: body.previewHash, symbol: body.symbol } });
      get().setGridPreview(null);
      get().toast(result.simulated ? "Grid simulated. No orders sent."
        : result.outcome === "confirmed" ? `${result.responses?.length || 0} grid placements confirmed.`
          : `Grid ${result.outcome}: ${result.error || "Check the rung results before another submission."}`,
      result.simulated ? "warn" : result.outcome === "confirmed" ? "ok" : "err", 10000);
      await Promise.all([get().refreshTables(), get().refreshAccount()]);
    } catch (error) {
      if (get().readOnly) {
        const rejected = error.data?.outcome === "rejected";
        get().settleGridReceipt(body, { outcome: rejected ? "rejected" : "unknown", error: error.message });
      }
      set({ gridResult: { outcome: "unknown", error: error.message, transportError: true,
        previewHash: body.previewHash, symbol: body.symbol } });
      get().toast("Grid response unavailable. Check submission to reuse the same request ID; do not blindly place again.", "err", 12000);
    } finally {
      set({ ticketBusy: false });
    }
  },

  // Record what the venue said about a batch against its stored identities. The
  // pending receipt already blocks a resubmission, so a failed write here is safe.
  settleGridReceipt(body, result) {
    const s = get();
    const outcome = result.simulated ? "simulated" : result.outcome || "unknown";
    const receipt = { version: 1, requestId: body.requestId, batch: true, cloids: body.cloids, body,
      outcome, uncertain: !["confirmed", "rejected", "simulated"].includes(outcome),
      error: result.error || null, createdAt: s.hlReceipt?.createdAt || new Date().toISOString() };
    set({ hlReceipt: receipt });
    try { writeReceipt(s.hlReceiptKey, receipt); } catch { /* the pending receipt still guards */ }
  },

  setRes(res) {
    set({ res });
    localStorage.setItem(venueKey("kt.res"), res);
    if (chart?.ownsData) chart.setMarket({ timeframe: toVelaTimeframe(res) }).catch(e => get().toast(`Chart error: ${e.message}`, "err"));
    else get().loadChart();
  },

  async loadChart() {
    const { symbol, res } = get();
    if (chart?.ownsData) {
      await chart.setMarket({ symbol, timeframe: toVelaTimeframe(res) });
      return;
    }
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
    const { positions, orders, instruments, symbol, readOnly, canTrade, dataStatus, bulkBusy } = get();
    const symbols = new Set([symbol, ...(chart?.symbols?.() || [])]);
    const bySymbol = Object.fromEntries(
      [...symbols].map(current => [current, buildChartOverlays(current, positions, orders, instruments, readOnly, canTrade && dataStatus.orders?.state === "current" && !bulkBusy, get().canHyperliquidChart())]),
    );
    const preview = get().gridPreview;
    if (preview && bySymbol[preview.symbol]) {
      bySymbol[preview.symbol].push(...preview.orders.map((order, i) => ({
        key: `grid-preview:${i}`, price: order.limitPrice, color: "#f0b90b", dashed: true,
        title: `PREVIEW ${i + 1} · ${order.side} ${order.size} @ ${order.limitPrice}`,
      })));
    }
    const overlays = bySymbol[symbol] || [];
    set({ overlayPrices: overlays.map(line => line.price) });
    if (chart?.setOverlayMap) chart.setOverlayMap(bySymbol);
    else chart?.setOverlays(overlays);
  },

  // ---- account / tables ----
  proAdj(v) { return get().pro ? Number(v || 0) + 950 : Number(v || 0); },

  async refreshAccount() {
    try {
      const a = await api("/api/account");
      const { state, error, ageSeconds, ...account } = a;
      set(s => ({
        account: Object.keys(account).length ? account : s.account,
        dataStatus: { ...s.dataStatus, account: { state, error, ageSeconds } },
      }));
    } catch (e) {
      set(s => ({ dataStatus: { ...s.dataStatus, account: { state: "unavailable", error: e.message } } }));
    }
  },

  // One basis everywhere: the side of the book this position closes into, so the
  // number on screen is what closing right now would actually realise. "mid" stays
  // available for callers that want the untraded middle.
  // Default "net": what closing at market right now would add to the balance. The
  // book is walked for the full size, the taker fee is paid, and Kraken's unsettled
  // funding is realized. "gross" is the old best-bid/ask value, "mid" the book middle.
  computeUpnl(p, { mode = "net" } = {}) {
    const s = get();
    if (!p?.symbol || p.error) return null;
    const inst = s.instruments.find(i => i.symbol === p.symbol);
    if (!inst) return null;
    if (mode === "net") return netIfClosed(s, p, inst);
    const side = String(p.side).toLowerCase();
    // Value against the live book, not the tape. On thin pairs the last trade
    // drifts outside the bid/ask and invents PnL that could never be realised;
    // the book keeps updating even when nothing trades. Falls back to last only
    // when the book is unusable, never to mark, which is not a tradeable price.
    const { price } = valuationPrice(s.tickers[p.symbol], { mode: mode === "gross" ? "exit" : mode, side });
    const mult = Number(inst.contractSize ?? 1);
    const size = Number(p.size), entry = Number(p.price);
    if (![price, mult, size, entry].every(v => Number.isFinite(v) && v > 0)
      || !["long", "short"].includes(side)) return null;
    const dir = side === "short" ? -1 : 1;
    const pnl = inst.type === "futures_inverse"
      ? dir * size * mult * (1 / entry - 1 / price)
      : dir * size * mult * (price - entry);
    return Number.isFinite(pnl) ? pnl : null;
  },

  totalUpnl() {
    const s = get();
    if (s.dataStatus.positions?.state !== "current") return null;
    const values = s.positions.map(p => s.computeUpnl(p));
    const total = values.every(Number.isFinite) ? values.reduce((sum, pnl) => sum + pnl, 0) : null;
    return Number.isFinite(total) ? total : null;
  },

  async refreshTables() {
    try {
      const [pos, ord] = await Promise.all([api("/api/positions"), api("/api/orders")]);
      const positions = pos.positions || [];
      const orders = ord.orders || [];
      set(s => ({
        positions,
        orders,
        dataStatus: {
          ...s.dataStatus,
          positions: { state: pos.state, error: pos.error, ageSeconds: pos.ageSeconds },
          orders: { state: ord.state, error: ord.error, ageSeconds: ord.ageSeconds },
        },
      }));
      const posSymbols = positions.filter(p => p.symbol && !p.error).map(p => p.symbol);
      if (posSymbols.length) api(`/api/tickers?symbols=${encodeURIComponent(posSymbols.join(","))}`).catch(() => {});
      get().updatePositionCells();
      if (!(chart && chart._drag)) get().applyOverlayLines(); // keep TP/LIQ lines in sync (skip mid-drag)
    } catch (e) {
      set(s => ({ dataStatus: { ...s.dataStatus,
        positions: { state: "unavailable", error: e.message },
        orders: { state: "unavailable", error: e.message },
      } }));
    }
  },

  async refreshFills() {
    try {
      const f = await api("/api/fills");
      set(s => ({ fills: (f.fills || []).slice().sort((a, b) => new Date(b.fillTime) - new Date(a.fillTime)),
        dataStatus: { ...s.dataStatus, fills: { state: f.state || "current", error: f.error, ageSeconds: f.ageSeconds } },
      }));
    } catch (e) {
      set(s => ({ dataStatus: { ...s.dataStatus, fills: { state: "unavailable", error: e.message } } }));
    }
  },

  // ---- discipline rules (display only: never places or cancels an order) ----

  // Peaks ratchet per position and persist, so a page reload does not reset a trail
  // that is already armed. Keyed on entry price, so scaling in starts a fresh peak.
  updateRulePeaks() {
    const s = get();
    if (s.dataStatus.positions?.state !== "current") return;
    const entries = s.positions
      .filter(p => p && !p.error && Number(p.size) > 0)
      .map(p => ({ key: peakKey(p), upnl: s.computeUpnl(p) }));
    const peaks = nextPeaks(s.rulePeaks, entries);
    set({ rulePeaks: peaks });
    try { localStorage.setItem(PEAKS_KEY, JSON.stringify(peaks)); } catch { /* private mode or quota */ }
  },

  // Realized ledger lines drive the post-loss cooldown. The ledger is tens of
  // thousands of rows, so it is reduced to the handful of recent events here, once
  // per poll -- never in render, which runs on every ticker tick. The raw rows are
  // deliberately not stored.
  //
  // On failure the previous events are kept and the state is marked unavailable, so
  // the panel says "unknown" rather than reporting "no cooldown" when it could not look.
  async refreshStats() {
    try {
      const r = await api("/api/stats");
      if (r.error) { set({ statsState: "unavailable" }); return; }
      const events = realizedEvents(Array.isArray(r.rows) ? r.rows : [], {
        sinceMs: Date.now() - RULES.cooldownMs * 2,
      });
      set({ realizedRecent: events, statsState: "current" });
    } catch {
      set({ statsState: "unavailable" });
    }
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

  // Hyperliquid only writes bid/ask into a ticker once that symbol's book has been
  // fetched, so a position in a symbol you are not looking at would be valued off a
  // last trade that can be minutes old on a thin coin. Fetching the book publishes a
  // ticker over SSE, which keeps the exit price a real one. The selected symbol is
  // already polled by the order book component.
  // Books for the largest open positions, both venues. A failed read keeps the last
  // book; netIfClosed stops using it once it is stale.
  async refreshPositionBooks() {
    const s = get();
    const ranked = s.positions
      .filter(p => p && !p.error && typeof p.symbol === "string" && p.symbol)
      .map(p => ({ symbol: p.symbol, notional: Math.abs(Number(p.size) * Number(p.price)) || 0 }))
      .sort((a, b) => b.notional - a.notional);
    const symbols = [...new Set(ranked.map(r => r.symbol))].slice(0, MAX_POSITION_BOOKS);
    const fetched = await Promise.all(symbols.map(symbol =>
      api(`/api/orderbook?symbol=${encodeURIComponent(symbol)}`)
        .then(r => (r?.orderBook ? [symbol, { bids: r.orderBook.bids, asks: r.orderBook.asks, at: Date.now() }] : null))
        .catch(() => null)));
    const fresh = fetched.filter(Boolean);
    if (!fresh.length) return;
    const open = new Set(ranked.map(r => r.symbol));
    set(state => ({ books: Object.fromEntries([
      ...Object.entries(state.books).filter(([symbol]) => open.has(symbol)),
      ...fresh,
    ]) }));
  },

  // ---- orders ----
  async submitOrder(side) {
    const s = get();
    if (s.ticketBusy) return;
    set({ ticketBusy: true });
    try {
      if (s.otype === "chase") return await get().submitChase(side);
      const body = get().ticketPayload(side);
      if (!body) return;
      const r = await api("/api/order", { method: "POST", body: { ...body, requestId: newRequestId() } });
      if (r.simulated) {
        get().toast(`<b>Simulated order</b> — ${body.side} ${fmt(body.size)} ${body.symbol}. Terminal is disarmed.`, "warn", 9000);
      } else if (r.outcome !== "confirmed") {
        get().toast(`<b>Order ${r.outcome || "failed"}:</b> ${r.error || "Kraken did not confirm the write."}`, "err", 10000);
      } else {
        get().toast(`Order confirmed: ${body.side} ${fmt(body.size)} ${body.symbol}.`, "ok");
      }
      get().refreshTables(); get().refreshAccount();
    } catch (e) {
      get().toast(`Order failed: ${e.message}`, "err", 8000);
    } finally {
      set({ ticketBusy: false });
    }
  },

  loadHyperliquidRecovery(network, account) {
    try {
      const key = receiptKey(network, account);
      set({ hlReceiptKey: key, hlReceipt: readReceipt(key), hlRecoveryError: null });
    } catch (error) {
      set({ hlReceiptKey: null, hlRecoveryError: error.message });
    }
  },

  async refreshHyperliquidRecovery() {
    try {
      const result = await api("/api/execution-recovery");
      if (result.state !== "current" || !Array.isArray(result.items)) throw new Error("Recovery journal unavailable");
      set({ hlRecoveryLoaded: true, hlServerUnresolved: result.items, hlRecoveryHasMore: !!result.hasMore });
    } catch (error) {
      set({ hlRecoveryLoaded: false });
      get().toast(`Recovery journal unavailable: ${error.message}`, "warn");
    }
  },

  restoreHyperliquidRequest(item) {
    const s = get();
    if (s.hlFillBusy || s.hlReconciling || s.ticketBusy) return;
    if (!hasRecoveryIdentity(item) || !s.hlReceiptKey) {
      s.toast("This older submission has no recoverable client ID; manual investigation is required.", "err");
      return;
    }
    if (unresolvedReceipt(s.hlReceipt) && s.hlReceipt.requestId !== item.requestId) {
      s.toast("Resolve the current saved submission first.", "warn");
      return;
    }
    try {
      const receipt = { ...item, version: 1 };
      writeReceipt(s.hlReceiptKey, receipt);
      set({ hlReceipt: receipt });
    } catch (error) {
      set({ hlRecoveryError: error.message });
    }
  },

  async checkHyperliquidLifecycle() {
    const s = get();
    const receipt = s.hlReceipt;
    if (s.hlReconciling || s.hlFillBusy || s.ticketBusy || !receipt?.requestId) return;
    set({ hlReconciling: true });
    try {
      const report = await api(`/api/order-lifecycle?requestId=${encodeURIComponent(receipt.requestId)}`);
      if (report.state !== "current" || report.requestId !== receipt.requestId) {
        throw new Error("Lifecycle response does not match the submission");
      }
      const updated = { ...receipt, lifecycleReport: report };
      writeReceipt(s.hlReceiptKey, updated);
      set({ hlReceipt: updated });
    } catch (error) {
      const updated = { ...receipt, lifecycleReport: { state: "unavailable", reason: error.message } };
      set({ hlReceipt: updated });
      try { writeReceipt(s.hlReceiptKey, updated); } catch {}
      s.toast(`Lifecycle unavailable: ${error.message}`, "err");
    } finally {
      set({ hlReconciling: false });
    }
  },

  async loadHyperliquidOrderFills() {
    const s = get();
    const receipt = s.hlReceipt;
    if (s.hlFillBusy || s.hlReconciling || s.ticketBusy || !receipt?.status?.order_id) return;
    const startTime = Math.max(0, Date.parse(receipt.createdAt) - 5000);
    const endTime = Date.now();
    if (!Number.isSafeInteger(startTime) || startTime > endTime) {
      s.toast("Submission timestamp is unavailable; cannot choose a safe fill-history window.", "err");
      return;
    }
    set({ hlFillBusy: true });
    try {
      const scan = await api("/api/fill-history/sync", { method: "POST", body: { startTime, endTime } });
      const totals = await api(`/api/fill-history?orderId=${encodeURIComponent(receipt.status.order_id)}`);
      if (totals.state !== "current" || totals.orderId !== receipt.status.order_id ||
          (totals.fillCount > 0 && (totals.symbol !== receipt.body.symbol || totals.side !== receipt.body.side))) {
        throw new Error("Stored fill identity does not match this order");
      }
      const updated = { ...receipt, fillSummary: { ...totals, scan }, lifecycleReport: null };
      writeReceipt(s.hlReceiptKey, updated);
      set({ hlReceipt: updated });
      s.toast(scan.scanComplete ? "Available fills loaded. Exchange retention limits still apply."
        : "Fill scan is incomplete. Stored totals contain observed fills only.", "warn", 10000);
    } catch (error) {
      s.toast(`Fill history unavailable: ${error.message}`, "err");
    } finally {
      set({ hlFillBusy: false });
    }
  },

  async reconcileHyperliquidOrder() {
    const s = get();
    if (s.ticketBusy || s.hlReconciling || s.hlFillBusy || !hasRecoveryIdentity(s.hlReceipt) || !s.hlReceiptKey) return;
    set({ hlReconciling: true });
    try {
      const response = await api("/api/order-reconcile", { method: "POST", body: { requestId: s.hlReceipt.requestId } });
      if (s.hlReceipt.kind === "leverage") {
        const capacity = response.capacity;
        if (response.kind !== "leverage" || response.requestId !== s.hlReceipt.requestId || response.outcome !== "reconciled" ||
            response.state !== "current" || capacity?.symbol !== s.hlReceipt.body.symbol ||
            !Number.isSafeInteger(response.expiresAfter) || !Number.isFinite(response.exchangeTime) ||
            response.exchangeTime <= response.expiresAfter + 2000 || response.canReplace !== false ||
            receiptKey(capacity.network, capacity.accountAddress) !== s.hlReceiptKey ||
            capacity.leverage?.value !== s.hlReceipt.body.leverage ||
            capacity.leverage?.type !== (s.hlReceipt.body.cross ? "cross" : "isolated")) {
          throw new Error("Requested leverage has not been confirmed by readback. Do not retry it.");
        }
        const receipt = { ...s.hlReceipt, outcome: "reconciled", uncertain: false, error: null, leverageEvidence: response };
        writeReceipt(s.hlReceiptKey, receipt);
        set({ hlReceipt: receipt });
        await get().refreshHyperliquidRecovery();
        await get().refreshHyperliquidCapacity();
        s.toast("Requested leverage observed after the request expired. No request was retried.", "ok");
        return;
      }
      if (s.hlReceipt.batch) {
        const ids = s.hlReceipt.cloids.map(id => id.toLowerCase());
        if (!response.batch || response.requestId !== s.hlReceipt.requestId || !Array.isArray(response.cloids) ||
            response.cloids.length !== ids.length || !response.cloids.every((id, i) => typeof id === "string" && id.toLowerCase() === ids[i]) ||
            !response.targets || typeof response.targets !== "object" || Array.isArray(response.targets) ||
            Object.keys(response.targets).some(id => !ids.includes(id))) throw new Error("Batch recovery identity mismatch");
        const remaining = ids.filter(id => !["observed", "rejected"].includes(response.targets[id]?.state)).length;
        const outcome = remaining ? "unknown" : "reconciled";
        if (response.remaining !== remaining || response.outcome !== outcome) throw new Error("Incomplete batch recovery evidence");
        const receipt = { ...s.hlReceipt, outcome, uncertain: !!remaining, error: null, batchEvidence: response };
        writeReceipt(s.hlReceiptKey, receipt);
        set({ hlReceipt: receipt });
        s.toast(remaining ? `${remaining} batch order(s) still unresolved. Check the next identity.`
          : "Batch submission identities reconciled. No orders were replaced.", remaining ? "warn" : "ok");
        await get().refreshHyperliquidRecovery();
        return;
      }
      if (s.hlReceipt.kind === "chart" && (response.kind !== "chart" || response.requestId !== s.hlReceipt.requestId ||
          response.canReplace !== false || !Number.isSafeInteger(response.expiresAfter) ||
          !Number.isFinite(response.exchangeTime) || response.exchangeTime <= response.expiresAfter + 2000)) {
        throw new Error("Chart request expiry and replacement identity are not confirmed. Do not retry.");
      }
      const status = response.status;
      if (response.state !== "current" || response.outcome !== "reconciled" || !status?.found ||
          status.cliOrdId?.toLowerCase() !== s.hlReceipt.cloid.toLowerCase() ||
          status.symbol !== s.hlReceipt.body.symbol) {
        throw new Error("Order identity not confirmed. Submission remains unresolved; do not resubmit.");
      }
      const receipt = { ...s.hlReceipt, outcome: "reconciled", uncertain: false, error: null, status, lifecycleReport: null };
      writeReceipt(s.hlReceiptKey, receipt);
      set({ hlReceipt: receipt });
      s.toast(`Hyperliquid order ${status.order_id}: ${status.orderStatus}.`, "ok");
      await get().refreshHyperliquidRecovery();
      if (receipt.body.closePosition) await get().observeHyperliquidClose(receipt, s.hlReceiptKey);
      get().refreshTables(); get().refreshFills();
    } catch (error) {
      s.toast(error.message, "warn", 12000);
    } finally {
      set({ hlReconciling: false });
    }
  },

  hlCapacity: null,
  hlCapacityError: "",
  hlCapacitySeq: 0,
  async refreshHyperliquidCapacity() {
    const s = get(), symbol = s.symbol, key = s.hlReceiptKey, seq = s.hlCapacitySeq + 1;
    if (s.exchange !== "hyperliquid" || !key) return;
    set({ hlCapacity: null, hlCapacityError: "", hlCapacitySeq: seq });
    try {
      const data = await api(`/api/trading-capacity?symbol=${encodeURIComponent(symbol)}`);
      if (get().symbol !== symbol || get().hlReceiptKey !== key || get().hlCapacitySeq !== seq) return;
      if (data.state !== "current" || data.symbol !== symbol || data.exchange !== "hyperliquid" ||
          receiptKey(data.network, data.accountAddress) !== key || !Number.isInteger(data.leverage?.value) ||
          data.leverage.value < 1 || !["cross", "isolated"].includes(data.leverage.type) ||
          !["buy", "sell"].every(side => typeof data.maxTradeSizes?.[side] === "string" && /^\d+(?:\.\d+)?$/.test(data.maxTradeSizes[side]))) {
        throw new Error("Exchange trading capacity is invalid or belongs to another account");
      }
      set({ hlCapacity: { ...data, scopeKey: key, fetchedAt: Date.now() } });
    } catch (error) {
      if (get().symbol === symbol && get().hlReceiptKey === key && get().hlCapacitySeq === seq)
        set({ hlCapacity: null, hlCapacityError: error.message });
    }
  },

  async submitHyperliquidLeverage(leverage) {
    const s = get(), capacity = s.hlCapacity;
    if (!capacity || capacity.scopeKey !== s.hlReceiptKey || capacity.symbol !== s.symbol || !Number.isFinite(capacity.fetchedAt) ||
        Date.now() - capacity.fetchedAt < 0 || Date.now() - capacity.fetchedAt > 15000) {
      s.toast("Refresh exchange capacity before changing leverage.", "err"); return;
    }
    if (leverage === capacity.leverage.value) return;
    await s.submitHyperliquidOrder(null, { kind: "leverage", leverage,
      expectedLeverage: capacity.leverage.value, cross: capacity.leverage.type === "cross" });
  },

  async submitHyperliquidOrder(side, order) {
    const s = get(), symbol = order.symbol || s.symbol, orderType = order.orderType || s.otype;
    if (s.exchange !== "hyperliquid" || s.exchangeBusy || s.bulkBusy || s.ticketBusy || s.hlFillBusy || s.hlReconciling) return;
    if (!isVenueSymbol(symbol)) return;
    if (!s.canTrade) { s.toast("Hyperliquid trading is disabled by the backend gate.", "err", 9000); return; }
    if (!s.hlReceiptKey || s.hlRecoveryError || !s.hlRecoveryLoaded ||
        s.hlServerUnresolved.length || s.hlRecoveryHasMore || unresolvedReceipt(s.hlReceipt)) {
      s.toast(s.hlRecoveryError || "Resolve the saved Hyperliquid submission before placing another order.", "err", 12000);
      return;
    }
    const instrument = s.instruments.find(i => i.symbol === symbol) || {};
    const setting = order.kind === "leverage";
    const size = setting ? 0 : normalizeContractSize(order.size, instrument.contractValueTradePrecision ?? 0);
    if (!setting && (!Number.isFinite(size) || size <= 0)) {
      s.toast("Enter a size that meets this market's lot size.", "err");
      return;
    }
    const close = order.closePosition === true ? { symbol, side, size: order.position?.sizeExact } : s.hlCloseDraft;
    if (close && (close.symbol !== symbol || close.side !== side || size > Number(close.size))) {
      s.toast("Close draft changed or size exceeds the selected position. Open a fresh close ticket.", "err");
      return;
    }
    const trigger = !setting && !close && ["stp", "take_profit"].includes(orderType);
    const market = !setting && orderType === "mkt";
    const body = setting ? { symbol, leverage: order.leverage, cross: order.cross, expectedLeverage: order.expectedLeverage, expectedArmed: s.armed }
      : { symbol, side, orderType: close && !market ? "ioc" : orderType, size };
    if (setting) {
      if (!Number.isFinite(instrument.maxLeverage) || !Number.isInteger(order.leverage) || order.leverage < 1 || order.leverage > instrument.maxLeverage ||
          !Number.isInteger(order.expectedLeverage) || typeof order.cross !== "boolean") {
        s.toast("Invalid exchange leverage setting.", "err"); return;
      }
      if (!confirm(`${s.armed ? "LIVE" : "SIMULATED"} exchange leverage change for ${s.symbol}: ${order.expectedLeverage}x to ${order.leverage}x, keeping ${order.cross ? "cross" : "isolated"} margin. This affects margin requirements and existing position risk. Continue?`)) return;
    } else if (trigger) {
      if (!Number.isFinite(order.stopPrice) || order.stopPrice <= 0) { s.toast("Enter a trigger price.", "err"); return; }
      body.stopPrice = order.stopPrice;
      body.reduceOnly = true;
      // A trigger-limit rests at its limit price once it fires, so a fast move
      // through the trigger can leave the position unprotected. Market trigger is
      // the default; the limit variant stays available but is opt-in and confirmed.
      body.triggerMarket = order.triggerMarket !== false;
      if (!body.triggerMarket) {
        if (!Number.isFinite(order.limitPrice) || order.limitPrice <= 0) {
          s.toast("Enter a stop-limit price.", "err"); return;
        }
        body.limitPrice = order.limitPrice;
        if (!confirm(`${s.armed ? "LIVE" : "SIMULATED"} ${orderType === "stp" ? "STOP" : "TAKE-PROFIT"}-LIMIT ${symbol}: triggers at ${order.stopPrice}, then rests as a limit at ${order.limitPrice}. It may remain unfilled and leave the position unprotected. Continue?`)) return;
      }
    } else if (market) {
      if (!Number.isFinite(order.slippagePercent) || order.slippagePercent < 0.01 || order.slippagePercent > 5) {
        s.toast("Enter a slippage limit between 0.01% and 5%.", "err"); return;
      }
      body.slippagePercent = order.slippagePercent;
      body.reduceOnly = close ? true : order.reduceOnly === true;
    } else {
      if (!Number.isFinite(order.limitPrice) || order.limitPrice <= 0) { s.toast("Enter a limit price.", "err"); return; }
      body.limitPrice = order.limitPrice;
      body.reduceOnly = close ? true : order.reduceOnly === true;
    }
    if (order.quickPercent !== undefined && !setting) {
      if (!Number.isInteger(order.quickPercent) || order.quickPercent < 1 || order.quickPercent > 100) return;
      body.quickPercent = order.quickPercent;
      body.expectedLeverage = order.expectedLeverage;
      body.expectedMarginMode = order.expectedMarginMode;
    }
    if (close) {
      body.closePosition = true;
      if (market) {
        body.expectedArmed = s.armed;
        body.position = order.position;
      }
      if (!confirm(`${s.armed ? "LIVE" : "SIMULATED"} reduce-only ${market ? "MARKET" : "IOC"} close: ${side} ${size} ${symbol}, ${market ? `slippage limit ${body.slippagePercent}%` : `limit ${body.limitPrice}`}. Unfilled quantity may remain. Existing orders will not be cancelled. Continue?`)) return;
    }
    if (order.maxNotional !== undefined) {
      if (!["string", "number"].includes(typeof order.maxNotional) ||
          !Number.isFinite(Number(order.maxNotional)) || Number(order.maxNotional) <= 0) {
        s.toast("Enter a positive finite USD notional budget.", "err");
        return;
      }
      body.maxNotional = order.maxNotional;
    }
    if (market && !close && !confirm(`${s.armed ? "LIVE" : "SIMULATED"} MARKET ${side.toUpperCase()} ${symbol}: ${size} contracts${body.maxNotional !== undefined ? ` (up to $${body.maxNotional})` : ""}, slippage limit ${body.slippagePercent}%. Partial or no fill is possible. Continue?`)) return;
    const requestId = newRequestId();
    const identity = setting ? { kind: "leverage" } : { cloid: newCloid() };
    const pending = { version: 1, requestId, ...identity, body: { ...body, requestId, ...(setting ? {} : identity) },
      outcome: "pending", uncertain: true, createdAt: new Date().toISOString() };
    try {
      writeReceipt(s.hlReceiptKey, pending);
    } catch (error) {
      set({ hlRecoveryError: error.message });
      s.toast("Cannot save recovery receipt. Nothing was submitted.", "err");
      return;
    }
    set({ ticketBusy: true, hlReceipt: pending });
    try {
      const response = await api(setting ? "/api/leverage" : "/api/order", { method: "POST", body: pending.body });
      const receipt = { ...pending, ...response };
      writeReceipt(s.hlReceiptKey, receipt);
      set({ hlReceipt: receipt });
      if (response.simulated) {
        s.toast(`<b>Not sent.</b> ${response.message || "The order was validated but not signed."}`, "warn", 12000);
      } else if (response.outcome === "unknown") {
        s.toast(`<b>Order UNKNOWN:</b> ${response.error || "no response"}. Verify on Hyperliquid before retrying — do not re-place blindly.`, "err", 15000);
      } else if (response.outcome !== "confirmed") {
        s.toast(`<b>Order ${response.outcome}:</b> ${response.error || "not confirmed"}`, "err", 12000);
      } else {
        s.toast(setting ? "Exchange leverage updated. Refreshing trading capacity."
          : close ? "Reduce-only IOC accepted. Check positions: this does not confirm the position is flat."
          : market ? "Market IOC accepted. Check fills and positions; acceptance does not guarantee a full fill."
          : `Hyperliquid order confirmed: ${body.side} ${fmt(body.size)} ${body.symbol}.`, "ok");
      }
      if (close && response.outcome === "confirmed" && !response.simulated)
        await get().observeHyperliquidClose(receipt, s.hlReceiptKey);
      get().refreshTables(); get().refreshAccount();
    } catch (error) {
      const rejected = error.data?.outcome === "rejected";
      const receipt = { ...pending, outcome: rejected ? "rejected" : "unknown", uncertain: !rejected, error: error.message };
      set({ hlReceipt: receipt });
      // The durable pending receipt already blocks resubmission if this update fails.
      try { writeReceipt(s.hlReceiptKey, receipt); } catch {}
      s.toast(rejected ? `Submission rejected: ${error.message}`
        : `Submission unresolved: ${error.message}. Check order status before another order.`, "err", 12000);
      await get().refreshHyperliquidRecovery();
    } finally {
      set({ ticketBusy: false });
      get().refreshHyperliquidCapacity();
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
        body: { symbol: body.symbol, side: body.side, size: body.size, reduceOnly: !!body.reduceOnly, requestId: newRequestId() },
      });
      const chase = r.chase;
      get().onChaseEvent(chase);
      get().toast(`Chase ${chase.id} running: ${side} ${fmt(body.size)} ${body.symbol}.`, "ok", 9000);
    } catch (e) {
      get().toast(`Chase failed: ${e.message}`, "err");
    }
  },

  // Hyperliquid Chase: values come from the Hyperliquid ticket, never the Kraken DOM.
  async submitHyperliquidChase(side, { size, reduceOnly }) {
    const s = get(), symbol = s.symbol;
    if (s.exchange !== "hyperliquid" || s.exchangeBusy || s.ticketBusy || s.hlReconciling || !isVenueSymbol(symbol)) return;
    if (!s.canTrade) { s.toast("Hyperliquid trading is disabled by the backend gate.", "err", 9000); return; }
    if (!s.hlRecoveryLoaded || s.hlRecoveryError || s.hlServerUnresolved.length || unresolvedReceipt(s.hlReceipt)) {
      s.toast(s.hlRecoveryError || "Resolve the saved Hyperliquid submission before starting a Chase.", "err", 12000);
      return;
    }
    const instrument = s.instruments.find(i => i.symbol === symbol) || {};
    const quantity = normalizeContractSize(size, instrument.contractValueTradePrecision ?? 0);
    if (!Number.isFinite(quantity) || quantity <= 0) { s.toast("Enter a size that meets this market's lot size.", "err"); return; }
    const onTimeout = reduceOnly ? "the unfilled rest closes with a reduce-only market order" : "the unfilled rest is cancelled";
    if (s.armed && !confirm(`LIVE CHASE ${side.toUpperCase()} ${quantity} ${symbol}${reduceOnly ? " (reduce-only)" : ""}\n\n`
      + `Rests post-only at the best ${side === "buy" ? "bid" : "ask"} and re-pegs as it moves. After 5 minutes ${onTimeout}. Continue?`)) return;
    set({ ticketBusy: true });
    try {
      const r = await api("/api/chase", { method: "POST",
        body: { symbol, side, size: quantity, reduceOnly: !!reduceOnly, expectedArmed: s.armed, requestId: newRequestId() } });
      if (r.outcome === "simulated") {
        const order = r.action?.orders?.[0];
        get().toast(`Chase simulated (DISARMED): first order ${side} ${order?.s ?? quantity} @ ${order?.p ?? "?"} post-only. Nothing was sent.`, "ok", 12000);
      } else if (r.chase) {
        get().onChaseEvent(r.chase);
        get().toast(`Chase ${r.chase.id} running: ${side} ${quantity} ${symbol}.`, "ok", 9000);
      }
    } catch (e) {
      get().toast(`Chase failed: ${e.message}`, "err", 12000);
    } finally {
      set({ ticketBusy: false });
    }
  },

  async abortChase(chaseId, { acknowledge = false } = {}) {
    if (acknowledge && !confirm("Mark this Chase as checked?\n\nOnly do this after confirming on Hyperliquid that no chase order is still open and the position is what you expect. Nothing is sent.")) return;
    try {
      const r = await api("/api/chase/abort", { method: "POST",
        body: { chaseId, ...(acknowledge ? { acknowledge: true } : {}), requestId: newRequestId() } });
      if (r.error) get().toast(r.error, "err", 9000);
      else if (r.chase) get().onChaseEvent(r.chase);
    } catch (e) {
      get().toast(`Stop failed: ${e.message}`, "err", 9000);
    }
  },

  clearHyperliquidClose() { set({ hlCloseDraft: null, otype: "lmt" }); },

  async observeHyperliquidClose(receipt, scopeKey) {
    const symbol = receipt.body.symbol;
    try {
      const data = await api("/api/positions?fresh=1");
      if (get().exchange !== "hyperliquid" || get().hlReceiptKey !== scopeKey ||
          get().hlReceipt?.requestId !== receipt.requestId) return;
      if (data.state !== "current" || data.exchange !== "hyperliquid" || !Array.isArray(data.positions) ||
          data.positions.some(p => !p || p.error || typeof p.symbol !== "string" || !p.symbol.startsWith("HL_") ||
            !["long", "short"].includes(p.side) || compareContractSizes(p.sizeExact, p.sizeExact) === null) ||
          new Set(data.positions.map(p => p.symbol)).size !== data.positions.length) {
        throw new Error("Fresh position readback is unavailable or invalid");
      }
      const remaining = data.positions.find(p => p.symbol === symbol);
      const observation = { state: remaining ? "remaining" : "flat", symbol,
        side: remaining?.side, sizeExact: remaining?.sizeExact, checkedAt: new Date().toISOString() };
      const updated = { ...get().hlReceipt, closeObservation: observation };
      writeReceipt(scopeKey, updated);
      set({ hlReceipt: updated });
      get().toast(remaining ? `Close did not leave ${symbol} flat: ${remaining.side} ${remaining.sizeExact} contracts remain. No automatic retry.`
        : `${symbol}: flat on fresh exchange readback. Existing orders were not cancelled.`, remaining ? "warn" : "ok", 12000);
    } catch (error) {
      if (get().exchange === "hyperliquid" && get().hlReceiptKey === scopeKey)
        get().toast(`Close order accepted, but position verification failed: ${error.message}. Do not assume it is closed.`, "warn", 12000);
    }
  },

  async closePosition(symbol) {
    const s = get();
    if (s.exchange === "hyperliquid") {
      const position = s.positions.find(p => p.symbol === symbol && !p.error);
      if (s.ticketBusy || !s.canTrade || s.dataStatus.positions?.state !== "current" || !position ||
          !["long", "short"].includes(position.side) || !Number.isFinite(Number(position.size)) || !(Number(position.size) > 0)) {
        s.toast("Current position data and an idle trading ticket are required.", "err");
        return;
      }
      if (s.positions.filter(p => p.symbol === symbol).length !== 1 ||
          compareContractSizes(position.sizeExact, position.sizeExact) === null ||
          !Number.isFinite(Number(position.price)) || !(Number(position.price) > 0)) {
        s.toast("Refresh the position before closing.", "err"); return;
      }
      return get().submitHyperliquidOrder(position.side === "long" ? "sell" : "buy", {
        symbol, orderType: "mkt", closePosition: true, size: position.sizeExact, reduceOnly: true,
        slippagePercent: 0.5, position: { side: position.side, sizeExact: position.sizeExact, price: position.price },
      });
    }
    const p = s.positions.find(x => x.symbol === symbol);
    if (!p) return;
    if (!confirm(`Close ${p.side} position on ${symbol} (${fmt(p.size)} contracts) with a reduce-only market order?`)) return;
    try {
      const r = await api("/api/order", {
        method: "POST",
        body: { symbol, orderType: "mkt", size: Number(p.size), side: p.side === "long" ? "sell" : "buy", reduceOnly: true, requestId: newRequestId() },
      });
      if (r.simulated) s.toast("Close simulated — terminal is disarmed.", "warn");
      else s.toast(r.outcome === "confirmed" ? "Close order confirmed." : `Close ${r.outcome || "failed"}: ${r.error || "not confirmed"}`, r.outcome === "confirmed" ? "ok" : "err");
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
    try { return await s.cancelOrder(s.readOnly ? { ...target, symbol: order.symbol } : target); }
    finally { chartCancelPending.delete(key); }
  },

  async cancelOrder(payload) {
    const s = get();
    if (!s.canTrade) { s.toast("Trading is disabled for this venue.", "err"); return false; }
    try {
      if (s.readOnly && (s.bulkBusy || s.dataStatus.orders?.state !== "current" || !isVenueSymbol(payload.symbol))) {
        throw new Error("Current Hyperliquid order data and a venue symbol are required.");
      }
      // Hyperliquid order ids are 64-bit, so send them as a decimal string; JSON numbers
      // would be rounded and could cancel the wrong order.
      if (s.readOnly && !payload.cliOrdId &&
          !(typeof payload.orderId === "string" && /^[0-9]{1,20}$/.test(payload.orderId))) {
        throw new Error("Hyperliquid cancellation requires an exact decimal order id.");
      }
      const target = payload.cliOrdId
        ? { ...(s.readOnly ? { symbol: payload.symbol } : {}), cliOrdId: payload.cliOrdId }
        : s.readOnly ? { symbol: payload.symbol, orderId: payload.orderId } : { orderId: payload.orderId };
      const body = { ...target, requestId: newRequestId() };
      const r = await api("/api/cancel", { method: "POST", body });
      let ok = false;
      if (r.simulated) s.toast("Cancel simulated — terminal is disarmed.", "warn");
      else if (r.outcome !== "confirmed") s.toast(`Cancel ${r.outcome || "failed"}: ${r.error || "not confirmed"}`, "err");
      else {
        ok = true;
        s.toast("Order cancellation confirmed.", "ok");
      }
      await s.refreshTables();
      return ok;
    } catch (e) {
      s.toast(`Cancel failed: ${e.message}`, "err");
      return false;
    }
  },

  async flattenAll(mode, symbol) {
    const s = get();
    if (s.bulkBusy) return;
    if (s.dataStatus.positions?.state !== "current") {
      s.toast("Position state is not current; flatten rejected.", "err", 10000);
      return;
    }
    if (mode === "emergency" && s.dataStatus.orders?.state !== "current") {
      s.toast("Order state is not current; emergency flatten rejected.", "err", 10000);
      return;
    }
    const positions = s.positions.filter(position => !position.error && Number(position.size) > 0 && (!symbol || position.symbol === symbol));
    const orders = s.orders.filter(order => !order.error);
    if (mode === "chase" && !positions.length) { s.toast("No open positions to Chase-close."); return; }
    if (mode === "emergency" && !positions.length && !orders.length) { s.toast("Already flat with no open orders."); return; }
    const action = mode === "emergency"
      ? `market-close ${positions.length} position(s), confirm each is flat, then cancel ${orders.length} order(s)`
      : `start ${positions.length} reduce-only closing Chase order(s)${symbol ? ` on ${symbol}` : ""}; existing non-Chase orders stay open`;
    const chaseNote = !s.armed ? "" : mode === "emergency"
      ? " After every position is confirmed flat, active Chase workers will be stopped before the remaining orders are canceled."
      : ` This will refuse if another Chase is active or unresolved${symbol ? ` on ${symbol}` : ""}.`;
    if (!confirm(`${s.armed ? "LIVE" : "SIMULATED"} ${mode === "emergency" ? "EMERGENCY FLATTEN" : "SOFT FLATTEN"}?\n\nThis will ${action}.${chaseNote}`)) return;
    set({ bulkBusy: true });
    try {
      const result = await api("/api/flatten", { method: "POST", body: { mode, ...(symbol ? { symbol } : {}), requestId: newRequestId() } });
      for (const item of result.results || []) {
        if (item.chase) get().onChaseEvent(item.chase);
      }
      if (result.simulated) {
        get().toast(`${mode === "emergency" ? "Emergency" : "Soft"} flatten simulated. Nothing was sent.`, "warn", 9000);
      } else if (result.outcome === "confirmed") {
        get().toast(
          mode === "emergency"
            ? `Emergency flatten confirmed: ${result.closedPositionCount} position(s) closed, ${result.cancelledOrderCount} order(s) canceled.`
            : result.startedChaseCount
              ? `Started ${result.startedChaseCount} reduce-only closing Chase order(s).`
              : "All positions were already flat.",
          "ok", 10000,
        );
      } else {
        get().toast(`Flatten ${result.outcome || "failed"}: ${result.error || "not fully confirmed"}`, "err", 12000);
      }
      await Promise.all([get().refreshTables(), get().refreshAccount()]);
    } catch (error) {
      get().toast(`Flatten failed: ${error.message}`, "err", 12000);
    } finally {
      set({ bulkBusy: false });
    }
  },

  async loadHyperliquidCancellations(requestId = "") {
    if (get().hlCancelChecking || get().bulkBusy) return;
    if (typeof requestId !== "string" || (requestId.trim() && !/^[A-Za-z0-9._:-]{8,100}$/.test(requestId.trim()))) {
      get().toast("Enter a valid cancellation request ID.", "err");
      return;
    }
    const id = requestId.trim();
    set({ hlCancelChecking: true, hlCancelHistoryNote: null });
    try {
      const result = await api(`/api/cancel-recovery${id ? `?requestId=${encodeURIComponent(id)}` : ""}`);
      if (result.state !== "current" || !Array.isArray(result.items) || (id && result.items.some(item => item.requestId !== id))) {
        throw new Error("Cancellation history unavailable or request identity mismatched");
      }
      set({ hlCancelHistory: result.items, hlCancelHistoryMore: !!result.hasMore,
        hlCancelHistoryNote: result.items.length ? null : "No matching receipt in this network/account journal. This does not prove no cancellation was submitted." });
    } catch (error) {
      set({ hlCancelHistoryNote: "History refresh failed. Previously listed receipts have not been refreshed." });
      get().toast(error.message, "err");
    }
    finally { set({ hlCancelChecking: false }); }
  },

  inspectHyperliquidCancellation(item) {
    if (get().hlCancelChecking || get().bulkBusy) return;
    const result = item.result || {};
    set({ hlCancelReceipt: { requestId: item.requestId, symbol: item.body.symbol || "Multi-symbol batch", symbols: item.symbols, orderIds: item.targets,
      outcome: result.outcome || "unknown", recoveryError: item.recoveryError,
      results: item.targets.map(orderId => {
        const row = Array.isArray(result.cancelResults) ? result.cancelResults.find(value => value?.orderId === orderId) : null;
        return { orderId, error: row?.error || result.error,
          outcome: row?.outcome || (["confirmed", "rejected", "simulated"].includes(result.outcome) ? result.outcome : "unknown") };
      }),
      readbacks: item.evidence?.targets || {} } });
  },

  async reconcileHyperliquidCancel(target) {
    const s = get(), receipt = s.hlCancelReceipt;
    if (s.hlCancelChecking || s.bulkBusy || !receipt?.orderIds.includes(target) || receipt.outcome === "simulated") return;
    set({ hlCancelChecking: true });
    try {
      const evidence = await api("/api/cancel-reconcile", { method: "POST", body: { requestId: receipt.requestId, target } });
      if (evidence.requestId !== receipt.requestId || evidence.target !== target) throw new Error("Cancellation evidence identity mismatch");
      set(current => current.hlCancelReceipt?.requestId === receipt.requestId
        ? { hlCancelReceipt: { ...current.hlCancelReceipt, readbacks: { ...current.hlCancelReceipt.readbacks, [target]: evidence } } } : {});
    } catch (error) { get().toast(`Cancellation readback: ${error.message}`, "err"); }
    finally { set({ hlCancelChecking: false }); }
  },

  cancelHyperliquidForSymbol() { return get().cancelHyperliquidOrders(false); },

  async cancelHyperliquidOrders(allSymbols = false) {
    const s = get();
    if (s.exchange !== "hyperliquid" || typeof allSymbols !== "boolean" || s.bulkBusy || s.ticketBusy) return;
    if (!s.canTrade || s.dataStatus.orders?.state !== "current") {
      s.toast("Cancellation requires enabled trading and current order data.", "err");
      return;
    }
    const symbol = allSymbols ? "all supported native-perp symbols in this Hyperliquid account" : s.symbol;
    const selected = s.orders.filter(o => allSymbols || o?.symbol === symbol);
    if (selected.some(o => !o || o.error || !isVenueSymbol(o.symbol))) {
      s.toast("Order snapshot contains unavailable or foreign-venue rows. Refresh before cancellation.", "err");
      return;
    }
    const targets = selected.map(o => ({ symbol: o.symbol, orderId: o.order_id }));
    const orderIds = targets.map(o => o.orderId);
    const symbols = Object.fromEntries(targets.map(o => [o.orderId, o.symbol]));
    if (!orderIds.length) { s.toast(`No open orders on ${symbol}.`); return; }
    if (orderIds.length > 100 || orderIds.some(id => typeof id !== "string" || !/^[0-9]{1,20}$/.test(id)) ||
        new Set(orderIds).size !== orderIds.length) {
      s.toast("Bulk cancellation requires at most 100 distinct, exact order IDs.", "err");
      return;
    }
    if (!confirm(`${s.armed ? "LIVE" : "SIMULATED"}: cancel these ${orderIds.length} orders on ${symbol}?${allSymbols ? " This includes TP/SL orders; positions will remain open." : ""} Orders created afterward will not be included.`)) return;
    const requestId = newRequestId();
    const receipt = { requestId, symbol, orderIds, symbols, outcome: "pending", results: [] };
    set({ bulkBusy: true, hlCancelReceipt: receipt });
    try {
      const result = await api("/api/cancel", { method: "POST", body: allSymbols ? { targets, requestId } : { symbol, orderIds, requestId } });
      const matched = Array.isArray(result.cancelResults) && result.cancelResults.length === orderIds.length &&
        result.cancelResults.every((row, index) => row.orderId === orderIds[index]);
      const results = matched ? result.cancelResults : orderIds.map(orderId => ({ orderId, outcome: "unknown" }));
      const outcome = matched ? result.outcome : "unknown";
      set({ hlCancelReceipt: { ...receipt, outcome, results, error: result.error } });
      const confirmed = results.filter(row => row.outcome === "confirmed").length;
      if (result.simulated) s.toast(`Cancellation simulated for ${orderIds.length} orders on ${symbol}. Nothing was sent.`, "warn");
      else if (confirmed === orderIds.length) s.toast(`Canceled ${confirmed} orders on ${symbol}.`, "ok");
      else s.toast(`Cancellation incomplete: ${confirmed}/${orderIds.length} confirmed. Inspect the per-order results.`, "warn", 12000);
    } catch (error) {
      const outcome = error.data?.outcome === "rejected" ? "rejected" : "unknown";
      set({ hlCancelReceipt: { ...receipt, outcome, error: error.message,
        results: orderIds.map(orderId => ({ orderId, outcome, error: error.message })) } });
      s.toast(`Cancellation ${outcome}: ${error.message}. No automatic retry.`, "err", 12000);
    } finally {
      set({ bulkBusy: false });
      await get().refreshTables();
    }
  },

  async cancelAllForSymbol() {
    const s = get();
    if (s.readOnly) { await s.cancelHyperliquidForSymbol(); return; }
    const mine = s.orders.filter(o => o.symbol === s.symbol);
    if (!mine.length) { s.toast(`No open orders on ${s.symbol}.`); return; }
    if (!confirm(`Cancel ${mine.length} open order(s) on ${s.symbol}?`)) return;
    const results = [];
    for (const o of mine) {
      try {
        results.push(await api("/api/cancel", { method: "POST", body: { cliOrdId: o.cliOrdId || undefined, orderId: o.order_id || undefined, requestId: newRequestId() } }));
      } catch (e) {
        results.push({ outcome: "unknown", error: e.message });
      }
    }
    const confirmed = results.filter(result => result.outcome === "confirmed").length;
    if (!s.armed) s.toast(`Cancel-all simulated for ${s.symbol}.`, "warn");
    else if (confirmed === mine.length) s.toast(`Canceled ${confirmed} order(s) on ${s.symbol}.`, "ok");
    else s.toast(`Cancel-all incomplete: ${confirmed}/${mine.length} confirmed. Check live orders before retrying.`, "err", 10000);
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
        body: { actions: acts, messageId: s.chat[msgIdx]?.id, blockIndex: blockIdx, requestId: newRequestId() },
      });
    } catch (e) {
      s.toast(`Execute failed: ${e.message}`, "err");
      set({ actionBusy: false });
      return;
    }
    const sim = !r.armed;
    const executed = Array.isArray(r.actions) ? r.actions : acts;
    const results = r.results || [];
    const allFailed = results.length > 0 && results.every(res => ["rejected", "unknown"].includes(res.outcome) || (res.ok === false && res.outcome !== "partial"));
    const hasPartial = results.some(res => res.outcome === "partial");
    const lines = results.map(res => `${res.type || "??"}: ${res.outcome || (res.ok ? "confirmed" : "failed")}${res.error ? ` (${res.error})` : ""}`);
    const toastLead = sim ? "<b>Simulated</b> (disarmed)" : allFailed ? "<b>Failed</b>" : hasPartial ? "<b>Partial</b>" : "<b>Confirmed</b>";
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
      if (res.error && res.outcome !== "partial") { rows.push({ status: "error", primary: describeAction(a), secondary: String(res.error).slice(0, 80) }); return; }
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
      return { chat, actionBusy: false, lastExecution: { mode: sim ? "SIMULATED (terminal was disarmed — nothing was sent to Kraken)" : allFailed ? "LIVE ATTEMPT FAILED (nothing confirmed)" : hasPartial ? "LIVE PARTIAL (some writes confirmed)" : "LIVE (confirmed by Kraken)", results: lines, verification: rows, when: new Date().toISOString() } };
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
        body: { message: text, symbol: s.symbol, lastExecution: s.lastExecution || null, requestId: newRequestId() },
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

  onProtectionAlert(alert) {
    if (!alert?.symbol || !alert?.kind) return;
    const key = `${alert.symbol}:${alert.kind}`;
    set(s => {
      const protectionAlerts = { ...s.protectionAlerts };
      if (alert.status === "RESTORED") delete protectionAlerts[key];
      else protectionAlerts[key] = alert;
      return { protectionAlerts };
    });
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
    // Arming a venue that cannot trade is pointless, but disarming must always work.
    if (!s.canTrade && !s.armed) { s.toast("Trading is disabled for this venue. Enable the backend gate first.", "warn"); return; }
    if (s.armed) {
      const r = await api("/api/arm", { method: "POST", body: { armed: false } }).catch(() => null);
      if (r) { set({ armed: !!r.armed }); s.toast("Disarmed — orders are simulated again.", ""); }
      return;
    }
    if (s.env === "live") {
      // ARM is one process-wide gate, so say so rather than implying it is venue-local.
      const venue = s.readOnly ? `Hyperliquid (${s.signedTrading})` : `${s.env.toUpperCase()} Kraken Futures`;
      const answer = prompt(
        `This enables LIVE order entry for the whole terminal, both exchanges.\n` +
        `You are on ${venue}.\nOrders you place will be real.\n\nType ARM to continue:`);
      if (answer !== "ARM") { s.toast("Arm cancelled.", ""); return; }
    }
    try {
      const { challenge } = await api("/api/arm/challenge");
      const r = await api("/api/arm", { method: "POST", body: { armed: true, challenge } });
      set({ armed: !!r.armed });
      if (r.armed) s.toast("Order entry ARMED. Orders are now live.", "warn");
    } catch (e) { s.toast(`Arm failed: ${e.message}`, "err"); }
  },

  togglePro() {
    const pro = !get().pro;
    set({ pro });
    localStorage.setItem("kt.pro", pro ? "1" : "0");
    get().refreshAccount();
    get().toast(pro ? `Pro-Mode on: Balance and Avail margin shown +$${get().proAdj(0).toLocaleString("en-US")} (display only; orders use real margin).` : "Pro-Mode off: balances are real.", "warn", 6000);
  },

  toggleSound() {
    const soundOn = !get().soundOn;
    set({ soundOn });
    localStorage.setItem("kt.sound", soundOn ? "1" : "0");
    if (soundOn) get().playChime();
  },

  setLev(lev) {
    set({ lev });
    localStorage.setItem(venueKey("kt.lev"), String(lev));
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
