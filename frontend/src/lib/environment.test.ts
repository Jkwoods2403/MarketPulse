// Run with `pnpm test`.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { environmentLabel } from "./environment.ts";

describe("environmentLabel", () => {
  it("names staging so it cannot be mistaken for production", () => {
    assert.equal(environmentLabel("staging"), "Staging");
  });

  it("names the other non-production environments", () => {
    assert.equal(environmentLabel("development"), "Development");
    assert.equal(environmentLabel("test"), "Test");
  });

  it("adds no label in production", () => {
    assert.equal(environmentLabel("production"), null);
  });
});
