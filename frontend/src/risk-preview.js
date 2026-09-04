export function riskLineOptions(price) {
  return { price: Number(price), color: "#ef5350", lineWidth: 2, axisLabelVisible: true };
}

export function mirroredRisk({ entry, target, dir, ratio, tick, size, mult = 1 }) {
  const distance = Math.abs(target - entry) * ratio;
  const price = Math.max(tick, Math.round((entry - dir * distance) / tick) * tick);
  return { price, pnl: dir * (price - entry) * size * mult };
}
