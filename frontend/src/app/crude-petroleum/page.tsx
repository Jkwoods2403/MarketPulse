import type { Metadata } from "next";

import { TimeSeriesChart } from "@/components/charts/time-series-chart";
import { CalculatedMetricsPanel } from "@/components/dashboard/calculated-metrics-panel";
import { Section } from "@/components/dashboard/section";
import { SeriesCards } from "@/components/dashboard/series-cards";
import { UnavailablePricePanel } from "@/components/dashboard/unavailable-panel";
import { FreshnessLegend } from "@/components/data/freshness-badge";
import { SourceAttribution } from "@/components/data/source-attribution";
import { PageHeader } from "@/components/shell/page-header";
import { CRUDE_PETROLEUM } from "@/lib/dashboard";

export const metadata: Metadata = { title: "Crude & Petroleum" };

export default function CrudePetroleumPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Crude & Petroleum"
        description="Weekly U.S. inventories, production, and refinery utilization from EIA (week ending Friday). Stocks are levels in thousand barrels; production is a rate in thousand barrels per day."
        aside={<FreshnessLegend />}
      />
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <Section title="Crude oil prices" meta="Spot benchmarks" className="xl:col-span-3">
          <UnavailablePricePanel filter={(label) => label.includes("crude oil")} />
        </Section>
        <Section
          title="Inventories, production & refining"
          meta="EIA · weekly"
          className="xl:col-span-3"
          footer={<SourceAttribution metrics={CRUDE_PETROLEUM} />}
        >
          <div className="space-y-2">
            <SeriesCards metrics={CRUDE_PETROLEUM} />
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-2 2xl:grid-cols-3">
              {CRUDE_PETROLEUM.map((m) => (
                <TimeSeriesChart key={m.code} code={m.code} title={m.label} note={m.note} />
              ))}
            </div>
          </div>
        </Section>
        <Section
          title="Calculated metrics"
          meta="Derived · current versions"
          className="xl:col-span-3"
        >
          <CalculatedMetricsPanel metrics={CRUDE_PETROLEUM} />
        </Section>
      </div>
    </div>
  );
}
