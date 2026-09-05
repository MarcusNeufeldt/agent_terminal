import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "vite";

globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
};

const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
const { default: useStore } = await vite.ssrLoadModule("/src/store.js");

test("bulk flatten blocks duplicate clicks and rejects unavailable state", async t => {
  t.after(() => vite.close());
  let confirmCalls = 0;
  let flattenCalls = 0;
  let releaseFlatten;
  let requestBody;
  const flattenResponse = new Promise(resolve => { releaseFlatten = resolve; });

  globalThis.confirm = () => { confirmCalls += 1; return true; };
  globalThis.fetch = async (path, options = {}) => {
    if (path === "/api/session") {
      return new Response(JSON.stringify({ token: "test-token" }), { status: 200 });
    }
    if (path === "/api/flatten") {
      flattenCalls += 1;
      requestBody = JSON.parse(options.body);
      await flattenResponse;
      return new Response(JSON.stringify({ simulated: true, outcome: "simulated", results: [] }), { status: 200 });
    }
    throw new Error(`unexpected request ${path}`);
  };

  const toasts = [];
  useStore.setState({
    armed: false,
    bulkBusy: false,
    positions: [{ symbol: "PF_XBTUSD", side: "long", size: 1 }],
    orders: [],
    dataStatus: { positions: { state: "current" }, orders: { state: "current" } },
    toast: message => toasts.push(message),
    refreshTables: async () => {},
    refreshAccount: async () => {},
  });

  const first = useStore.getState().flattenAll("emergency");
  const duplicate = useStore.getState().flattenAll("emergency");
  await duplicate;
  while (!flattenCalls) await new Promise(resolve => setImmediate(resolve));
  assert.equal(flattenCalls, 1);
  assert.equal(confirmCalls, 1);
  assert.equal(requestBody.mode, "emergency");
  assert.ok(requestBody.requestId);
  assert.equal(useStore.getState().bulkBusy, true);
  releaseFlatten();
  await first;
  assert.equal(useStore.getState().bulkBusy, false);
  assert.match(toasts.at(-1), /simulated/i);

  let networkCalls = 0;
  globalThis.fetch = async () => { networkCalls += 1; throw new Error("should not fetch"); };
  useStore.setState({
    bulkBusy: false,
    positions: [],
    dataStatus: { positions: { state: "unavailable" }, orders: { state: "current" } },
  });
  await useStore.getState().flattenAll("chase");
  assert.equal(networkCalls, 0);
  assert.match(toasts.at(-1), /not current/i);
});
