/** Terminal modules shown in the navigation, in display order. Pure (unit-tested). */

// active: has a dashboard page with data; planned: a later milestone, no functionality yet;
// blocked: the module exists in the plan but its data is not permitted yet (ERCOT, ADR-0014).
export type NavStatus = "active" | "planned" | "blocked";
export type NavGroup = "markets" | "research";

export type NavItem = {
  href: string;
  label: string;
  short: string; // compact label for narrow screens
  status: NavStatus;
  group: NavGroup;
};

export const NAV_GROUPS: Record<NavGroup, string> = {
  markets: "Markets",
  research: "Research & tools",
};

export const NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "Market Overview", short: "Overview", status: "active", group: "markets" },
  { href: "/natural-gas", label: "Natural Gas", short: "Gas", status: "active", group: "markets" },
  {
    href: "/crude-petroleum",
    label: "Crude & Petroleum",
    short: "Crude",
    status: "active",
    group: "markets",
  },
  { href: "/electricity", label: "Electricity", short: "Power", status: "blocked", group: "markets" },
  { href: "/macro", label: "Macroeconomics", short: "Macro", status: "planned", group: "markets" },
  { href: "/analytics", label: "Analytics", short: "Analytics", status: "planned", group: "research" },
  { href: "/news", label: "News", short: "News", status: "planned", group: "research" },
  { href: "/watchlist", label: "Watchlist", short: "Watchlist", status: "planned", group: "research" },
  { href: "/journal", label: "Journal", short: "Journal", status: "planned", group: "research" },
];

/** How each status is labeled wherever a module is named (navigation, page headers). */
export const STATUS_LABELS: Record<NavStatus, { badge: string | null; description: string }> = {
  active: { badge: null, description: "Operational" },
  planned: { badge: "Planned", description: "Planned for a later milestone; not operational" },
  blocked: { badge: "On hold", description: "On hold: data not yet permitted (licensing pending)" },
};

export function isActivePath(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function navItem(href: string): NavItem | undefined {
  return NAV_ITEMS.find((item) => item.href === href);
}

export function groupedNavItems(): Array<{ group: NavGroup; label: string; items: NavItem[] }> {
  return (Object.keys(NAV_GROUPS) as NavGroup[]).map((group) => ({
    group,
    label: NAV_GROUPS[group],
    items: NAV_ITEMS.filter((item) => item.group === group),
  }));
}
