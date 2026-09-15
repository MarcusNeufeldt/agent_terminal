export function linearExitPreview({ position, quantity, exitPrice }) {
  if (!position || position.error || !["long", "short"].includes(position.side)) return null;
  const values = [position.price, position.size, quantity, exitPrice];
  if (!values.every(value => typeof value === "number" || typeof value === "string")) return null;
  const [entry, exposure, size, price] = values.map(Number);
  if (![entry, exposure, size, price].every(value => Number.isFinite(value) && value > 0)) return null;
  const coveredSize = Math.min(size, exposure);
  const pnl = (position.side === "long" ? 1 : -1) * (price - entry) * coveredSize;
  if (!Number.isFinite(pnl)) return null;
  return { pnl, coveredSize, coveragePct: coveredSize / exposure * 100,
    oversized: size > exposure, side: position.side === "long" ? "sell" : "buy" };
}

export function riskLineOptions(price) {
  return { price: Number(price), color: "#ef5350", lineWidth: 2, axisLabelVisible: true };
}

export function mirroredRisk({ entry, target, dir, ratio, tick, size, mult = 1 }) {
  const distance = Math.abs(target - entry) * ratio;
  const price = Math.max(tick, Math.round((entry - dir * distance) / tick) * tick);
  return { price, pnl: dir * (price - entry) * size * mult };
}
