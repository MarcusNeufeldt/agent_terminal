import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { selectAltBtcRows } from "./alt-btc.js";

globalThis.localStorage = { getItem: () => null, setItem: () => {} };

test("Alt/BTC modal ranks and filters read-only comparisons", async t => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: "custom" });
  t.after(() => vite.close());
  const { default: Modal } = await vite.ssrLoadModule("/src/components/AltBtcModal.jsx");
  const rows = [
    { symbol: "PF_SOLUSD", asset: "SOL", binanceSymbol: "SOLUSDT", changeVsBtcPct: 4 },
    { symbol: "PF_ETHUSD", asset: "ETH", binanceSymbol: "ETHUSDT", changeVsBtcPct: -2 },
    { symbol: "PF_UNIUSD", asset: "UNI", binanceSymbol: "UNIUSDT", changeVsBtcPct: 0 },
  ];
  assert.deepEqual(selectAltBtcRows(rows, "", "all", "best").map(r => r.asset), ["SOL", "UNI", "ETH"]);
  assert.deepEqual(selectAltBtcRows(rows, "", "all", "worst").map(r => r.asset), ["ETH", "UNI", "SOL"]);
  assert.deepEqual(selectAltBtcRows(rows, "", "up", "best").map(r => r.asset), ["SOL"]);
  assert.deepEqual(selectAltBtcRows(rows, "", "down", "best").map(r => r.asset), ["ETH"]);
  assert.deepEqual(selectAltBtcRows(rows, " pf_eth ", "all", "best").map(r => r.asset), ["ETH"]);
  assert.deepEqual(rows.map(r => r.asset), ["SOL", "ETH", "UNI"], "filtering must not reorder the cached snapshot");
  const html = renderToStaticMarkup(createElement(Modal, { onClose() {} }));
  for (const text of ["<dialog", "Altcoins vs Bitcoin", "Search altcoins", "Best first", "Worst first",
    "Loading Binance comparison", "not a BTC trading pair", "Close Alt/BTC modal",
    "Comparison time frame", "1H</button>", "6H</button>", "24H</button>"]) assert.ok(html.includes(text), text);
  assert.match(html, /aria-pressed="true">24H<\/button>/);
  assert.equal((html.match(/aria-pressed="false"/g) || []).length, 2);
});
