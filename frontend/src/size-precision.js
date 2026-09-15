function boundedPrecision(precision) {
  const value = Math.trunc(Number(precision) || 0);
  return Math.max(-8, Math.min(8, value));
}

export function normalizeContractSize(value, precision) {
  const number = Number(value);
  if (!Number.isFinite(number)) return NaN;
  const step = 10 ** -boundedPrecision(precision);
  const units = Math.trunc(number / step + Math.sign(number) * 1e-10);
  return units * step;
}

// Exact decimal division keeps USD sizing from rounding up across a lot boundary.
function positiveFraction(value) {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value ?? "").trim();
  if (text.length > 80) return null;
  const match = text.match(/^(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i);
  if (!match || !(match[1] || match[2])) return null;
  const exponent = Number(match[3] || 0) - (match[2] || "").length;
  if (Math.abs(exponent) > 80) return null;
  const units = BigInt((match[1] || "0") + (match[2] || ""));
  if (units <= 0n) return null;
  return exponent >= 0 ? [units * 10n ** BigInt(exponent), 1n] : [units, 10n ** BigInt(-exponent)];
}

export function compareContractSizes(left, right) {
  const a = positiveFraction(left), b = positiveFraction(right);
  if (!a || !b) return null;
  const delta = a[0] * b[1] - b[0] * a[1];
  return delta < 0n ? -1 : delta > 0n ? 1 : 0;
}

export function contractsForNotional(notional, unitValue, precision, percent = 100) {
  const budget = positiveFraction(notional), price = positiveFraction(unitValue);
  if (!budget || !price || !Number.isInteger(precision) || precision < -8 || precision > 8 ||
      !Number.isInteger(percent) || percent < 1 || percent > 100) return "";
  const factor = 10n ** BigInt(Math.abs(precision));
  const numerator = budget[0] * price[1] * BigInt(percent), denominator = budget[1] * price[0] * 100n;
  const lots = precision >= 0 ? numerator * factor / denominator : numerator / (denominator * factor);
  if (lots > BigInt(Number.MAX_SAFE_INTEGER)) return "";
  if (precision <= 0) return String(lots * factor);
  const digits = String(lots).padStart(precision + 1, "0");
  return `${digits.slice(0, -precision)}.${digits.slice(-precision)}`;
}

export function formatContractSize(value, precision) {
  const normalized = normalizeContractSize(value, precision);
  if (!Number.isFinite(normalized)) return "";
  const digits = Math.max(0, boundedPrecision(precision));
  return normalized.toFixed(digits);
}
