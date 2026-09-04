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

export function formatContractSize(value, precision) {
  const normalized = normalizeContractSize(value, precision);
  if (!Number.isFinite(normalized)) return "";
  const digits = Math.max(0, boundedPrecision(precision));
  return normalized.toFixed(digits);
}
