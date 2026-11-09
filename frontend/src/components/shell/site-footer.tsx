import Link from "next/link";

/** Data policy reminder and the third-party notices link, on every page. */
export function SiteFooter() {
  return (
    <footer className="flex flex-col gap-1 border-t border-border px-3 py-2 text-[11px] leading-4 text-faint md:flex-row md:items-center md:justify-between md:px-4">
      <p>
        Delayed and historical data from public sources; each panel names its source and
        period. Not investment advice.
      </p>
      <nav aria-label="Legal">
        <Link href="/notices" className="text-muted underline decoration-dotted hover:text-fg">
          Third-party notices
        </Link>
      </nav>
    </footer>
  );
}
