import test from "node:test";
import assert from "node:assert/strict";
import config from "../vite.config.js";

test("dev proxy presents the backend's trusted host and origin", () => {
  const proxy = config.server.proxy["/api"];
  assert.equal(proxy.target, "http://127.0.0.1:8787");
  assert.equal(proxy.changeOrigin, true);
  assert.equal(proxy.headers.Origin, proxy.target);
});
