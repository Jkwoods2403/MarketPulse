import { Badge } from "@/components/ui/badge";
import type { FreshnessStatus } from "@/lib/api";

/**
 * Data status labels (CLAUDE.md §3), as computed by the API. No value in Market Pulse is live:
 * sources publish with a delay, so the strongest status is "fresh" (the latest period is
 * within the series' own expected release interval).
 */
const LABELS: Record<FreshnessStatus, string> = {
  fresh: "Fresh",
  stale: "Stale",
  unavailable: "Unavailable",
};

const MEANINGS: Record<FreshnessStatus, string> = {
  fresh: "Latest period is within the series' expected release interval",
  stale: "Newer data should have been published by now",
  unavailable: "Nothing stored, or not served by the data API",
};

export function FreshnessBadge({
  status,
  title,
  className,
}: {
  status: FreshnessStatus;
  title?: string;
  className?: string;
}) {
  return (
    <Badge tone={status} marker="●" title={title ?? MEANINGS[status]} className={className}>
      {LABELS[status]}
    </Badge>
  );
}

/** Marks generated development fixtures wherever they appear. */
export function SyntheticTag({ className }: { className?: string }) {
  return (
    <Badge tone="synthetic" title="Generated development fixture, not market data" className={className}>
      Synthetic
    </Badge>
  );
}

/** Explains the status labels, for page headers: each label with its meaning in text. */
export function FreshnessLegend() {
  return (
    <details className="group text-[11px] leading-4">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-1 select-none">
        <span className="sr-only">Data status legend: </span>
        {(Object.keys(LABELS) as FreshnessStatus[]).map((status) => (
          <FreshnessBadge key={status} status={status} />
        ))}
        <span className="ml-1 text-faint underline decoration-dotted group-open:no-underline" aria-hidden>
          ?
        </span>
      </summary>
      <dl className="mt-1.5 grid max-w-sm grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-muted">
        {(Object.keys(LABELS) as FreshnessStatus[]).map((status) => (
          <div key={status} className="contents">
            <dt className="font-mono uppercase text-faint">{LABELS[status]}</dt>
            <dd>{MEANINGS[status]}.</dd>
          </div>
        ))}
        <dt className="font-mono uppercase text-faint">Live</dt>
        <dd>Not used: every source publishes with a delay.</dd>
      </dl>
    </details>
  );
}
