import { Suspense } from "react";

import { getSeriesList } from "@/lib/api";
import type { MetricDefinition } from "@/lib/dashboard";

/**
 * The attribution each source requires, read from the API (ref.sources.attribution_text), plus
 * the original producers of the section's series. Shown under the data it applies to.
 */
export function SourceAttribution({ metrics }: { metrics: readonly MetricDefinition[] }) {
  return (
    <Suspense fallback={<p className="text-[11px] leading-4 text-faint">Loading sources…</p>}>
      <LoadedAttribution metrics={metrics} />
    </Suspense>
  );
}

async function LoadedAttribution({ metrics }: { metrics: readonly MetricDefinition[] }) {
  const result = await getSeriesList();
  if (result.state !== "ok") {
    return <p className="text-[11px] leading-4 text-faint">Source attribution unavailable.</p>;
  }
  const codes = new Set(metrics.map((m) => m.code));
  const series = result.data.series.filter((s) => codes.has(s.code));
  const sources = new Map<string, { attribution: string; producers: Set<string> }>();
  for (const s of series) {
    const entry = sources.get(s.source.code) ?? {
      attribution: s.source.attribution ?? `Source: ${s.source.name}`,
      producers: new Set<string>(),
    };
    if (s.original_source && s.original_source !== s.source.name) {
      entry.producers.add(s.original_source);
    }
    sources.set(s.source.code, entry);
  }
  if (sources.size === 0) return null;
  return (
    <ul className="space-y-0.5 text-[11px] leading-4 text-faint">
      {[...sources.entries()].map(([code, { attribution, producers }]) => (
        <li key={code}>
          {attribution}
          {producers.size > 0 ? `. Original producers: ${[...producers].sort().join("; ")}` : ""}.
        </li>
      ))}
    </ul>
  );
}
