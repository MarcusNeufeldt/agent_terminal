// One unresolved submission blocks further ticket writes until a fresh identity lookup succeeds.
export function receiptKey(network, account) {
  if (!["mainnet", "testnet"].includes(network) || !/^0x[0-9a-fA-F]{40}$/.test(account || "")) {
    throw new Error("Hyperliquid account identity unavailable");
  }
  return `kt.hyperliquid.receipt.${network}.${account.toLowerCase()}`;
}

export function hasRecoveryIdentity(value) {
  const valid = id => typeof id === "string" && /^0x[0-9a-fA-F]{32}$/.test(id);
  if (value?.kind === "leverage") {
    return typeof value.body?.symbol === "string" && value.body.symbol.startsWith("HL_") &&
      Number.isInteger(value.body.leverage) && value.body.leverage >= 1 && typeof value.body.cross === "boolean";
  }
  if (value?.batch === true) {
    return Array.isArray(value.cloids) && value.cloids.length >= 2 && value.cloids.length <= 20 &&
      value.cloids.every(valid) && new Set(value.cloids.map(id => id.toLowerCase())).size === value.cloids.length;
  }
  return (value?.batch === undefined || value?.batch === false) && valid(value?.cloid);
}

export function readReceipt(key) {
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  const value = JSON.parse(raw);
  if (value?.version !== 1 || typeof value.requestId !== "string" ||
      !hasRecoveryIdentity(value) || !value.body ||
      typeof value.outcome !== "string") throw new Error("Invalid saved Hyperliquid receipt");
  return value;
}

export function writeReceipt(key, receipt) {
  if (!key) throw new Error("Hyperliquid recovery storage is not ready");
  localStorage.setItem(key, JSON.stringify(receipt));
}

export function unresolvedReceipt(receipt) {
  return receipt && !["confirmed", "rejected", "simulated", "reconciled"].includes(receipt.outcome);
}

export function newCloid() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return "0x" + Array.from(bytes, n => n.toString(16).padStart(2, "0")).join("");
}
