import test from "node:test";
import assert from "node:assert/strict";
import { newRequestId } from "./api.js";

test("intentional requests receive unique replay keys", () => {
  const ids = new Set(Array.from({ length: 100 }, newRequestId));
  assert.equal(ids.size, 100);
  for (const id of ids) assert.match(id, /^[A-Za-z0-9._:-]{8,100}$/);
});
