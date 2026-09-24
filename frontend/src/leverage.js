/* Per-pair leverage limits for both venues. */

const PRESETS = [1, 2, 3, 5, 10, 20, 25, 40, 50];

// Buttons up to the pair's maximum, always ending on the maximum itself.
export function leverageChoices(max, presets = PRESETS) {
  const limit = Math.floor(Number(max));
  if (!Number.isFinite(limit) || limit < 1) return presets.slice(0, 5);
  const choices = presets.filter(v => v <= limit);
  if (!choices.includes(limit)) choices.push(limit);
  return choices;
}

// Hyperliquid publishes maxLeverage; Kraken only publishes margin tiers, so the
// first tier's initial margin gives the maximum for a small position.
export function pairMaxLeverage(instrument) {
  const inst = instrument || {};
  if (Number.isFinite(inst.maxLeverage) && inst.maxLeverage >= 1) return Math.floor(inst.maxLeverage);
  const levels = inst.retailMarginLevels || inst.marginLevels;
  const initial = Array.isArray(levels) && levels.length ? Number(levels[0].initialMargin) : NaN;
  return initial > 0 && initial <= 1 ? Math.floor(1 / initial + 1e-9) : null;
}
