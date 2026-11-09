import type { SeriesMetrics } from "@/lib/api";
import { formatPeriod } from "@/lib/format";
import { direction, formatChange, headlineMetrics, provenanceLabel, shortLabel } from "@/lib/metric-format";

/** What a card knows about calculated metrics: loaded, or the reason they are not shown. */
export type CalculatedState = { state: "ok"; series: SeriesMetrics | undefined } | { state: "unavailable" };

const ARROWS = { up: "▲", down: "▼", flat: "■" } as const;

/**
 * The compact "Calculated" block on a metric card: the period change and year over year,
 * each with the period it is compared against. The methodology is in the row's tooltip; the
 * full detail is in the Calculated metrics panel of each module page.
 */
export function CalculatedSummary({ calculated }: { calculated: CalculatedState }) {
  if (calculated.state === "unavailable") {
    return (
      <Frame label="Calculated">
        <p className="text-faint">Calculated metrics could not be loaded; none are shown.</p>
      </Frame>
    );
  }
  const series = calculated.series;
  if (series === undefined || series.current === null) return null;
  const metrics = headlineMetrics(series);
  if (metrics.length === 0) return null;
  return (
    <Frame label={provenanceLabel(series.synthetic)}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
        {metrics.map((m) => {
          const ok = m.status === "ok" && m.absolute_change !== null;
          const dir = ok && m.absolute_change !== null ? direction(m.absolute_change) : null;
          const versus = m.comparison
            ? `vs ${formatPeriod(series.period_convention, m.comparison.period_start, m.comparison.period_end)}`
            : (m.reason ?? "");
          return (
            <div key={m.kind} className="contents">
              <dt className="font-mono text-faint">{shortLabel(m)}</dt>
              <dd className="min-w-0" title={`${m.label}. ${m.methodology}${m.reason ? ` ${m.reason}.` : ""}`}>
                <span className={ok ? "font-mono text-fg" : "text-faint"}>
                  {dir ? <span aria-hidden className="mr-1 text-[9px] text-muted">{ARROWS[dir]}</span> : null}
                  {formatChange(m)}
                </span>
                <span className="text-faint"> · {versus}</span>
              </dd>
            </div>
          );
        })}
      </dl>
    </Frame>
  );
}

function Frame({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5 border-t border-dashed border-border pt-1.5 text-[11px] leading-4">
      <p className="font-mono text-[9px] uppercase tracking-wider text-accent">{label}</p>
      {children}
    </div>
  );
}
