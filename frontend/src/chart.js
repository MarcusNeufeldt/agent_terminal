/* Imperative lightweight-charts controller (v5). React mounts it once and the
   store drives it — charting never goes through React's render cycle. */

import * as LightweightCharts from "lightweight-charts";

export class ChartController {
  constructor(onNote) {
    this.onNote = onNote;
    this.chart = null;
    this.candleSeries = null;
    this.priceLines = [];
    this.overlayPrices = [];
    this.tpLines = [];
    this._drag = null;
    this._dragRaf = 0;
    this._handleRaf = 0;
    this._protection = null;
    this._protectionHandle = null;
    this.orderButtons = [];
    this._listeners = null;
    this.onTpDrop = null; // set by the store: (symbol, newPrice) => Promise<boolean>
    this.onProtectionDrop = null; // set by the store: ({symbol, kind, price, pnl}) => Promise<boolean>
    this.onOrderCancel = null; // set by the store: (order) => Promise<boolean>
  }

  mount(container, symbol, instruments) {
    this.chart = LightweightCharts.createChart(container, {
      layout: { background: { type: "solid", color: "#0b0e14" }, textColor: "#787b86", fontSize: 11 },
      grid: { vertLines: { color: "#161b27" }, horzLines: { color: "#161b27" } },
      rightPriceScale: { borderColor: "#1e2430" },
      timeScale: { borderColor: "#1e2430", timeVisible: true, secondsVisible: false },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    this.candleSeries = this.chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: "#26a69a", downColor: "#ef5350",
      borderUpColor: "#26a69a", borderDownColor: "#ef5350",
      wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    });
    this.candleSeries.applyOptions({
      autoscaleInfoProvider: (original) => {
        const res = original ? original() : {};
        if (!this.overlayPrices.length) return res;
        let min = res.priceRange ? res.priceRange.minValue : Infinity;
        let max = res.priceRange ? res.priceRange.maxValue : -Infinity;
        for (const v of this.overlayPrices) { if (v < min) min = v; if (v > max) max = v; }
        if (!Number.isFinite(min) || !Number.isFinite(max)) return res;
        const pad = (max - min) * 0.04 || Math.abs(min) * 0.005 || 1;
        return { ...res, priceRange: { minValue: min - pad, maxValue: max + pad } };
      },
    });
    this.ro = new ResizeObserver(() => {
      this.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      this._scheduleProtectionHandle();
    });
    this.ro.observe(container);
    this.applyPriceFormat(symbol, instruments);
    this._bindTpDrag(container);
    this._bindProtectionHandle(container);
  }

  destroy() {
    try { if (this.ro) this.ro.disconnect(); } catch {}
    if (this._listeners && this._container) {
      const l = this._listeners;
      this._container.removeEventListener("mousedown", l.down, true);
      this._container.removeEventListener("mousemove", l.hover);
      this._container.removeEventListener("wheel", l.viewport);
      document.removeEventListener("mousemove", l.move);
      document.removeEventListener("mouseup", l.up);
      this._protectionHandle?.removeEventListener("mousedown", l.protectionDown);
    }
    if (this._dragRaf) cancelAnimationFrame(this._dragRaf);
    if (this._handleRaf) cancelAnimationFrame(this._handleRaf);
    this._protectionHandle?.remove();
    for (const item of this.orderButtons) item.button.remove();
    this.orderButtons = [];
    try { if (this.chart) this.chart.remove(); } catch {}
    this.chart = null;
  }

  applyPriceFormat(symbol, instruments) {
    if (!this.candleSeries) return;
    const inst = (instruments || []).find(i => i.symbol === symbol) || {};
    const tick = Number(inst.tickSize) || 0.01;
    const decimals = Math.max(0, Math.min(10, Math.ceil(-Math.log10(tick) - 1e-9)));
    this.candleSeries.applyOptions({ priceFormat: { type: "price", precision: decimals, minMove: tick } });
  }

  setData(candles) {
    if (!this.candleSeries) return;
    this.candleSeries.setData(candles.map(c => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] })));
    if (candles.length) this.chart.timeScale().fitContent();
    this._scheduleProtectionHandle();
  }

  updateBar(c) {
    if (!this.candleSeries) return;
    this.candleSeries.update({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] });
    this._scheduleProtectionHandle();
  }

  appendBar(c) {
    if (!this.candleSeries) return;
    this.candleSeries.update({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] });
    this._scheduleProtectionHandle();
  }

  clearOverlays() {
    if (this._dragRaf) { cancelAnimationFrame(this._dragRaf); this._dragRaf = 0; }
    if (this._drag) this.chart?.applyOptions({ handleScroll: true, handleScale: true });
    this._protectionHandle?.classList.remove("dragging");
    for (const item of this.orderButtons) item.button.remove();
    this.orderButtons = [];
    for (const line of this.priceLines) { try { this.candleSeries.removePriceLine(line); } catch {} }
    this.priceLines = [];
    this.overlayPrices = [];
    this.tpLines = [];
    this._drag = null;
    this._setProtection(null);
  }

  setOverlays(overlays) {
    if (!this.candleSeries) return;
    this.clearOverlays();
    let protection = null;
    for (const o of overlays) {
      if (o.price === null || o.price === undefined || !Number.isFinite(Number(o.price))) continue;
      this.overlayPrices.push(Number(o.price));
      const line = this.candleSeries.createPriceLine({
        price: Number(o.price), color: o.color, lineWidth: 1,
        lineStyle: o.dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
        title: o.title, axisLabelVisible: true,
      });
      this.priceLines.push(line);
      if (o.tp) this.tpLines.push({ line, price: Number(o.price), origPrice: Number(o.price), ...o.tp });
      if (o.order) this._addOrderButton(o.order, Number(o.price));
      if (o.position) protection = o.position;
    }
    this._setProtection(protection);
  }

  _bindTpDrag(container) {
    if (this._tpBound) return;
    this._tpBound = true;
    this._container = container;
    this._listeners = {
      down: e => this._tpDown(e),
      hover: e => { this._tpHover(e); this._scheduleProtectionHandle(); },
      move: e => this._tpDragMove(e),
      up: e => this._tpUp(e),
      viewport: () => this._scheduleProtectionHandle(),
      protectionDown: e => this._protectionDown(e),
    };
    container.addEventListener("mousedown", this._listeners.down, true);
    container.addEventListener("mousemove", this._listeners.hover);
    container.addEventListener("wheel", this._listeners.viewport, { passive: true });
    document.addEventListener("mousemove", this._listeners.move);
    document.addEventListener("mouseup", this._listeners.up);
  }

  _bindProtectionHandle(container) {
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "chart-protection-handle";
    handle.textContent = "↕ TP / SL";
    handle.title = "Drag from the position: profit side creates TP, loss side creates SL";
    handle.setAttribute("aria-label", handle.title);
    handle.hidden = true;
    handle.addEventListener("mousedown", this._listeners.protectionDown);
    container.appendChild(handle);
    this._protectionHandle = handle;
    this._setProtection(this._protection);
  }

  _setProtection(protection) {
    this._protection = protection;
    if (!this._protectionHandle) return;
    this._protectionHandle.dataset.side = protection?.side || "";
    this._protectionHandle.hidden = !protection;
    this._scheduleProtectionHandle();
  }

  _addOrderButton(order, price) {
    if (!this._container || !order || !Number.isFinite(price)) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chart-order-cancel";
    button.textContent = "×";
    button.title = `Cancel ${order.orderType || "order"} ${order.side || ""} ${order.symbol || ""} @ ${price}`;
    button.setAttribute("aria-label", button.title);
    button.addEventListener("mousedown", e => { e.stopPropagation(); e.preventDefault(); });
    button.addEventListener("click", e => {
      e.stopPropagation();
      if (!this.onOrderCancel || button.disabled) return;
      button.disabled = true;
      Promise.resolve().then(() => this.onOrderCancel(order)).finally(() => {
        if (button.isConnected) button.disabled = false;
      });
    });
    this._container.appendChild(button);
    this.orderButtons.push({ button, order, price });
  }

  _scheduleProtectionHandle() {
    if (this._handleRaf || !this.candleSeries || (!this._protection && !this.orderButtons.length)) return;
    this._handleRaf = requestAnimationFrame(() => {
      this._handleRaf = 0;
      if (this._protectionHandle && this._protection) {
        const y = this.candleSeries?.priceToCoordinate(Number(this._protection.entry));
        this._protectionHandle.hidden = y === null || !Number.isFinite(Number(y));
        if (!this._protectionHandle.hidden) {
          this._protectionHandle.style.transform = `translateY(${Math.round(Number(y) - this._protectionHandle.offsetHeight / 2)}px)`;
        }
      }
      const rows = [];
      for (const item of this.orderButtons) {
        const y = this.candleSeries?.priceToCoordinate(item.price);
        item.button.hidden = y === null || !Number.isFinite(Number(y));
        if (item.button.hidden) continue;
        const slot = rows.filter(row => Math.abs(row - Number(y)) < 18).length;
        rows.push(Number(y));
        item.button.style.right = `${194 + slot * 22}px`;
        item.button.style.transform = `translateY(${Math.round(Number(y) - item.button.offsetHeight / 2)}px)`;
      }
    });
  }

  _protectionDown(e) {
    if (e.button !== 0 || !this._protection || !this.candleSeries) return;
    const p = this._protection;
    const line = this.candleSeries.createPriceLine({
      price: p.entry, color: "#f0b90b", lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      title: "Drag for TP / SL", axisLabelVisible: true,
    });
    this.priceLines.push(line);
    this._drag = { ...p, mode: "protection", line, price: p.entry, origPrice: p.entry, kind: "be", pnl: 0 };
    this._protectionHandle.classList.add("dragging");
    this.chart.applyOptions({ handleScroll: false, handleScale: false });
    e.stopPropagation();
    e.preventDefault();
  }

  _tpY(e) {
    const r = this._container.getBoundingClientRect();
    return e.clientY - r.top;
  }

  _tpHit(e) {
    if (!this.candleSeries) return null;
    const y = this._tpY(e);
    for (const t of this.tpLines) {
      const ly = this.candleSeries.priceToCoordinate(t.price);
      if (ly !== null && Math.abs(y - ly) <= 8) return t;
    }
    return null;
  }

  _tpDown(e) {
    if (e.target?.closest?.(".chart-protection-handle, .chart-order-cancel")) return;
    const t = this._tpHit(e);
    if (!t) return;
    this._drag = { ...t, mode: "tp", kind: "tp" };
    e.stopPropagation();
    e.preventDefault();
    this.chart.applyOptions({ handleScroll: false, handleScale: false });
  }

  _tpHover(e) {
    if (this._drag) return;
    this._container.style.cursor = this._tpHit(e) ? "ns-resize" : "";
  }

  _tpDecimals(tick) {
    return Math.max(0, Math.min(10, Math.ceil(-Math.log10(tick || 0.01) - 1e-9)));
  }

  _applyDragY(y) {
    const t = this._drag;
    if (!t || !this.candleSeries) return;
    const price = this.candleSeries.coordinateToPrice(y);
    if (price === null || !Number.isFinite(Number(price))) return;
    const tick = t.tick || 0.01;
    t.price = Math.max(tick, Math.round(Number(price) / tick) * tick);
    t.pnl = t.dir * (t.price - t.entry) * t.size * (t.mult || 1);
    if (t.mode === "protection") t.kind = Math.abs(t.pnl) < 0.005 ? "be" : t.pnl > 0 ? "tp" : "sl";
    const dec = this._tpDecimals(tick);
    const label = `${t.pnl >= 0 ? "+" : "-"}$${Math.abs(t.pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const type = t.kind === "sl" ? "SL" : t.kind === "tp" ? "TP" : "BE";
    const color = t.kind === "sl" ? "#ef5350" : t.kind === "tp" ? "#26a69a" : "#f0b90b";
    t.line.applyOptions({ price: t.price, color, lineWidth: 2, title: `${type} ${t.price.toFixed(dec)} (${label})` });
  }

  _tpDragMove(e) {
    if (!this._drag || !this.candleSeries) return;
    this._pendingDragY = this._tpY(e);
    if (this._dragRaf) return;
    this._dragRaf = requestAnimationFrame(() => {
      this._dragRaf = 0;
      this._applyDragY(this._pendingDragY);
    });
  }

  _tpUp(e) {
    const t = this._drag;
    if (!t) return;
    if (this._dragRaf) { cancelAnimationFrame(this._dragRaf); this._dragRaf = 0; }
    if (e?.clientY !== undefined) this._applyDragY(this._tpY(e));
    this._drag = null;
    this._protectionHandle?.classList.remove("dragging");
    this.chart.applyOptions({ handleScroll: true, handleScale: true });

    if (t.mode === "protection") {
      try { this.candleSeries.removePriceLine(t.line); } catch {}
      this.priceLines = this.priceLines.filter(line => line !== t.line);
      if (t.kind !== "be" && t.price !== t.origPrice && this.onProtectionDrop) {
        this.onProtectionDrop({ symbol: t.symbol, kind: t.kind, price: t.price, pnl: t.pnl });
      }
      return;
    }
    if (t.price !== t.origPrice && this.onTpDrop) this.onTpDrop(t.symbol, t.price);
  }

  onPrice(last, prev) {
    if (prev !== null && prev !== undefined && last !== prev && this.onPriceChange) this.onPriceChange(last > prev);
    this._scheduleProtectionHandle();
  }
}
export default ChartController;
