/* Kraken Futures Terminal — frontend. Vanilla JS, lightweight-charts. */

"use strict";

const state = {
  symbol: localStorage.getItem("kt.symbol") || "PI_XBTUSD",
  res: localStorage.getItem("kt.res") || "1m",
  instruments: [],
  instrumentsLoaded: false,
  tickers: {},          // symbol -> ticker snapshot
  watchlist: [],
  armed: false,
  env: "live",
  hasKeys: false,
  tab: "positions",
  otype: "mkt",
  lev: Number(localStorage.getItem("kt.lev")) || 10,
  soundOn: localStorage.getItem("kt.sound") !== "0",
  pro: localStorage.getItem("kt.pro") === "1",
  chat: [],
  candles: [],          // current chart candles [t, o, h, l, c, v]
  prevPrice: null,
};

const RES_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400, "1w": 604800 };
const $ = (id) => document.getElementById(id);

/* ---------- helpers ---------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function fmt(x, digits) {
  if (x === null || x === undefined || Number.isNaN(Number(x))) return "–";
  const n = Number(x);
  if (digits !== undefined) return n.toFixed(digits);
  if (Math.abs(n) >= 1000) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (Math.abs(n) >= 1) return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
  return n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}
function fmtVol(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "–";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toFixed(1);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, kind = "", ms = 5000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/* ---------- chart ---------- */

let chart, candleSeries, volSeries;
const priceLines = [];
let overlayPrices = []; // line levels the price scale must include

function initChart() {
  chart = LightweightCharts.createChart($("chart"), {
    layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#787b86", fontSize: 11 },
    grid: { vertLines: { color: "#161b27" }, horzLines: { color: "#161b27" } },
    rightPriceScale: { borderColor: "#1e2430" },
    timeScale: { borderColor: "#1e2430", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350",
    borderUpColor: "#26a69a", borderDownColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350",
  });
  // keep position/order/liq lines inside the visible price range
  candleSeries.applyOptions({
    autoscaleInfoProvider: (original) => {
      const res = original ? original() : {};
      if (!overlayPrices.length) return res;
      let min = res.priceRange ? res.priceRange.minValue : Infinity;
      let max = res.priceRange ? res.priceRange.maxValue : -Infinity;
      for (const v of overlayPrices) { if (v < min) min = v; if (v > max) max = v; }
      if (!Number.isFinite(min) || !Number.isFinite(max)) return res;
      const pad = (max - min) * 0.04 || Math.abs(min) * 0.005 || 1;
      return { ...res, priceRange: { minValue: min - pad, maxValue: max + pad } };
    },
  });
  volSeries = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" } });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  new ResizeObserver(() => chart.applyOptions({ width: $("chart").clientWidth, height: $("chart").clientHeight }))
    .observe($("chart"));
}

function clearPriceLines() {
  for (const line of priceLines) { try { candleSeries.removePriceLine(line); } catch (e) {} }
  priceLines.length = 0;
}

function addPriceLine(price, color, title, dashed) {
  if (price === null || price === undefined || !Number.isFinite(Number(price))) return;
  overlayPrices.push(Number(price));
  priceLines.push(candleSeries.createPriceLine({
    price: Number(price), color, lineWidth: 1,
    lineStyle: dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
    title, axisLabelVisible: true,
  }));
}

function applyOverlayLines() {
  clearPriceLines();
  overlayPrices = [];
  for (const p of lastPositions) {
    if (p.symbol !== state.symbol || !p.size) continue;
    addPriceLine(p.price, p.side === "long" ? "#26a69a" : "#ef5350", `${p.side} ${fmt(p.size)}`, false);
    if (p.liqPriceEstimate) addPriceLine(p.liqPriceEstimate, "#f0b90b", `LIQ ${fmt(p.liqPriceEstimate)}`, true);
  }
  for (const o of lastOrders) {
    if (o.symbol !== state.symbol) continue;
    if (o.limitPrice) addPriceLine(o.limitPrice, "#4f8cff", `${o.side} ${o.orderType} ${fmt(o.size)}`, true);
    if (o.stopPrice) {
      const isTp = String(o.orderType).toLowerCase() === "take_profit";
      addPriceLine(o.stopPrice, isTp ? "#26a69a" : "#ef5350", `${isTp ? "TP" : "SL"} ${fmt(o.stopPrice)}`, true);
    }
  }
}

/* price scale format follows the instrument's tickSize (PUMP 0.004681 needs 4+ digits) */
function applyPriceFormat() {
  const inst = state.instruments.find(i => i.symbol === state.symbol) || {};
  const tick = Number(inst.tickSize) || 0.01;
  const decimals = Math.max(0, Math.min(10, Math.ceil(-Math.log10(tick) - 1e-9)));
  const priceFormat = { type: "price", precision: decimals, minMove: tick };
  candleSeries.applyOptions({ priceFormat });
  volSeries.applyOptions({ priceFormat: { type: "volume" } });
}

async function loadChart() {
  // drop the previous symbol's overlay levels BEFORE setData, or the autoscale
  // stretches to stale prices and the scale looks broken until manually reset
  clearPriceLines();
  overlayPrices = [];
  applyPriceFormat();
  $("h-symbol").textContent = state.symbol;
  $("ticket-symbol").textContent = state.symbol;
  document.querySelectorAll(".wl-row").forEach((r) => r.classList.toggle("active", r.dataset.sym === state.symbol));
  try {
    const data = await api(`/api/candles?symbol=${encodeURIComponent(state.symbol)}&res=${state.res}`);
    state.candles = data.candles || [];
    $("chart-note").textContent = state.candles.length
      ? `${state.candles.length} candles (${data.source === "hub" ? "built from live trades" : data.source === "rest" ? "Kraken charts API" : "waiting for data"})`
      : "No candle history yet — chart fills in as trades stream in.";
    if (state.candles.length) {
      candleSeries.setData(state.candles.map(c => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] })));
      volSeries.setData(state.candles.map(c => ({
        time: c[0], value: c[5] || 0, color: c[4] >= c[1] ? "rgba(38,166,154,0.4)" : "rgba(239,83,80,0.4)",
      })));
      applyOverlayLines();
      chart.timeScale().fitContent();
    } else {
      candleSeries.setData([]); volSeries.setData([]);
    }
  } catch (e) {
    $("chart-note").textContent = `Chart error: ${e.message}`;
  }
}

/* live trade → merge into current candle of the active resolution.
   Quiet minutes are carried forward as flat candles (same as Kraken's own
   charts), otherwise a thin market turns the right edge into sparse dots. */
function appendCandle(c) {
  state.candles.push(c);
  if (state.candles.length > 3000) state.candles.shift();
  updateSeriesBar(c);
}

function updateSeriesBar(c) {
  candleSeries.update({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] });
  const gray = "rgba(120,123,134,0.30)";
  volSeries.update({
    time: c[0], value: c[5] || 0,
    color: !c[5] ? gray : (c[4] >= c[1] ? "rgba(38,166,154,0.4)" : "rgba(239,83,80,0.4)"),
  });
}

function advanceBuckets(toBucket) {
  const sec = RES_SECONDS[state.res] || 60;
  const candles = state.candles;
  let last = candles[candles.length - 1];
  if (!last || toBucket <= last[0]) return;
  let guard = 0;
  while (last[0] + sec <= toBucket) {
    if (++guard > 500) { loadChart(); return; } // tab slept for ages — refetch instead
    const flat = [last[0] + sec, last[4], last[4], last[4], last[4], 0];
    appendCandle(flat);
    last = flat;
  }
}

function currentBucket() {
  const sec = RES_SECONDS[state.res] || 60;
  return Math.floor(Date.now() / 1000 / sec) * sec;
}

function onTrade(trade) {
  if (trade.symbol !== state.symbol) return;
  const sec = RES_SECONDS[state.res] || 60;
  const bucket = Math.floor(trade.time / sec) * sec;
  const candles = state.candles;
  let last = candles[candles.length - 1];
  if (!last) return;
  if (bucket < last[0]) return; // stale trade for an older bucket
  if (bucket > last[0]) advanceBuckets(bucket); // flat-fill gaps and the new bucket
  last = candles[candles.length - 1];
  if (last[0] !== bucket) return;
  if (last[5] === 0 && last[1] === last[4]) {
    // filler candle receiving its first real trade — OHLC starts here
    last[1] = last[2] = last[3] = trade.price;
  } else {
    last[2] = Math.max(last[2], trade.price);
    last[3] = Math.min(last[3], trade.price);
  }
  last[4] = trade.price;
  last[5] = (last[5] || 0) + trade.qty;
  updateSeriesBar(last);
}

/* ---------- header + watchlist ---------- */

function renderHeader(t) {
  if (!t) return;
  const prev = state.prevPrice;
  const last = Number(t.last);
  const el = $("h-price");
  el.textContent = fmt(last);
  if (prev !== null && last !== prev) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(last > prev ? "flash-up" : "flash-down");
  }
  state.prevPrice = last;
  const ch = Number(t.change24h);
  const chEl = $("h-change");
  chEl.textContent = `${ch >= 0 ? "+" : ""}${ch.toFixed(2)}%`;
  chEl.className = "v " + (ch >= 0 ? "up" : "down");
  $("h-mark").textContent = fmt(t.markPrice);
  $("h-index").textContent = fmt(t.indexPrice);
  $("h-bid").textContent = fmt(t.bid);
  $("h-ask").textContent = fmt(t.ask);
  $("h-high").textContent = fmt(t.high24h);
  $("h-low").textContent = fmt(t.low24h);
  $("h-vol").textContent = fmtVol(t.vol24h);
  $("h-funding").textContent = t.fundingRate !== undefined ? (Number(t.fundingRate) * 100).toFixed(4) + "%" : "–";
  $("h-oi").textContent = fmtVol(t.openInterest);
}

function renderWatchlist() {
  const wl = $("watchlist");
  wl.innerHTML = "";
  for (const sym of state.watchlist) {
    const t = state.tickers[sym] || {};
    const row = document.createElement("div");
    row.className = "wl-row" + (sym === state.symbol ? " active" : "");
    row.dataset.sym = sym;
    const last = t.last !== undefined ? Number(t.last) : null;
    const ch = t.change24h !== undefined ? Number(t.change24h) : null;
    row.innerHTML = `
      <div><div class="wl-sym">${esc(sym)}</div><div class="wl-sub">${esc(t.pair || "")}</div></div>
      <div><div class="wl-price">${last !== null ? fmt(last) : "–"}</div>
      <div class="wl-change ${ch !== null ? (ch >= 0 ? "up" : "down") : ""}">${ch !== null ? (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%" : ""}</div></div>`;
    row.addEventListener("click", () => selectSymbol(sym));
    wl.appendChild(row);
  }
}

function onTicker(t) {
  state.tickers[t.symbol] = t;
  if (t.symbol === state.symbol) renderHeader(t);
  updatePositionCells();
  const row = document.querySelector(`.wl-row[data-sym="${CSS.escape(t.symbol)}"]`);
  if (row) {
    const priceEl = row.querySelector(".wl-price");
    const chEl = row.querySelector(".wl-change");
    const last = Number(t.last), ch = Number(t.change24h);
    priceEl.textContent = fmt(last);
    chEl.textContent = (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%";
    chEl.className = "wl-change " + (ch >= 0 ? "up" : "down");
  }
}

async function selectSymbol(sym) {
  if (!sym) return;
  state.symbol = sym;
  state.prevPrice = null;
  localStorage.setItem("kt.symbol", sym);
  await api(`/api/tickers?symbols=${encodeURIComponent(sym)}`); // make sure the hub subscribes
  hubWatch(sym);
  loadChart();
  refreshBook();
  renderWatchlist();
  refreshSignal();
}

async function hubWatch(sym) {
  // ensure the watchlist contains the symbol even before the first ticker arrives
  if (!state.watchlist.includes(sym)) {
    state.watchlist.push(sym);
    renderWatchlist();
  }
}

/* ---------- search ---------- */

function renderSearchResults(q) {
  const box = $("search-results");
  if (!q) { box.style.display = "none"; return; }
  const ql = q.toLowerCase();
  const matches = state.instruments
    .filter(i => String(i.symbol).toLowerCase().includes(ql) || String(i.pair || "").toLowerCase().includes(ql))
    .slice(0, 12);
  if (!matches.length) { box.style.display = "none"; return; }
  box.innerHTML = matches.map(i =>
    `<div class="row" data-sym="${esc(i.symbol)}"><span class="sym">${esc(i.symbol)}</span><span class="name">${esc(i.pair || i.underlying || "")}</span></div>`
  ).join("");
  box.style.display = "block";
  box.querySelectorAll(".row").forEach(r => r.addEventListener("click", async () => {
    box.style.display = "none";
    $("symbol-search").value = "";
    const sym = r.dataset.sym;
    await api(`/api/tickers?symbols=${encodeURIComponent(sym)}`);
    selectSymbol(sym);
  }));
}

/* ---------- SSE ---------- */

function connectStream() {
  const es = new EventSource("/api/stream");
  es.addEventListener("ticker", (e) => { try { onTicker(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("trade", (e) => { try { onTrade(JSON.parse(e.data)); } catch (err) {} });
  es.addEventListener("status", (e) => {
    try {
      const s = JSON.parse(e.data);
      const st = typeof s.status === "string" ? s.status : s.status && s.status.status;
      const connected = st === "connected";
      $("feed-dot").classList.toggle("on", connected);
      $("feed-label").textContent = st || "unknown";
    } catch (err) {}
  });
  es.addEventListener("armed", (e) => {
    try { setArmed(JSON.parse(e.data).armed); } catch (err) {}
  });
  es.addEventListener("chase", (e) => {
    try {
      const c = JSON.parse(e.data);
      if (c.status && c.status !== "running") {
        toast(`Chase ${esc(c.id)} ${esc(c.status)}: filled ${fmt(c.filled)}/${fmt(c.size)} ${esc(c.symbol)}`, c.status === "filled" ? "ok" : "warn", 9000);
        if (c.status === "filled" && state.soundOn) playChime();
      }
    } catch (err) {}
  });
  es.onerror = () => {
    $("feed-dot").classList.remove("on");
    $("feed-label").textContent = "reconnecting";
  };
}

/* ---------- orderbook ---------- */

async function refreshBook() {
  try {
    const data = await api(`/api/orderbook?symbol=${encodeURIComponent(state.symbol)}`);
    const book = data.orderBook || {};
    const asks = (book.asks || []).slice(0, 9);
    const bids = (book.bids || []).slice(0, 9);
    const maxQty = Math.max(...asks.map(a => Number(a.qty || a[1])), ...bids.map(b => Number(b.qty || b[1])), 1);
    const color = cls => cls === "ask" ? "rgba(239,83,80,0.13)" : "rgba(38,166,154,0.13)";
    const row = (p, cls) => {
      const price = Array.isArray(p) ? p[0] : p.price;
      const qty = Array.isArray(p) ? p[1] : p.qty;
      const w = Math.min(100, (Number(qty) / maxQty) * 100);
      const bg = `linear-gradient(to left, ${color(cls)} ${w}%, transparent ${w}%)`;
      return `<div class="book-row ${cls}" style="background:${bg}"><span class="price">${fmt(price)}</span><span class="qty">${fmt(qty)}</span></div>`;
    };
    $("book-asks").innerHTML = asks.slice().reverse().map(a => row(a, a, "ask")).join("");
    $("book-bids").innerHTML = bids.map(b => row(b, b, "bid")).join("");
    if (asks.length && bids.length) {
      const bestAsk = Array.isArray(asks[0]) ? asks[0][0] : asks[0].price;
      const bestBid = Array.isArray(bids[0]) ? bids[0][0] : bids[0].price;
      $("book-mid-price").textContent = fmt((Number(bestAsk) + Number(bestBid)) / 2);
      $("book-spread").textContent = fmt(Number(bestAsk) - Number(bestBid));
    }
  } catch (e) { /* orderbook is best-effort */ }
}

/* ---------- account / positions / orders / fills ---------- */

let lastPositions = [];
let lastOrders = [];

/* Pro-Mode: display-only sticker on the two sidebar numbers. Never feeds sizing,
   orders, or anything Kraken sees — those always use the REAL margin. */
function proAdj(v) { return state.pro ? Number(v || 0) + 7800 : Number(v || 0); }

async function refreshAccount() {
  try {
    const a = await api("/api/account");
    if (a.error) { $("acct-balance").textContent = "–"; return; }
    $("acct-balance").textContent = "$" + fmt(proAdj(a.balanceValue));
    const availEl = $("acct-avail");
    availEl.textContent = "$" + fmt(proAdj(a.availableMargin ?? a.collateralValue));
    availEl.dataset.raw = String(a.availableMargin ?? a.collateralValue ?? 0); // real margin — % sizing uses this
    const pnl = a.pnl ?? a.totalUnrealized;
    const el = $("acct-pnl");
    // with open positions the SSE-computed sum owns this row (updatePositionCells)
    if (!lastPositions.some(p => p.symbol && !p.error)) {
      el.textContent = (pnl >= 0 ? "+$" : "-$") + fmt(Math.abs(pnl || 0));
      el.className = "v " + (pnl >= 0 ? "up" : "down");
    }
  } catch (e) {}
}

/* ---- live position PnL — the single mechanism for sidebar AND table ----
   Marks come from the SSE ticker stream (~1s). uPnL computed client-side:
   linear (PF_*): size × (mark − entry) · inverse (PI_*): size × (1/entry − 1/mark) */
function computeUpnl(p) {
  const t = state.tickers[p.symbol];
  const mark = t && Number(t.markPrice);
  const inst = state.instruments.find(i => i.symbol === p.symbol) || {};
  const mult = Number(inst.contractSize || 1);
  const size = Number(p.size), entry = Number(p.price);
  if (!size || !entry || !mark) return Number(p.unrealizedPnl || 0);
  const dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
  return inst.type === "futures_inverse"
    ? dir * size * mult * (1 / entry - 1 / mark)
    : dir * size * mult * (mark - entry);
}

function updatePositionCells() {
  const live = lastPositions.filter(p => p.symbol && !p.error);
  let total = null;
  for (const p of live) {
    const t = state.tickers[p.symbol];
    const mark = t ? t.markPrice : null;
    const pnl = computeUpnl(p);
    total = (total ?? 0) + pnl;
    if (state.tab !== "positions") continue;
    const markCell = document.querySelector(`[data-mark="${CSS.escape(p.symbol)}"]`);
    if (markCell && mark !== undefined && mark !== null) markCell.textContent = fmt(mark);
    const pnlCell = document.querySelector(`[data-upnl="${CSS.escape(p.symbol)}"]`);
    if (pnlCell) {
      pnlCell.textContent = (pnl >= 0 ? "+" : "") + fmt(pnl, 2);
      pnlCell.classList.toggle("up", pnl >= 0);
      pnlCell.classList.toggle("down", pnl < 0);
    }
  }
  if (total !== null) {
    const el = $("acct-pnl");
    el.textContent = (total >= 0 ? "+$" : "-$") + fmt(Math.abs(total), 2);
    el.className = "v " + (total >= 0 ? "up" : "down");
  }
}

function tableSignature() {
  const r = lastPositions.filter(p => !p.error).map(p => [p.symbol, p.side, p.size, Number(p.price).toFixed(6), Number(p.liqPriceEstimate || 0).toFixed(4)]);
  const o = lastOrders.filter(o => !o.error).map(o => [o.symbol, o.side, o.orderType, o.size, o.limitPrice, o.stopPrice, o.cliOrdId || o.orderId, o.status, o.filledSize]);
  return JSON.stringify([r, o]);
}

let lastTableSig = null;

async function refreshTables() {
  try {
    const [pos, ord] = await Promise.all([api("/api/positions"), api("/api/orders")]);
    lastPositions = pos.positions || [];
    lastOrders = ord.orders || [];
    const posSymbols = lastPositions.filter(p => p.symbol && !p.error).map(p => p.symbol);
    if (posSymbols.length) api(`/api/tickers?symbols=${encodeURIComponent(posSymbols.join(","))}`).catch(() => {});
    updatePositionCells();
    $("cnt-positions").textContent = lastPositions.length || "";
    $("cnt-orders").textContent = lastOrders.length || "";
    const sig = tableSignature();
    if (sig !== lastTableSig) {
      lastTableSig = sig;
      renderTab();
      applyOverlayLines(); // recreate chart lines only when positions/orders actually changed
    }
  } catch (e) {}
}

let lastFillsSig = null;

async function refreshFills() {
  if (state.tab !== "fills") return;
  try {
    const f = await api("/api/fills");
    // Kraken returns newest-first; sort explicitly so ordering assumptions can't hide recent fills
    const fills = (f.fills || []).slice().sort((a, b) => new Date(b.fillTime) - new Date(a.fillTime)).slice(0, 80);
    const sig = JSON.stringify(fills.map(x => [x.fillTime, x.symbol, x.side, x.price, x.size || x.qty]));
    if (sig !== lastFillsSig) {
      lastFillsSig = sig;
      renderTab(fills);
    }
  } catch (e) {}
}

let scannerLoading = false;

async function refreshScanner() {
  if (state.tab !== "scanner" || scannerLoading) return;
  scannerLoading = true;
  const body = $("tab-body");
  if (!body.querySelector("table.grid")) body.innerHTML = `<div class="empty">Scanning perpetuals… (first scan fetches 1m mark candles per market)</div>`;
  try {
    const data = await api("/api/volatility?limit=20");
    const rows = data.rows || [];
    const note = `realized vol over ${data.windowMinutes}m closed 1m mark candles · ${data.marketsScanned} markets scanned${data.cached ? " · cached" : ""}`;
    body.innerHTML = `<div class="scanner-note">${esc(note)} — click a row to open its chart</div>` +
      (rows.length ? `<table class="grid"><thead><tr>
        <th>Symbol</th><th class="num">Mark</th><th class="num">Realized vol</th><th class="num">Range</th><th class="num">Move</th><th class="num">Spread</th><th class="num">24h volume $</th></tr></thead><tbody>` +
      rows.map(r => `<tr class="sym-link" data-sym="${esc(r.symbol)}" style="cursor:pointer">
        <td>${esc(r.symbol)}</td>
        <td class="num">${fmt(r.markPrice)}</td>
        <td class="num ${r.realizedVolatilityPercent >= 0.3 ? "up" : ""}">${fmt(r.realizedVolatilityPercent, 3)}%</td>
        <td class="num">${fmt(r.rangePercent, 2)}%</td>
        <td class="num ${r.movePercent >= 0 ? "up" : "down"}">${r.movePercent >= 0 ? "+" : ""}${fmt(r.movePercent, 2)}%</td>
        <td class="num">${fmt(r.spreadPercent, 3)}%</td>
        <td class="num">$${fmtVol(r.volumeQuote)}</td>
      </tr>`).join("") + `</tbody></table>` : `<div class="empty">No markets passed the filters</div>`);
    body.querySelectorAll("tr[data-sym]").forEach(tr => tr.addEventListener("click", () => selectSymbol(tr.dataset.sym)));
  } catch (e) {
    body.innerHTML = `<div class="empty">Scan failed: ${esc(e.message)}</div>`;
  } finally {
    scannerLoading = false;
  }
}

async function refreshSignal() {
  try {
    const s = await api(`/api/signal?symbol=${encodeURIComponent(state.symbol)}`);
    const el = $("ema-badge");
    el.classList.remove("long", "short");
    if (s.error) { el.textContent = "EMA –"; return; }
    if (s.side) {
      el.textContent = `EMA${s.fast}/${s.slow}: ${s.side.toUpperCase()}`;
      el.classList.add(s.side);
    } else {
      el.textContent = `EMA${s.fast}/${s.slow}: flat`;
    }
    el.title = `price ${fmt(s.price)} · ema${s.fast} ${fmt(s.emaFast)} · ema${s.slow} ${fmt(s.emaSlow)}`;
  } catch (e) {
    $("ema-badge").textContent = "EMA –";
  }
}

function renderTab(fillsOverride) {
  const body = $("tab-body");
  if (state.tab === "positions") {
    if (!lastPositions.length) { body.innerHTML = `<div class="empty">No open positions</div>`; return; }
    body.innerHTML = `<table class="grid"><thead><tr>
      <th>Symbol</th><th>Side</th><th class="num">Size</th><th class="num">Entry</th><th class="num">Mark</th><th class="num">Liq (est)</th><th class="num">Unrealized PnL</th><th class="num">Funding</th><th></th></tr></thead><tbody>` +
      lastPositions.map(p => {
        const t = state.tickers[p.symbol];
        const mark = t ? t.markPrice : null;
        const pnl = Number(p.unrealizedPnl || 0);
        return `<tr>
          <td><a href="#" class="sym-link" data-sym="${esc(p.symbol)}">${esc(p.symbol)}</a></td>
          <td class="${p.side === "long" ? "up" : "down"}">${esc(p.side)}</td>
          <td class="num">${fmt(p.size)}</td>
          <td class="num">${fmt(p.price)}</td>
          <td class="num" data-mark="${esc(p.symbol)}">${fmt(mark)}</td>
          <td class="num" style="color:var(--warn)">${p.liqPriceEstimate ? fmt(p.liqPriceEstimate) : "–"}</td>
          <td class="num ${pnl >= 0 ? "up" : "down"}" data-upnl="${esc(p.symbol)}">${pnl >= 0 ? "+" : ""}${fmt(pnl, 2)}</td>
          <td class="num">${fmt(p.unrealizedFunding, 4)}</td>
          <td class="num"><button class="row-btn sell" data-close="${esc(p.symbol)}">Close</button></td>
        </tr>`;
      }).join("") + `</tbody></table>`;
    body.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => closePosition(b.dataset.close)));
    body.querySelectorAll(".sym-link").forEach(a => a.addEventListener("click", (e) => { e.preventDefault(); selectSymbol(a.dataset.sym); }));
  } else if (state.tab === "orders") {
    if (!lastOrders.length) { body.innerHTML = `<div class="empty">No open orders</div>`; return; }
    body.innerHTML = `<table class="grid"><thead><tr>
      <th>Symbol</th><th>Side</th><th>Type</th><th class="num">Size</th><th class="num">Limit</th><th class="num">Stop</th><th>Reduce-only</th><th>Time</th><th></th></tr></thead><tbody>` +
      lastOrders.map(o => `<tr>
        <td><a href="#" class="sym-link" data-sym="${esc(o.symbol)}">${esc(o.symbol)}</a></td>
        <td class="${o.side === "buy" ? "up" : "down"}">${esc(o.side)}</td>
        <td>${esc(o.orderType)}</td>
        <td class="num">${fmt(o.size ?? ((Number(o.filledSize || 0) + Number(o.unfilledSize || 0)) || null))}</td>
        <td class="num">${fmt(o.limitPrice)}</td>
        <td class="num">${fmt(o.stopPrice)}</td>
        <td>${o.reduceOnly === true || String(o.reduceOnly).toLowerCase() === "true" ? "yes" : ""}</td>
        <td>${esc(String(o.receivedTime || o.filledTime || "").slice(0, 19).replace("T", " "))}</td>
        <td class="num"><button class="row-btn" data-cancel='${esc(JSON.stringify({cliOrdId: o.cliOrdId || null, orderId: o.order_id || o.orderId || null}))}'>Cancel</button></td>
      </tr>`).join("") + `</tbody></table>`;
    body.querySelectorAll("[data-cancel]").forEach(b => b.addEventListener("click", async () => {
      try {
        const payload = JSON.parse(b.dataset.cancel);
        const body = payload.cliOrdId ? { cliOrdId: payload.cliOrdId } : { orderId: payload.orderId };
        const r = await api("/api/cancel", { method: "POST", body });
        if (r.simulated) toast("Cancel simulated — terminal is disarmed.", "warn");
        else if (r.error) toast(`Cancel failed: ${esc(r.error)}`, "err");
        else {
          const ok = r.response && r.response.result === "success";
          toast(ok ? "Order canceled." : `Cancel rejected: ${esc(JSON.stringify((r.response && (r.response.cancelStatus || r.response)) || "").slice(0, 140))}`, ok ? "ok" : "err");
        }
        refreshTables();
      } catch (e) { toast(`Cancel failed: ${e.message}`, "err"); }
    }));
    body.querySelectorAll(".sym-link").forEach(a => a.addEventListener("click", (e) => { e.preventDefault(); selectSymbol(a.dataset.sym); }));
  } else {
    const fills = fillsOverride || [];
    if (!fills.length) { body.innerHTML = `<div class="empty">No recent fills</div>`; return; }
    body.innerHTML = `<table class="grid"><thead><tr>
      <th>Time</th><th>Symbol</th><th>Side</th><th class="num">Price</th><th class="num">Size</th></tr></thead><tbody>` +
      fills.map(f => `<tr>
        <td>${esc(String(f.fillTime || f.time || "").slice(0, 19).replace("T", " "))}</td>
        <td>${esc(f.symbol)}</td>
        <td class="${f.side === "buy" ? "up" : "down"}">${esc(f.side)}</td>
        <td class="num">${fmt(f.price)}</td>
        <td class="num">${fmt(f.size || f.qty)}</td>
      </tr>`).join("") + `</tbody></table>`;
  }
}

/* ---------- order ticket ---------- */

function updateTicketFields() {
  const isLimit = ["lmt", "post", "ioc"].includes(state.otype);
  const isTrigger = ["stp", "take_profit"].includes(state.otype);
  const isChase = state.otype === "chase";
  $("f-limit").style.display = isLimit ? "flex" : "none";
  $("f-stop").style.display = isTrigger ? "flex" : "none";
  $("btn-buy").textContent = state.otype === "mkt" ? "BUY / LONG" : "BUY";
  $("btn-sell").textContent = state.otype === "mkt" ? "SELL / SHORT" : "SELL";
  const t = state.tickers[state.symbol];
  if (t && isLimit && !$("in-limit").value) $("in-limit").placeholder = fmt(t.last);
  if (t && isTrigger && !$("in-stop").value) $("in-stop").placeholder = fmt(t.markPrice);
  if (isChase) {
    $("ticket-note").textContent = state.armed
      ? "CHASE: rests post-only at best bid/ask and re-pegs until filled (max 300s). Maker fees."
      : "CHASE requires an armed terminal.";
    $("ticket-note").style.color = state.armed ? "var(--accent)" : "var(--muted)";
  } else {
    $("ticket-note").textContent = state.armed
      ? `LIVE: orders go straight to Kraken (${state.env}).`
      : "SIMULATION: arm the terminal to send real orders.";
    $("ticket-note").style.color = state.armed ? "var(--red)" : "var(--muted)";
  }
}

function ticketPayload(side) {
  const size = Number($("in-size").value);
  if (!Number.isFinite(size) || size <= 0) { toast("Enter a size.", "err"); return null; }
  const body = { symbol: state.symbol, side, orderType: state.otype, size };
  if (["lmt", "post", "ioc"].includes(state.otype)) {
    const lp = Number($("in-limit").value);
    if (!Number.isFinite(lp) || lp <= 0) { toast("Enter a limit price.", "err"); return null; }
    body.limitPrice = lp;
  }
  if (["stp", "take_profit"].includes(state.otype)) {
    const sp = Number($("in-stop").value);
    if (!Number.isFinite(sp) || sp <= 0) { toast("Enter a trigger price.", "err"); return null; }
    body.stopPrice = sp;
    body.triggerSignal = "mark";
  }
  if ($("in-reduce").checked) body.reduceOnly = true;
  return body;
}

async function submitOrder(side) {
  if (state.otype === "chase") return submitChase(side);
  const body = ticketPayload(side);
  if (!body) return;
  try {
    const r = await api("/api/order", { method: "POST", body });
    if (r.simulated) {
      toast(`<b>Simulated order</b> — ${esc(body.side)} ${fmt(body.size)} ${esc(body.symbol)} @ ${esc(body.orderType)}${body.limitPrice ? " " + fmt(body.limitPrice) : ""}. Terminal is disarmed.`, "warn", 9000);
    } else if (r.error) {
      toast(`<b>Order rejected by Kraken:</b> ${esc(r.error)}`, "err", 10000);
    } else {
      const st = r.response && r.response.sendStatus;
      toast(`Order sent: ${esc(body.side)} ${fmt(body.size)} ${esc(body.symbol)}. Status: ${esc(String((st && st.orderEvents && st.orderEvents[0] && st.orderEvents[0].orderEvent) || "accepted"))}`, "ok");
    }
    refreshTables(); refreshAccount();
  } catch (e) {
    toast(`Order failed: ${esc(e.message)}`, "err", 8000);
  }
}

async function submitChase(side) {
  const size = Number($("in-size").value);
  if (!Number.isFinite(size) || size <= 0) { toast("Enter a size.", "err"); return; }
  if (!state.armed) { toast("CHASE requires an armed terminal — arm it first.", "warn"); return; }
  try {
    const r = await api("/api/chase", { method: "POST", body: { symbol: state.symbol, side, size } });
    const c = r.chase;
    toast(`Chase ${esc(c.id)} running: ${esc(side)} ${fmt(size)} ${esc(state.symbol)} — pegging best ${side === "buy" ? "bid" : "ask"} (max 300s). Fills will ping.`, "ok", 9000);
  } catch (e) {
    toast(`Chase failed: ${esc(e.message)}`, "err");
  }
}

async function closePosition(symbol) {
  const p = lastPositions.find(x => x.symbol === symbol);
  if (!p) return;
  if (!confirm(`Close ${p.side} position on ${symbol} (${fmt(p.size)} contracts) with a reduce-only market order?`)) return;
  try {
    const r = await api("/api/order", {
      method: "POST",
      body: {
        symbol, orderType: "mkt", size: Number(p.size),
        side: p.side === "long" ? "sell" : "buy", reduceOnly: true,
      },
    });
    if (r.simulated) toast("Close simulated — terminal is disarmed.", "warn");
    else toast("Close order sent.", r.response && r.response.result === "success" ? "ok" : "err");
    refreshTables(); refreshAccount();
  } catch (e) { toast(`Close failed: ${e.message}`, "err"); }
}

async function cancelAllForSymbol() {
  const mine = lastOrders.filter(o => o.symbol === state.symbol);
  if (!mine.length) { toast(`No open orders on ${state.symbol}.`, ""); return; }
  if (!confirm(`Cancel ${mine.length} open order(s) on ${state.symbol}?`)) return;
  for (const o of mine) {
    try { await api("/api/cancel", { method: "POST", body: { cliOrdId: o.cliOrdId || undefined, orderId: o.order_id || undefined } }); }
    catch (e) { toast(`Cancel failed for ${o.cliOrdId || o.order_id}: ${e.message}`, "err"); }
  }
  toast(`Cancel-all done for ${state.symbol}.`, "ok");
  refreshTables();
}

/* ---------- arm toggle ---------- */

async function setArmed(v) {
  state.armed = !!v;
  const btn = $("arm-btn");
  btn.classList.toggle("armed", state.armed);
  btn.textContent = state.armed ? "ARMED — click to disarm" : "DISARMED — click to arm";
  $("armed-badge").style.display = state.armed ? "" : "none";
  updateTicketFields();
}

async function armToggle() {
  if (state.armed) {
    const r = await api("/api/arm", { method: "POST", body: { armed: false } }).catch(() => null);
    if (r) setArmed(r.armed);
    return;
  }
  if (state.env === "live") {
    const answer = prompt(`This enables LIVE order entry on your ${state.env.toUpperCase()} Kraken Futures account.\nOrders you place will be real.\n\nType ARM to continue:`);
    if (answer !== "ARM") { toast("Arm cancelled.", ""); return; }
  }
  try {
    const r = await api("/api/arm", { method: "POST", body: { armed: true, confirm: "yes" } });
    setArmed(r.armed);
    if (r.armed) toast("Order entry ARMED. Orders are now live.", "warn");
  } catch (e) { toast(`Arm failed: ${e.message}`, "err"); }
}

/* ---------- size quick buttons ---------- */

function sizeFromPct(pct) {
  const t = state.tickers[state.symbol];
  const avail = Number($("acct-avail").dataset.raw || 0);
  if (!t || !t.last || !avail) { toast("No price or margin data for sizing.", "err"); return; }
  const inst = state.instruments.find(i => i.symbol === state.symbol) || {};
  const mult = Number(inst.contractSize || 1);
  const isInverse = inst.type === "futures_inverse";
  // notional = available margin × % × leverage; contracts follow the contract type
  const notional = avail * (pct / 100) * state.lev;
  const size = isInverse ? notional / mult : notional / (mult * Number(t.last));
  const instPrec = Math.max(0, Math.min(8, Number(inst.contractValueTradePrecision ?? 2)));
  $("in-size").value = size.toFixed(instPrec);
}

/* ---------- fill alerts ---------- */

let audioCtx = null;

function ensureAudio() {
  if (!audioCtx) {
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {}
  }
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

function playChime() {
  const ctx = ensureAudio();
  if (!ctx) return;
  const now = ctx.currentTime;
  for (const [freq, offset] of [[880, 0], [1318.51, 0.12]]) { // A5 → E6 two-tone ping
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, now + offset);
    gain.gain.exponentialRampToValueAtTime(0.2, now + offset + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.35);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now + offset);
    osc.stop(now + offset + 0.4);
  }
}

let lastFillSig = null;

async function checkFills() {
  try {
    const f = await api("/api/fills");
    const fills = (f.fills || []).filter(x => x && x.fillTime && !x.error);
    if (!fills.length) return; // transient empty/error payload — keep the previous baseline
    const key = (x) => JSON.stringify([x.fillTime, x.symbol, x.side, x.price, x.size || x.qty]);
    const sig = JSON.stringify(fills.map(key));
    if (lastFillSig === null) { lastFillSig = sig; return; } // baseline, no chime on boot
    if (sig === lastFillSig) return;
    const known = new Set((lastFillSig ? JSON.parse(lastFillSig) : []).map(JSON.stringify));
    const cutoff = Date.now() - 10 * 60 * 1000; // never celebrate history: only fills younger than 10 min
    const fresh = fills.filter(x => !known.has(key(x)) && new Date(x.fillTime).getTime() > cutoff);
    for (const x of fresh.slice(-3))
      toast(`<b>Fill:</b> ${esc(x.side)} ${fmt(x.size || x.qty)} ${esc(x.symbol)} @ ${fmt(x.price)}`, x.side === "buy" ? "ok" : "err", 8000);
    if (state.soundOn && fresh.length) playChime();
    lastFillSig = sig;
  } catch (e) {}
}

/* ---------- AI chat ---------- */

function renderChat() {
  const box = $("chat-messages");
  box.innerHTML = state.chat.map((m, i) => {
    if (m.role === "user") return `<div class="msg user">${esc(m.content)}</div>`;
    return `<div class="msg ai" data-idx="${i}">${renderAiText(m.content)}${renderProposals(m.proposals, i)}${renderActionBlocks(m.actionBlocks, i)}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
  box.querySelectorAll("[data-fill]").forEach(btn => btn.addEventListener("click", () => {
    const msg = btn.closest(".msg.ai");
    fillTicketFromProposal(JSON.parse(btn.dataset.fill), Number(msg.dataset.idx), Number(btn.dataset.pidx));
    renderChat();
  }));
  box.querySelectorAll("[data-execute]").forEach(btn => btn.addEventListener("click", () => {
    const msg = btn.closest(".msg.ai");
    executeActions(JSON.parse(btn.dataset.execute), Number(msg.dataset.idx), Number(btn.dataset.bidx), btn);
  }));
}

function renderAiText(text) {
  let out = esc(text);
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => `<pre><code>${code.trim()}</code></pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  return out.replace(/\n/g, "<br>");
}

function renderProposals(proposals, msgIdx) {
  if (!proposals || !proposals.length) return "";
  return proposals.map((p, i) => {
    const done = (state.chat[msgIdx].loaded || []).includes(i);
    return `<div class="proposal">
      <b>Order proposal</b>
      <div class="p-row"><span class="k">Symbol</span><span>${esc(p.symbol)}</span></div>
      <div class="p-row"><span class="k">Side</span><span class="${p.side === "buy" ? "up" : "down"}">${esc(p.side)}</span></div>
      <div class="p-row"><span class="k">Type</span><span>${esc(p.orderType)}</span></div>
      <div class="p-row"><span class="k">Size</span><span>${esc(p.size)}</span></div>
      ${p.limitPrice ? `<div class="p-row"><span class="k">Limit</span><span>${esc(p.limitPrice)}</span></div>` : ""}
      ${p.stopPrice ? `<div class="p-row"><span class="k">Trigger</span><span>${esc(p.stopPrice)}</span></div>` : ""}
      <button data-fill='${esc(JSON.stringify(p))}' data-pidx="${i}" ${done ? "disabled" : ""}>${done ? "Loaded into ticket" : "Fill order ticket →"}</button>
    </div>`;
  }).join("");
}

function describeAction(a) {
  switch (a.type) {
    case "order": return `${a.side} ${fmt(a.size)} ${a.symbol} ${a.orderType}${a.limitPrice ? " @ " + fmt(a.limitPrice) : ""}${a.stopPrice ? " trg " + fmt(a.stopPrice) : ""}${a.reduceOnly ? " reduce-only" : ""}`;
    case "ladder": return `${a.side} grid $${fmt(a.notional)} × ${a.orders} orders over ${a.depthPercent}% (${a.orderType || "post"}) on ${a.symbol}`;
    case "chase": return `chase ${a.side} ${fmt(a.size)} ${a.symbol} post-only @ best ${a.side === "buy" ? "bid" : "ask"}${a.timeoutSec ? `, max ${a.timeoutSec}s` : ""}`;
    case "close": return `close ${a.percent != null ? a.percent + "%" : fmt(a.size) + " ctr"} of ${a.symbol}`;
    case "replace_tp": return `replace TP on ${a.symbol} → ${fmt(a.stopPrice)} (mark)`;
    case "cancel_all": return `cancel all ${a.symbol}`;
    case "cancel": return `cancel ${String(a.cliOrdId).slice(0, 12)}…`;
    default: return JSON.stringify(a).slice(0, 80);
  }
}

function renderActionBlocks(blocks) {
  if (!blocks || !blocks.length) return "";
  return blocks.map((acts, bi) => `
    <div class="proposal">
      <b>Trade actions (${acts.length})</b>
      ${acts.map(a => `<div class="p-row"><span class="k">${esc(a.type || "?")}</span><span>${esc(describeAction(a))}</span></div>`).join("")}
      <button data-execute='${esc(JSON.stringify(acts))}' data-bidx="${bi}">Execute ${acts.length} action${acts.length > 1 ? "s" : ""} →</button>
    </div>`).join("");
}

/* verify each executed action against the live book; report what actually happened */
async function verifyExecution(r, acts, execStart) {
  const afterIds = new Set(lastOrders.map(o => o.cliOrdId || o.order_id || o.orderId).filter(Boolean));
  const afterSigs = new Set(lastOrders.filter(o => !o.error).map(o => JSON.stringify([o.symbol, o.side, o.orderType, o.limitPrice ?? null, o.stopPrice ?? null])));
  const sigOf = (o) => JSON.stringify([o.symbol, o.side, o.orderType, o.limitPrice ?? null, o.stopPrice ?? null]);
  const vlines = [];
  let fillsChecked = false, fills = [];
  for (let i = 0; i < (r.results || []).length; i++) {
    const res = r.results[i];
    // Kraken echoes tick-rounded prices back; verify against what was actually sent
    const a = res.order ? { ...res.order, type: res.type } : (res.params ? { ...res.params, type: res.type } : (acts[i] || {}));
    if (res.error) { vlines.push(`✗ ${describeAction(a)} — ${res.error}`); continue; }
    if (res.simulated) { vlines.push(`• ${describeAction(a)} — simulated only (disarmed)`); continue; }
    if (a.type === "order") {
      if (a.orderType === "mkt") { vlines.push(`✓ market order sent: ${describeAction(a)}`); continue; }
      if (afterSigs.has(sigOf(a))) { vlines.push(`✓ live on book: ${describeAction(a)}`); continue; }
      // not resting — either it crossed and died, or it filled instantly
      if (!fillsChecked) {
        fillsChecked = true;
        try { fills = (await api("/api/fills")).fills || []; } catch (e) { fills = []; }
      }
      const fill = fills.find(x => x.symbol === a.symbol && x.side === a.side && new Date(x.fillTime).getTime() >= execStart - 3000);
      vlines.push(fill ? `✓ filled immediately: ${a.side} ${fmt(a.size)} ${a.symbol} @ ${fmt(fill.price)}`
                       : `⚠ not on book, no fill — post-only likely crossed and was rejected: ${describeAction(a)}`);
    } else if (a.type === "ladder") {
      const want = res.orders || [];
      const got = want.filter(o => afterSigs.has(sigOf(o))).length;
      vlines.push(got === want.length ? `✓ grid live: ${got} rungs on book` : `⚠ grid partially on book: ${got}/${want.length}`);
    } else if (a.type === "cancel" || a.type === "cancel_all") {
      const targets = (res.results || []).map(x => x.target).filter(Boolean);
      if (!targets.length) { vlines.push(`✓ nothing to cancel`); continue; }
      const gone = targets.every(t => !afterIds.has(t.cliOrdId || t.order_id));
      vlines.push(gone ? `✓ canceled ${targets.length} order(s) — confirmed off book` : `⚠ some canceled orders still on book`);
    } else if (a.type === "replace_tp") {
      const tp = res.order || {};
      const cancelOk = (res.cancelResults || []).every(x => !x.error);
      vlines.push(cancelOk && afterSigs.has(sigOf(tp)) ? `✓ TP replaced @ ${fmt(tp.stopPrice)}` : `⚠ TP replace incomplete — check Orders tab`);
    } else if (a.type === "close") {
      vlines.push(`✓ reduce-only market close sent`);
    }
  }
  return vlines;
}

async function executeActions(acts, msgIdx, bidx, btn) {
  if (state.armed && !confirm(`Send ${acts.length} LIVE action(s) to Kraken (${state.env})?`)) return;
  btn.disabled = true;
  btn.textContent = "Executing…";
  let r;
  try {
    r = await api("/api/action", { method: "POST", body: { actions: acts } });
  } catch (e) {
    toast(`Execute failed: ${esc(e.message)}`, "err");
    btn.disabled = false;
    btn.textContent = "Execute…";
    return;
  }
  const sim = !r.armed;
  const lines = (r.results || []).map(res => res.error ? `${res.type || "?"}: ${res.error}` : `${res.type || "?"} ok`);
  toast(`${sim ? "<b>Simulated</b> (disarmed) — " : "<b>Sent</b> — "}${esc(lines.join(" · "))}`, sim ? "warn" : "ok", 9000);

  // verify against the live book, then report in chat
  const execStart = Date.now();
  await new Promise(res => setTimeout(res, 900)); // let the book settle
  await refreshTables();
  const vlines = await verifyExecution(r, acts, execStart);
  const report = `${sim ? "⚠️" : vlines.some(v => v.startsWith("✗") || v.startsWith("⚠")) ? "⚠️" : "✅"} Execution report${sim ? " — SIMULATED, nothing sent to Kraken" : ""}:\n` + vlines.join("\n");
  state.chat.push({ role: "assistant", content: report });
  api("/api/chat/note", { method: "POST", body: { role: "assistant", content: report } }).catch(() => {});
  state.lastExecution = {
    mode: sim ? "SIMULATED (terminal was disarmed — nothing was sent to Kraken)" : "LIVE (sent to Kraken)",
    results: lines,
    verification: vlines,
    when: new Date().toISOString(),
  };

  // executed card leaves the chat — the report replaces it
  const msg = state.chat[msgIdx];
  if (msg && Array.isArray(msg.actionBlocks)) msg.actionBlocks.splice(bidx, 1);
  renderChat();
}

function fillTicketFromProposal(p, msgIdx, pIdx) {
  if (msgIdx !== undefined) {
    const loaded = state.chat[msgIdx].loaded || (state.chat[msgIdx].loaded = []);
    if (!loaded.includes(pIdx)) loaded.push(pIdx);
  }
  if (p.symbol && p.symbol !== state.symbol) selectSymbol(p.symbol.toUpperCase());
  if (p.orderType && ["mkt", "lmt", "post", "stp", "take_profit", "ioc"].includes(p.orderType)) {
    state.otype = p.orderType;
    document.querySelectorAll(".ord-tab").forEach(b => b.classList.toggle("active", b.dataset.otype === p.orderType));
  }
  if (p.limitPrice) $("in-limit").value = p.limitPrice;
  if (p.stopPrice) $("in-stop").value = p.stopPrice;
  if (p.size) $("in-size").value = p.size;
  toast("Proposal loaded into the order ticket. Review, then click BUY or SELL.", "", 6000);
  updateTicketFields();
}

async function loadChatHistory() {
  try {
    const h = await api("/api/chat/history");
    state.chat = (h.messages || []).map(m => ({
      role: m.role,
      content: m.content,
      proposals: m.meta && m.meta.orderProposals,
      actionBlocks: m.meta && m.meta.actionProposals,
    }));
  } catch (e) { /* fresh start */ }
}

async function sendChat() {
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  state.chat.push({ role: "user", content: text });
  renderChat();
  $("chat-send").disabled = true;
  try {
    // the server owns chat history: send only the new message + live context
    const r = await api("/api/chat", {
      method: "POST",
      body: { message: text, symbol: state.symbol, lastExecution: state.lastExecution || null },
    });
    state.lastExecution = null; // annotate only the immediate follow-up
    state.chat.push({ role: "assistant", content: r.text || "(empty response)", proposals: r.orderProposals, actionBlocks: r.actionProposals });
  } catch (e) {
    state.chat.push({ role: "assistant", content: `⚠ ${e.message}` });
  } finally {
    $("chat-send").disabled = false;
    renderChat();
  }
}

/* ---------- boot ---------- */

async function boot() {
  initChart();

  try {
    const h = await api("/api/health");
    state.armed = h.armed;
    state.env = h.env;
    state.hasKeys = h.hasKeys;
    $("env-badge").textContent = h.env.toUpperCase();
    $("env-badge").classList.toggle("live", h.env === "live");
    setArmed(h.armed);
    if (!h.hasKeys) toast("No API keys found — private data (account, positions, trading) is unavailable.", "warn", 10000);
  } catch (e) {
    toast(`Server health check failed: ${e.message}`, "err");
  }

  try {
    const inst = await api("/api/instruments");
    state.instruments = (inst.instruments || []).filter(i => i.tradeable !== false);
    state.instrumentsLoaded = true;
  } catch (e) {}

  try {
    const t = await api(`/api/tickers?symbols=${encodeURIComponent(state.symbol)}`);
    state.watchlist = t.watchlist || [];
    state.tickers = t.tickers || {};
  } catch (e) {}

  renderWatchlist();
  renderHeader(state.tickers[state.symbol]);
  loadChart();
  connectStream();
  refreshAccount();
  refreshTables();
  refreshBook();

  // timeframe buttons
  const tfBar = $("tf-bar");
  for (const res of Object.keys(RES_SECONDS)) {
    const b = document.createElement("button");
    b.className = "tf-btn" + (res === state.res ? " active" : "");
    b.textContent = res;
    b.addEventListener("click", () => {
      state.res = res;
      localStorage.setItem("kt.res", res);
      tfBar.querySelectorAll(".tf-btn").forEach(x => x.classList.toggle("active", x === b));
      loadChart();
    });
    tfBar.appendChild(b);
  }

  // bottom tabs
  document.querySelectorAll(".tab-btn").forEach(btn => btn.addEventListener("click", () => {
    state.tab = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach(x => x.classList.toggle("active", x === btn));
    if (state.tab === "fills") refreshFills();
    else if (state.tab === "scanner") refreshScanner();
    else renderTab();
  }));
  $("btn-cancel-all").addEventListener("click", cancelAllForSymbol);
  $("btn-refresh").addEventListener("click", () => { refreshAccount(); refreshTables(); refreshBook(); if (state.tab === "fills") refreshFills(); if (state.tab === "scanner") refreshScanner(); });

  // search
  $("symbol-search").addEventListener("input", (e) => renderSearchResults(e.target.value.trim()));
  document.addEventListener("click", (e) => {
    if (!$("search-results").contains(e.target) && e.target !== $("symbol-search")) $("search-results").style.display = "none";
  });

  // ticket
  document.querySelectorAll(".ord-tab").forEach(b => b.addEventListener("click", () => {
    state.otype = b.dataset.otype;
    document.querySelectorAll(".ord-tab").forEach(x => x.classList.toggle("active", x === b));
    updateTicketFields();
  }));
  document.querySelectorAll(".size-quick button").forEach(b => b.addEventListener("click", () => sizeFromPct(Number(b.dataset.pct))));
  const levBtns = document.querySelectorAll("#lev-quick button");
  const renderLev = () => levBtns.forEach(b => b.classList.toggle("active", Number(b.dataset.lev) === state.lev));
  levBtns.forEach(b => b.addEventListener("click", () => {
    state.lev = Number(b.dataset.lev);
    localStorage.setItem("kt.lev", String(state.lev));
    renderLev();
  }));
  renderLev();
  $("btn-buy").addEventListener("click", () => submitOrder("buy"));
  $("btn-sell").addEventListener("click", () => submitOrder("sell"));
  $("arm-btn").addEventListener("click", armToggle);
  const proToggle = $("pro-toggle");
  proToggle.checked = state.pro;
  proToggle.addEventListener("change", () => {
    state.pro = proToggle.checked;
    localStorage.setItem("kt.pro", state.pro ? "1" : "0");
    refreshAccount();
    toast(state.pro ? "Pro-Mode on: Balance and Avail margin shown +$7,800 (display only — orders use real margin)." : "Pro-Mode off: balances are real.", "warn", 6000);
  });

  // chat
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat(); });
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  updateTicketFields();
  await loadChatHistory();
  renderChat();
  setInterval(() => advanceBuckets(currentBucket()), 5000); // keep quiet minutes flowing
  refreshSignal();
  setInterval(refreshSignal, 60000);
  setInterval(refreshScanner, 30000); // no-ops unless the scanner tab is open
  setInterval(checkFills, 5000);

  const bell = $("bell-btn");
  const renderBell = () => {
    bell.textContent = state.soundOn ? "🔔" : "🔕";
    bell.classList.toggle("off", !state.soundOn);
    bell.title = state.soundOn ? "Fill sound on — click to mute" : "Fill sound muted — click to enable";
  };
  bell.addEventListener("click", () => {
    state.soundOn = !state.soundOn;
    localStorage.setItem("kt.sound", state.soundOn ? "1" : "0");
    renderBell();
    if (state.soundOn) playChime(); // feedback + browser audio unlock
  });
  renderBell();

  if (state.chat.length === 0) {
    state.chat.push({
      role: "assistant",
      content: "Live account and market context is loaded. Ask about prices, your positions or risk — or ask me to draft an order and I'll give you a fill-in ticket.",
    });
    renderChat();
  }
  setInterval(refreshAccount, 5000);
  setInterval(refreshTables, 5000);
  setInterval(refreshBook, 2500);
  setInterval(refreshFills, 15000);
}

boot();
