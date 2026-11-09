// Run with `pnpm test`.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { backendPath, parseRequest, refuseSynthetic } from "./observations-proxy.ts";

describe("parseRequest", () => {
  it("accepts catalog-style codes and known ranges", () => {
    assert.deepEqual(parseRequest("l48_gas_storage", "5Y"), {
      ok: true,
      code: "l48_gas_storage",
      range: "5Y",
    });
    assert.deepEqual(parseRequest("ust_10y", null), { ok: true, code: "ust_10y", range: null });
  });

  it("rejects anything that is not a plain series code", () => {
    for (const code of ["../health", "WTI_SPOT", "wti spot", "x", "a/b", "ust_10y?all=1", ""]) {
      assert.equal(parseRequest(code, null).ok, false, code);
    }
  });

  it("rejects unknown ranges", () => {
    assert.equal(parseRequest("ust_10y", "10Y").ok, false);
    assert.equal(parseRequest("ust_10y", "all").ok, false);
  });
});

describe("backendPath", () => {
  it("passes only start, limit, and offset to the backend", () => {
    assert.equal(
      backendPath("ust_10y", { start: "2025-10-29", limit: 5000, offset: 0 }),
      "/series/ust_10y/observations?limit=5000&offset=0&start=2025-10-29",
    );
    assert.equal(
      backendPath("ust_10y", { limit: 1, offset: 0 }),
      "/series/ust_10y/observations?limit=1&offset=0",
    );
  });
});

describe("refuseSynthetic", () => {
  it("refuses synthetic fixtures only in production builds", () => {
    assert.equal(refuseSynthetic("synthetic", "production"), true);
    assert.equal(refuseSynthetic("synthetic", "development"), false);
    assert.equal(refuseSynthetic("database", "production"), false);
  });
});
