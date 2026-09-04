export function buildChartOverlays(symbol, positions, orders, instruments) {
  const overlays = [];
  const position = (positions || []).find(p => !p.error && p.symbol === symbol && p.size);
  const instrument = (instruments || []).find(i => i.symbol === symbol) || {};
  const tick = Number(instrument.tickSize) || 0.01;
  const mult = Number(instrument.contractSize || 1);

  for (const p of positions || []) {
    if (p.error || p.symbol !== symbol || !p.size) continue;
    const dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
    overlays.push({
      key: `position:${symbol}`,
      price: Number(p.price),
      color: p.side === "long" ? "#26a69a" : "#ef5350",
      title: `${p.side} ${Number(p.size)}`,
      dashed: false,
      position: { symbol, entry: Number(p.price), size: Number(p.size), mult, dir, tick, side: p.side },
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
    } : null;

    if (o.limitPrice) {
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

    if (!o.stopPrice) continue;
    const isTp = String(o.orderType).toLowerCase() === "take_profit";
    const type = isTp ? "TP" : "SL";
    if (!position) {
      overlays.push({
        key: `order-stop:${identity}`,
        price: Number(o.stopPrice),
        color: isTp ? "#26a69a" : "#ef5350",
        title: `${type} ${Number(o.stopPrice)}`,
        dashed: true,
        ...(order ? { order } : {}),
      });
      continue;
    }

    const dir = String(position.side).toLowerCase() === "short" ? -1 : 1;
    const positionSize = Number(position.size);
    const orderSize = Number(o.unfilledSize ?? o.size ?? positionSize);
    const coveredSize = Math.min(positionSize, orderSize);
    const coverage = positionSize > 0 ? Math.round(orderSize / positionSize * 100) : 0;
    const pnl = dir * (Number(o.stopPrice) - Number(position.price)) * coveredSize * mult;
    overlays.push({
      key: `order-stop:${identity}`,
      price: Number(o.stopPrice),
      color: isTp ? "#26a69a" : "#ef5350",
      title: `${type} ${Number(o.stopPrice)} (${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}) · ${coverage}%`,
      dashed: true,
      ...(isTp ? {
        tp: {
          symbol,
          entry: Number(position.price),
          size: coveredSize,
          mult,
          dir,
          tick,
          fullPosition: orderSize === positionSize,
          order,
        },
      } : {}),
      ...(order ? { order } : {}),
    });
  }

  return overlays.filter(line => Number.isFinite(line.price));
}
