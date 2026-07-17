import test from "node:test";
import assert from "node:assert/strict";
import { add } from "./lib.js";

test("add sums", () => {
  assert.equal(add(2, 2), 4);
});
