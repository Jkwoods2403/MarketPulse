import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { NAV_ITEMS, STATUS_LABELS, groupedNavItems, isActivePath, navItem } from "./navigation.ts";

describe("navigation", () => {
  it("lists every module once, in two groups", () => {
    const hrefs = groupedNavItems().flatMap((g) => g.items.map((i) => i.href));
    assert.deepEqual(hrefs, NAV_ITEMS.map((i) => i.href));
    assert.equal(new Set(hrefs).size, 9);
  });

  it("marks only modules with data as operational", () => {
    const active = NAV_ITEMS.filter((i) => i.status === "active").map((i) => i.href);
    assert.deepEqual(active, ["/", "/natural-gas", "/crude-petroleum"]);
    assert.equal(navItem("/electricity")?.status, "blocked");
    // Macro: FRED is retired (ADR-0015) and no direct macro source is integrated yet.
    for (const href of ["/macro", "/analytics", "/news", "/watchlist", "/journal"]) {
      assert.equal(navItem(href)?.status, "planned");
    }
  });

  it("gives every non-operational status a visible text label", () => {
    assert.equal(STATUS_LABELS.active.badge, null);
    assert.equal(STATUS_LABELS.planned.badge, "Planned");
    assert.equal(STATUS_LABELS.blocked.badge, "On hold");
    assert.match(STATUS_LABELS.planned.description, /not operational/);
  });

  it("matches the active route exactly, with nested paths", () => {
    assert.equal(isActivePath("/", "/"), true);
    assert.equal(isActivePath("/macro", "/"), false);
    assert.equal(isActivePath("/macro", "/macro"), true);
    assert.equal(isActivePath("/macro/rates", "/macro"), true);
    assert.equal(isActivePath("/macroeconomics", "/macro"), false);
  });
});
