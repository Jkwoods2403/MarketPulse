import Link from "next/link";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** "Rates & dollar" -> "section-rates-dollar" */
function sectionId(title: string): string {
  return `section-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`;
}

/**
 * A titled dashboard panel: a labelled region whose h2 is its title. `meta` sits at the right of
 * the title bar (cadence, source). `id` is needed only if two sections on a page share a title.
 */
export function Section({
  title,
  id,
  meta,
  href,
  footer,
  children,
  className,
}: {
  title: string;
  id?: string;
  meta?: ReactNode;
  href?: string; // link to the module page for this section
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const headingId = id ?? sectionId(title);
  return (
    <section
      aria-labelledby={headingId}
      className={cn("flex min-w-0 flex-col border border-border bg-canvas", className)}
    >
      <header className="flex h-8 items-center justify-between gap-3 border-b border-border bg-panel px-3">
        <h2 id={headingId} className="truncate text-[11px] font-semibold uppercase tracking-wider text-muted">
          {title}
        </h2>
        <div className="flex shrink-0 items-center gap-3 font-mono text-[10px] uppercase tracking-wider text-faint">
          {meta}
          {href ? (
            <Link href={href} className="text-accent hover:underline">
              Open<span className="sr-only"> {title}</span> <span aria-hidden>›</span>
            </Link>
          ) : null}
        </div>
      </header>
      <div className="flex-1 p-3">{children}</div>
      {footer ? <footer className="border-t border-border px-3 py-2">{footer}</footer> : null}
    </section>
  );
}
