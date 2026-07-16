import { expect, test } from "vitest";
import { add } from "./calc";

test("add sums", () => {
  expect(add(2, 2)).toBe(5);
});
