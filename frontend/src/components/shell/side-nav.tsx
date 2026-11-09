"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cn";
import { STATUS_LABELS, groupedNavItems, isActivePath } from "@/lib/navigation";

/**
 * Module navigation: a vertical rail on large screens, a horizontally scrolling strip below
 * that. Modules are grouped; the current page is marked (aria-current and an accent bar), and
 * modules that are not operational carry a text badge ("Planned", "On hold") at every width,
 * so they are never presented as working features. All routes are static, so usePathname
 * needs no Suspense boundary.
 */
export function SideNav() {
  const pathname = usePathname();
  return (
    <nav
      aria-label="Modules"
      className="shrink-0 border-b border-border bg-panel lg:w-56 lg:border-b-0 lg:border-r"
    >
      {/* relative: contains the badges' screen-reader text, so it cannot widen the page. */}
      <div className="relative flex overflow-x-auto lg:flex-col lg:overflow-visible lg:py-2">
        {groupedNavItems().map(({ group, label, items }) => (
          <div
            key={group}
            className="flex shrink-0 items-stretch border-r border-border last:border-r-0 lg:flex-col lg:border-r-0 lg:pb-2"
          >
            <h2
              id={`nav-${group}`}
              className="hidden px-4 pt-2 pb-1 font-mono text-[10px] uppercase tracking-wider text-faint lg:block"
            >
              {label}
            </h2>
            <ul aria-labelledby={`nav-${group}`} className="flex lg:flex-col">
              {items.map((item) => {
                const current = isActivePath(pathname, item.href);
                const status = STATUS_LABELS[item.status];
                return (
                  <li key={item.href} className="shrink-0">
                    <Link
                      href={item.href}
                      aria-current={current ? "page" : undefined}
                      className={cn(
                        "flex h-9 items-center justify-between gap-2 whitespace-nowrap border-b-2 px-3 text-xs transition-colors lg:h-8 lg:border-b-0 lg:border-l-2 lg:px-4",
                        current
                          ? "border-accent bg-panel-raised font-medium text-fg"
                          : "border-transparent hover:bg-panel-raised hover:text-fg",
                        !current && (item.status === "active" ? "text-muted" : "text-faint"),
                      )}
                    >
                      <span>
                        <span className="lg:hidden">{item.short}</span>
                        <span className="hidden lg:inline">{item.label}</span>
                      </span>
                      {status.badge ? (
                        <Badge
                          tone={item.status === "blocked" ? "hold" : "planned"}
                          className="px-1 text-[9px]"
                        >
                          <span className="sr-only">(</span>
                          {status.badge}
                          <span className="sr-only">)</span>
                        </Badge>
                      ) : null}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}
