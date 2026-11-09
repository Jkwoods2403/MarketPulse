import type { Metadata } from "next";

import { TimeSeriesChart } from "@/components/charts/time-series-chart";
import { CalculatedMetricsPanel } from "@/components/dashboard/calculated-metrics-panel";
import { Section } from "@/components/dashboard/section";
import { SeriesCards } from "@/components/dashboard/series-cards";
import { UnavailablePricePanel } from "@/components/dashboard/unavailable-panel";
import { FreshnessLegend } from "@/components/data/freshness-badge";
import { SourceAttribution } from "@/components/data/source-attribution";
import { PageHeader } from "@/components/shell/page-header";
import { NATURAL_GAS } from "@/lib/dashboard";

export const metadata: Metadata = { title: "Natural Gas" };

export default function NaturalGasPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Natural Gas"
        description="Storage, production, and LNG exports from EIA. Storage is weekly (week ending Friday); production and LNG exports are monthly totals in million cubic feet, not daily rates."
        aside={<FreshnessLegend />}
      />
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <Section title="Henry Hub price" meta="Spot benchmark" className="xl:col-span-3">
          <UnavailablePricePanel filter={(label) => label.includes("Henry Hub")} />
        </Section>
        <Section
          title="Supply & storage"
          meta="EIA"
          className="xl:col-span-3"
          footer={<SourceAttribution metrics={NATURAL_GAS} />}
        >
          <div className="space-y-2">
            <SeriesCards metrics={NATURAL_GAS} />
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-3">
              {NATURAL_GAS.map((m) => (
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
          <CalculatedMetricsPanel metrics={NATURAL_GAS} />
        </Section>
      </div>
    </div>
  );
}
