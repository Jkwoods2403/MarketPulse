import type { Metadata } from "next";

import { TimeSeriesChart } from "@/components/charts/time-series-chart";
import { CalculatedMetricsPanel } from "@/components/dashboard/calculated-metrics-panel";
import { Section } from "@/components/dashboard/section";
import { SeriesCards } from "@/components/dashboard/series-cards";
import { FreshnessLegend } from "@/components/data/freshness-badge";
import { SourceAttribution } from "@/components/data/source-attribution";
import { PageHeader } from "@/components/shell/page-header";
import { ECONOMY, RATES } from "@/lib/dashboard";

export const metadata: Metadata = { title: "Macroeconomics" };

export default function MacroPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Macroeconomics"
        description="Rates, the dollar, and economic indicators from FRED, with full revision history. The 10-year minus 2-year spread is FRED's published series, not a Market Pulse calculation."
        aside={<FreshnessLegend />}
      />
      <div className="grid grid-cols-1 gap-3">
        <Section
          title="Rates & dollar"
          meta="FRED · daily"
          footer={<SourceAttribution metrics={RATES} />}
        >
          <div className="space-y-2">
            <SeriesCards metrics={RATES} />
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-2 2xl:grid-cols-3">
              {RATES.map((m) => (
                <TimeSeriesChart key={m.code} code={m.code} title={m.label} note={m.note} />
              ))}
            </div>
          </div>
        </Section>
        <Section
          title="Economy"
          meta="FRED · monthly & quarterly"
          footer={<SourceAttribution metrics={ECONOMY} />}
        >
          <div className="space-y-2">
            <SeriesCards metrics={ECONOMY} />
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-2 2xl:grid-cols-3">
              {ECONOMY.map((m) => (
                <TimeSeriesChart key={m.code} code={m.code} title={m.label} note={m.note} />
              ))}
            </div>
          </div>
        </Section>
        <Section title="Calculated metrics" meta="Derived · current versions">
          <CalculatedMetricsPanel metrics={[...RATES, ...ECONOMY]} />
        </Section>
      </div>
    </div>
  );
}
