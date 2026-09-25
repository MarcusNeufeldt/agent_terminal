import { exitAfterFee, feeRateFor, makerFeeRateFor } from "./close-preview.js";
import { isChaseOrder } from "./chase-view.js";

export function buildChartOverlays(symbol, positions, orders, instruments, readOnly = false, canCancel = false, canProtect = false) {
  const overlays = [];
  const position = (positions || []).find(p => !p.error && p.symbol === symbol && p.size);
  const protectionReady = canProtect && (positions || []).filter(p => p.symbol === symbol).length === 1 && Number(position?.sizeExact) > 0;
  const instrument = (instruments || []).find(i => i.symbol === symbol) || {};
  const tick = Number(instrument.tickSize) || 0.01;
  const mult = Number(instrument.contractSize || 1);
  // A stop fills as a market order (taker fee); a TP is a resting post-only limit (maker fee).
  const feeRate = feeRateFor(readOnly);
  const makerFeeRate = makerFeeRateFor(readOnly);
  const exitSide = position ? (String(position.side).toLowerCase() === "long" ? "sell" : "buy") : null;

  for (const p of positions || []) {
    if (p.error || p.symbol !== symbol || !p.size) continue;
    const dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
    overlays.push({
      key: `position:${symbol}`,
      price: Number(p.price),
      color: p.side === "long" ? "#26a69a" : "#ef5350",
      title: `${p.side} ${Number(p.size)}`,
      dashed: false,
      position: { symbol, entry: Number(p.price), size: Number(p.size), mult, dir, tick, feeRate, makerFeeRate, side: p.side, snapshot: { ...p } },
    });
    if (p.liqPriceEstimate) {
      overlays.push({
        key: `liquidation:${symbol}`,
        price: Number(p.liqPriceEstimate),
        color: "#f0b90b",
        title: `LIQ ${Number(p.liqPriceEstimate)}`,
        dashed: true,
      });
    }
  }

  for (const [index, o] of (orders || []).entries()) {
    if (o.error || o.symbol !== symbol) continue;
    const orderId = o.order_id || o.orderId || null;
    const identity = o.cliOrdId || orderId || `${o.orderType}:${o.side}:${o.stopPrice || o.limitPrice}:${o.size}:${index}`;
    const order = (o.cliOrdId || orderId) ? {
      symbol: o.symbol,
      side: o.side,
      orderType: o.orderType,
      cliOrdId: o.cliOrdId || null,
      orderId,
      price: Number(o.stopPrice || o.limitPrice),
      snapshot: { ...o },
      positionSnapshot: position ? { ...position } : null,
    } : null;

    // A maker TP: a resting reduce-only limit on the exit side of the open position.
    // A Chase exit is also a reduce-only limit, but it is an exit in flight, not a TP.
    const limitTp = !!position && !o.stopPrice && Number(o.limitPrice) > 0 && String(o.reduceOnly) === "true" &&
      o.side === exitSide && !isChaseOrder(o);
    if (o.limitPrice && !limitTp) {
      const size = o.size !== null && o.size !== undefined ? ` ${Number(o.size)}` : "";
      overlays.push({
        key: `order-limit:${identity}`,
        price: Number(o.limitPrice),
        color: "#4f8cff",
        title: `${o.side} ${o.orderType}${size}`,
        dashed: true,
        ...(!o.stopPrice && order ? { order } : {}),
      });
    }

    if (!o.stopPrice && !limitTp) continue;
    const isTp = limitTp || String(o.orderType).toLowerCase() === "take_profit";
    const type = isTp ? "TP" : "SL";
    const exitPrice = Number(limitTp ? o.limitPrice : o.stopPrice);
    const lineFee = limitTp ? makerFeeRate : feeRate;
    if (!position) {
      overlays.push({
        key: `order-stop:${identity}`,
        price: exitPrice,
        color: isTp ? "#26a69a" : "#ef5350",
        title: `${type} ${exitPrice}`,
        dashed: true,
        ...(order ? { order } : {}),
      });
      continue;
    }

    const dir = String(position.side).toLowerCase() === "short" ? -1 : 1;
    const positionSize = Number(position.size);
    const nativeFullPosition = o.positionTpsl === true && o.reduceOnly === true &&
      o.side === (position.side === "long" ? "sell" : "buy");
    const orderSize = nativeFullPosition ? positionSize : Number(o.unfilledSize ?? o.size ?? positionSize);
    const coveredSize = Math.min(positionSize, orderSize);
    const coverage = positionSize > 0 ? Math.round(orderSize / positionSize * 100) : 0;
    const { net: pnl } = exitAfterFee({ dir, entry: Number(position.price), price: exitPrice, size: coveredSize, mult, feeRate: lineFee }) || { net: NaN };
    overlays.push({
      key: `order-stop:${identity}`,
      price: exitPrice,
      color: isTp ? "#26a69a" : "#ef5350",
      title: `${type} ${exitPrice} (${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} after ${limitTp ? "maker " : ""}fee) · ${coverage}%${nativeFullPosition ? " · auto size" : limitTp ? " · maker" : ""}`,
      dashed: true,
      ...(isTp ? {
        tp: {
          symbol,
          entry: Number(position.price),
          size: coveredSize,
          mult,
          dir,
          tick,
          feeRate: lineFee,
          stopFeeRate: feeRate,
          maker: limitTp,
          fullPosition: orderSize === positionSize,
          order,
        },
      } : {}),
      // A stop gets the same drag affordance the take profit already has, routed to
      // replace_sl. The server validates a stop against the current mark rather than
      // entry, so dragging one above entry to lock in profit is legitimate. Requires
      // an exact order so the edit targets that stop instead of creating a new one.
      // Hyperliquid sets its own protection further down, under stricter checks.
      ...(!isTp && !readOnly && order ? {
        protection: {
          symbol,
          kind: "sl",
          entry: Number(position.price),
          size: coveredSize,
          mult,
          dir,
          tick,
          feeRate,
          order,
          snapshot: { ...position },
        },
      } : {}),
      ...(order ? { order } : {}),
    });
  }

  return overlays.filter(line => Number.isFinite(line.price)).map(line => {
    if (readOnly) {
      if (!protectionReady) delete line.position;
      const order = line.order?.snapshot;
      // The maker TP (a resting reduce-only Alo limit) was recognised above; triggers are
      // the stop loss and any older market TP.
      const makerTp = line.tp?.maker === true;
      const protectionSize = order?.positionTpsl === true ? Number(position?.sizeExact) : Number(order?.unfilledSizeExact);
      if (protectionReady && position && protectionSize > 0 &&
          protectionSize <= Number(position.sizeExact) && line.key.startsWith("order-stop:") && order?.reduceOnly === true &&
          order.side === (position.side === "long" ? "sell" : "buy") &&
          (makerTp || (["tp", "sl"].includes(order.triggerKind) && typeof order.triggerMarket === "boolean"))) {
        line.protection = { symbol, kind: makerTp ? "tp" : order.triggerKind, entry: Number(position.price),
          size: protectionSize, mult, dir: position.side === "long" ? 1 : -1,
          tick, feeRate: makerTp ? makerFeeRate : feeRate, stopFeeRate: feeRate, maker: makerTp,
          order: line.order, snapshot: { ...position } };
      }
      if (!canCancel) delete line.order;
      if (line.protection?.kind === "tp") {
        line.tp = { ...line.protection, fullPosition: protectionSize === Number(position.sizeExact) };
      } else delete line.tp;
    }
    return line;
  });
}
