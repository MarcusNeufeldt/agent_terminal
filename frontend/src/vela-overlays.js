import { registerNativeIndicator, registerRendererLayer } from "@luxalgo/vela/plugin";
import { mirroredRisk } from "./risk-preview.js";

const TYPE = "terminal-overlays";
const channels = new Map();
const targets = new Map();
let controllerSequence = 0;

function channel(id) {
  if (!channels.has(id)) channels.set(id, { data: null, listeners: new Set() });
  return channels.get(id);
}

function publish(id, data) {
  const item = channel(id);
  item.data = data;
  for (const listener of item.listeners) listener(data);
}

function subscribe(id, listener) {
  const item = channel(id);
  item.listeners.add(listener);
  if (item.data) listener(item.data);
  return () => {
    item.listeners.delete(listener);
    if (!item.listeners.size && !item.data) channels.delete(id);
  };
}

export function snapOverlayPrice(price, tick = 0.01) {
  const step = Number(tick) > 0 ? Number(tick) : 0.01;
  return Math.max(step, Math.round(Number(price) / step) * step);
}

function decimals(tick = 0.01) {
  return Math.max(0, Math.min(10, Math.ceil(-Math.log10(Number(tick) || 0.01) - 1e-9)));
}

function money(value) {
  return Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function overlayIntent(line) {
  const source = line?.protection || line?.tp || line?.position || {};
  const order = source.order || line?.order || {};
  return JSON.stringify([line?.key, line?.price, source.symbol, source.entry, source.size, source.dir,
    order.orderId, order.cliOrdId, order.snapshot?.unfilledSizeExact, order.snapshot?.stopPrice,
    order.snapshot?.limitPrice, order.snapshot?.triggerKind, order.snapshot?.triggerMarket, order.snapshot?.reduceOnly, order.snapshot?.positionTpsl]);
}

export function previewProtection(line, rawPrice) {
  const source = line.protection || line.tp || line.position;
  if (!source) return null;
  const price = snapOverlayPrice(rawPrice, source.tick);
  const pnl = source.dir * (price - source.entry) * source.size * (source.mult || 1);
  const kind = line.protection?.kind || (line.tp ? "tp" : price === source.entry ? "be" : pnl > 0 ? "tp" : "sl");
  const type = kind === "sl" ? "SL" : kind === "tp" ? "TP" : "BE";
  const color = kind === "sl" ? "#ef5350" : kind === "tp" ? "#26a69a" : "#f0b90b";
  return {
    ...line,
    price,
    color,
    dashed: true,
    title: `${type} ${price.toFixed(decimals(source.tick))} (${pnl >= 0 ? "+" : "-"}$${money(pnl)})`,
    drop: { symbol: source.symbol, kind, price, pnl, order: source.order || line.order || null, positionSnapshot: source.snapshot },
  };
}

export function riskOverlays(lines) {
  const candidates = (lines || []).filter(line => line.tp);
  const line = candidates.find(item => item.tp.fullPosition) || (candidates.length === 1 ? candidates[0] : null);
  if (!line) return [];
  const pnl = line.tp.dir * (line.price - line.tp.entry) * line.tp.size * (line.tp.mult || 1);
  if (!(pnl > 0)) return [];
  return [1, 2, 3].map(ratio => {
    const risk = mirroredRisk({
      entry: line.tp.entry,
      target: line.price,
      dir: line.tp.dir,
      ratio,
      tick: line.tp.tick,
      size: line.tp.size,
      mult: line.tp.mult || 1,
    });
    return {
      key: `risk:${line.key}:${ratio}`,
      price: risk.price,
      color: "#ef5350",
      dashed: true,
      risk: true,
      title: `RISK ${risk.price.toFixed(decimals(line.tp.tick))} (-$${money(risk.pnl)}) · 1:${ratio}`,
    };
  });
}

class TerminalOverlayIndicator {
  start(ctx, inputs) {
    this.ctx = ctx;
    ctx.emit({});
    this.setInputs(inputs);
  }

  setInputs(inputs) {
    const next = String(inputs.targetId || "");
    if (next === this.targetId && this.unsubscribe) return;
    this.unsubscribe?.();
    this.targetId = next;
    if (!next || !this.ctx) return;
    this.unsubscribe = subscribe(next, data => {
      const scalePrices = (data.scalePrices || []).filter(Number.isFinite);
      this.ctx.emit({
        priceLines: scalePrices.map((price, index) => ({
          id: `terminal-scale-${index}`,
          paneId: "price",
          price,
          color: "rgba(0,0,0,0)",
          width: 1,
        })),
      });
      this.ctx.pushData(data);
    });
  }

  onBars() {}
  onViewport() {}
  suspend() { this.unsubscribe?.(); this.unsubscribe = null; }
  resume() { const targetId = this.targetId; this.targetId = ""; this.setInputs({ targetId }); }
  stop() { this.unsubscribe?.(); this.unsubscribe = null; }
}

registerNativeIndicator({
  type: TYPE,
  title: "Terminal orders",
  shortTitle: "Orders",
  paneHint: "price",
  overlay: true,
  inputsSchema: () => [],
  defaultInputs: () => ({ targetId: "" }),
  create: () => new TerminalOverlayIndicator(),
});

export class TerminalOverlayLayer {
  mount(canvas) {
    this.canvas = canvas;
    this.plot = canvas.parentElement;
    this.pointerDown = event => this.onPointerDown(event);
    this.pointerMove = event => this.onPointerMove(event);
    this.plot.addEventListener("pointerdown", this.pointerDown, true);
    this.plot.addEventListener("pointermove", this.pointerMove, true);
  }

  destroy() {
    this.stopDrag();
    this.plot?.removeEventListener("pointerdown", this.pointerDown, true);
    this.plot?.removeEventListener("pointermove", this.pointerMove, true);
  }

  point(event) {
    const rect = this.plot.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  rowAt(point, kind) {
    const candidates = this.hits || [];
    if (kind === "cancel") return candidates.find(hit => hit.cancel && inside(point, hit.cancel));
    if (kind === "protection") return candidates.find(hit => hit.protection && inside(point, hit.protection));
    return candidates.find(hit => hit.draggable && Math.abs(point.y - hit.y) <= 7);
  }

  onPointerDown(event) {
    if (event.button !== 0 || !this.args) return;
    const point = this.point(event);
    const cancel = this.rowAt(point, "cancel");
    if (cancel) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const handler = targets.get(this.data?.targetId);
      if (!handler || this.pendingCancels?.has(cancel.line.key)) return;
      this.pendingCancels ??= new Set();
      this.pendingCancels.add(cancel.line.key);
      this.render(this.args);
      Promise.resolve().then(() => handler.cancel(cancel.line.order)).finally(() => {
        this.pendingCancels.delete(cancel.line.key);
        this.render(this.args);
      });
      return;
    }

    const hit = this.rowAt(point, "protection") || this.rowAt(point, "drag");
    if (!hit || this.pending?.size) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    this.drag = { line: hit.line, price: hit.line.price };
    this.dragTargetId = this.data?.targetId;
    targets.get(this.dragTargetId)?.dragging?.(true);
    this.windowMove = move => this.onDragMove(move);
    this.windowUp = up => this.onDragEnd(up);
    window.addEventListener("pointermove", this.windowMove, true);
    window.addEventListener("pointerup", this.windowUp, true);
    window.addEventListener("pointercancel", this.windowUp, true);
  }

  onPointerMove(event) {
    if (this.drag || !this.args) return;
    const point = this.point(event);
    const interactive = this.rowAt(point, "cancel") || this.rowAt(point, "protection") || this.rowAt(point, "drag");
    this.plot.style.cursor = interactive ? (interactive.cancel ? "pointer" : "ns-resize") : "";
  }

  onDragMove(event) {
    if (!this.drag || !this.args) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const point = this.point(event);
    const price = this.args.coords.yToPrice(point.y, this.args.scale, this.args.bounds);
    this.drag.price = snapOverlayPrice(price, (this.drag.line.protection || this.drag.line.tp || this.drag.line.position)?.tick);
    this.render(this.args);
  }

  onDragEnd(event) {
    if (!this.drag || !this.args) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const drag = this.drag;
    const targetId = this.dragTargetId;
    const current = this.data?.lines?.find(line => line.key === drag.line.key);
    if (event.type === "pointercancel" || targetId !== this.data?.targetId || !current ||
        overlayIntent(current) !== overlayIntent(drag.line)) {
      this.stopDrag();
      this.render(this.args);
      return;
    }
    const point = this.point(event);
    drag.price = snapOverlayPrice(
      this.args.coords.yToPrice(point.y, this.args.scale, this.args.bounds),
      (drag.line.protection || drag.line.tp || drag.line.position)?.tick,
    );
    this.stopDrag();
    const preview = previewProtection(drag.line, drag.price);
    if (!preview || preview.price === drag.line.price || preview.drop.kind === "be") {
      this.render(this.args);
      return;
    }
    const handler = targets.get(this.data?.targetId);
    if (!handler) return;
    this.pending ??= new Map();
    this.pending.set(drag.line.key, preview);
    this.render(this.args);
    Promise.resolve().then(() => handler.drop(drag.line, preview.drop)).finally(() => {
      this.pending.delete(drag.line.key);
      this.render(this.args);
    });
  }

  stopDrag() {
    if (this.windowMove) window.removeEventListener("pointermove", this.windowMove, true);
    if (this.windowUp) {
      window.removeEventListener("pointerup", this.windowUp, true);
      window.removeEventListener("pointercancel", this.windowUp, true);
    }
    this.windowMove = null;
    this.windowUp = null;
    this.drag = null;
    targets.get(this.dragTargetId)?.dragging?.(false);
    this.dragTargetId = null;
  }

  render(args) {
    this.args = args;
    this.data = args.data || { lines: [] };
    const canvas = this.canvas;
    const ctx = canvas?.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(args.coords.dpr, 0, 0, args.coords.dpr, 0, 0);

    let lines = (this.data.lines || []).map(line => this.pending?.get(line.key) || line);
    if (this.drag) lines = lines.map(line => line.key === this.drag.line.key ? previewProtection(line, this.drag.price) || line : line);
    if (this.data.riskEnabled) lines = [...lines, ...riskOverlays(lines)];

    const width = canvas.clientWidth;
    this.hits = [];
    for (const line of lines) {
      if (!Number.isFinite(Number(line.price))) continue;
      const y = args.coords.priceToY(Number(line.price), args.scale, args.bounds);
      if (!Number.isFinite(y) || y < args.bounds.top || y > args.bounds.top + args.bounds.height) continue;
      const hovered = args.cursor && Math.abs(args.cursor.y - y) <= 7;
      drawLine(ctx, line, y, width, hovered);
      const hit = { line, y, draggable: Boolean(line.tp || line.protection) };
      if (line.position) hit.protection = drawPill(ctx, 8, y, `${line.title} · ↕ TP / SL`, "#f0b90b");
      if (line.order) hit.cancel = drawCancel(ctx, width - 86, y, this.pendingCancels?.has(line.key));
      this.hits.push(hit);
    }
  }
}

function inside(point, rect) {
  return point.x >= rect.x && point.x <= rect.x + rect.w && point.y >= rect.y && point.y <= rect.y + rect.h;
}

function drawLine(ctx, line, y, width, hovered) {
  ctx.save();
  ctx.strokeStyle = line.color || "#4f8cff";
  ctx.globalAlpha = hovered ? 1 : line.risk ? 0.7 : 0.9;
  ctx.lineWidth = hovered || line.tp ? 2 : 1;
  ctx.setLineDash(line.dashed ? [5, 4] : []);
  ctx.beginPath();
  ctx.moveTo(0, Math.round(y) + 0.5);
  ctx.lineTo(width, Math.round(y) + 0.5);
  ctx.stroke();
  ctx.restore();

  if (line.position) return;
  const x = Math.max(8, width - 360);
  drawPill(ctx, x, y, line.title || String(line.price), line.color || "#4f8cff");
}

function drawPill(ctx, x, y, text, color) {
  ctx.save();
  ctx.font = "10px ui-monospace, SFMono-Regular, Consolas, monospace";
  const w = Math.ceil(ctx.measureText(text).width) + 10;
  const h = 18;
  const top = Math.round(y - h / 2);
  ctx.fillStyle = "rgba(11,14,20,.88)";
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.fillRect(x, top, w, h);
  ctx.strokeRect(x + 0.5, top + 0.5, w - 1, h - 1);
  ctx.fillStyle = color;
  ctx.textBaseline = "middle";
  ctx.fillText(text, x + 5, top + h / 2 + 0.5);
  ctx.restore();
  return { x, y: top, w, h };
}

function drawCancel(ctx, x, y, pending) {
  const w = 18;
  const h = 18;
  const top = Math.round(y - h / 2);
  ctx.save();
  ctx.fillStyle = pending ? "rgba(80,80,80,.8)" : "rgba(11,14,20,.92)";
  ctx.strokeStyle = "#ef5350";
  ctx.fillRect(x, top, w, h);
  ctx.strokeRect(x + 0.5, top + 0.5, w - 1, h - 1);
  ctx.fillStyle = "#ef5350";
  ctx.font = "bold 14px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(pending ? "…" : "×", x + w / 2, top + h / 2);
  ctx.restore();
  return { x, y: top, w, h };
}

registerRendererLayer({
  id: TYPE,
  placement: "above-data",
  repaintOnCursor: true,
  create: () => new TerminalOverlayLayer(),
});

export class VelaChartController {
  constructor(workspace) {
    this.workspace = workspace;
    this.id = `terminal-overlay-${++controllerSequence}`;
    this.cells = new Map();
    this.draggingCells = new Set();
    this.overlayMap = {};
    this.riskEnabled = false;
    workspace.cells().forEach(cell => this.bindCell(cell));
    this.offCreated = workspace.on("cell:created", ({ id }) => this.bindCell(workspace.cell(id)));
    this.offDestroyed = workspace.on("cell:destroyed", ({ id }) => this.unbindCell(id));
    this.offActive = workspace.on("cell:active", () => {
      for (const id of this.cells.keys()) this.publishCell(id);
    });
  }

  get _drag() { return this.draggingCells.size > 0; }
  get ownsData() { return true; }

  bindCell(cell) {
    if (!cell || this.cells.has(cell.id)) return;
    const targetId = `${this.id}:${cell.id}`;
    targets.set(targetId, {
      cancel: order => this.onOrderCancel?.(order),
      dragging: active => active ? this.draggingCells.add(cell.id) : this.draggingCells.delete(cell.id),
      drop: (line, drop) => line.protection ? this.onProtectionDrop?.(drop) : line.tp
        ? this.onTpDrop?.({ symbol: drop.symbol, price: drop.price, order: drop.order })
        : this.onProtectionDrop?.(drop),
    });
    this.cells.set(cell.id, { cell, targetId, handle: null, fitRisk: false });
    this.publishCell(cell.id);
  }

  unbindCell(id) {
    const state = this.cells.get(id);
    if (!state) return;
    targets.delete(state.targetId);
    this.draggingCells.delete(id);
    publish(state.targetId, { targetId: state.targetId, lines: [], scalePrices: [] });
    channels.delete(state.targetId);
    this.cells.delete(id);
  }

  destroy() {
    this.offCreated?.();
    this.offDestroyed?.();
    this.offActive?.();
    for (const id of [...this.cells.keys()]) this.unbindCell(id);
  }

  symbols() {
    return this.workspace.cells().map(cell => String(cell.chart.market.symbol || "").toUpperCase()).filter(Boolean);
  }

  setMarket(next) { return this.workspace.chart.setMarket(next); }
  applyPriceFormat() {}
  setData() {}
  updateBar() {}
  appendBar() {}
  onPrice() {}

  setOverlays(lines) {
    const symbol = String(this.workspace.chart.market.symbol || "").toUpperCase();
    this.setOverlayMap({ ...this.overlayMap, [symbol]: lines || [] });
  }

  setOverlayMap(map) {
    this.overlayMap = map || {};
    for (const id of this.cells.keys()) this.publishCell(id);
  }

  setRiskEnabled(enabled) {
    this.riskEnabled = Boolean(enabled);
    for (const state of this.cells.values()) {
      if (!this.riskEnabled) state.fitRisk = false;
      this.publishCell(state.cell.id);
    }
  }

  fitRisk() {
    const state = this.cells.get(this.workspace.getState().activeCellId);
    if (!state) return false;
    const lines = this.linesFor(state);
    const risks = riskOverlays(lines);
    if (!risks.length) return false;
    state.fitRisk = true;
    this.publishCell(state.cell.id);
    state.cell.chart.renderer.set("autoScale", true);
    return true;
  }

  linesFor(state) {
    const symbol = String(state.cell.chart.market.symbol || "").toUpperCase();
    return this.overlayMap[symbol] || [];
  }

  publishCell(id) {
    const state = this.cells.get(id);
    if (!state) return;
    const handle = state.cell.chart.addNativeIndicator(TYPE, { inputs: { targetId: state.targetId } });
    if (handle.inputValues().targetId !== state.targetId) handle.setInput("targetId", state.targetId);
    if (!handle.visible) handle.setVisible(true);
    state.handle = handle;
    const symbol = String(state.cell.chart.market.symbol || "").toUpperCase();
    if (state.symbol && state.symbol !== symbol) state.fitRisk = false;
    state.symbol = symbol;
    const lines = this.linesFor(state);
    const riskEnabled = this.riskEnabled && this.workspace.getState().activeCellId === id;
    const riskLines = riskEnabled ? riskOverlays(lines) : [];
    publish(state.targetId, {
      targetId: state.targetId,
      lines,
      riskEnabled,
      scalePrices: [
        ...lines.map(line => Number(line.price)),
        ...(state.fitRisk && riskEnabled ? riskLines.map(line => Number(line.price)) : []),
      ],
    });
  }
}
