import { Suspense } from "react";

import { TimeSeriesChart } from "@/components/charts/time-series-chart";
import { Section } from "@/components/dashboard/section";
import { SeriesCards } from "@/components/dashboard/series-cards";
import { UnavailablePricePanel } from "@/components/dashboard/unavailable-panel";
import { FreshnessLegend } from "@/components/data/freshness-badge";
import { FredLicensingNotice } from "@/components/data/fred-licensing";
import { SourceAttribution } from "@/components/data/source-attribution";
import { PageHeader } from "@/components/shell/page-header";
import { ApiStatus, ApiStatusFallback } from "@/components/system/api-status";
import {
  CRUDE_PETROLEUM,
  ECONOMY,
  KEY_INDICATORS,
  METRICS,
  NATURAL_GAS,
  withoutFred,
} from "@/lib/dashboard";
import { FRED_LICENSING_STATUS } from "@/lib/fred-status";

const CRUDE_CARDS = CRUDE_PETROLEUM.slice(1, 5);
const GAS_CARDS = NATURAL_GAS.slice(1);
const MACRO_CARDS = [METRICS.ust2y, METRICS.ust30y, METRICS.usdIndex, ...ECONOMY];

export default function MarketOverview() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Market Overview"
        description={`Latest observed values, with calculated changes labeled separately, across crude oil, petroleum products, and natural gas, each with its period, retrieval time, freshness, and source. Sources publish with a delay: nothing on this terminal is a live quote, and daily, weekly, and monthly series are judged against their own release schedules. ${FRED_LICENSING_STATUS.indicators}`}
        aside={<FreshnessLegend />}
      />

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <Section title="Energy prices" meta="Spot benchmarks" className="xl:col-span-3">
          <UnavailablePricePanel />
        </Section>

        <Section
          title="Key indicators"
          meta="Latest values"
          className="xl:col-span-3"
          footer={<SourceAttribution metrics={withoutFred(KEY_INDICATORS)} />}
        >
          <SeriesCards metrics={KEY_INDICATORS} />
        </Section>

        <Section
          title="Crude oil & petroleum"
          meta="EIA · weekly"
          href="/crude-petroleum"
          className="xl:col-span-2"
          footer={<SourceAttribution metrics={CRUDE_CARDS} />}
        >
          <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
            <TimeSeriesChart
              code={METRICS.crudeStocks.code}
              title={METRICS.crudeStocks.label}
              height={200}
            />
            <SeriesCards metrics={CRUDE_CARDS} layout="stack" />
          </div>
        </Section>

        <Section
          title="Natural gas"
          meta="EIA · weekly & monthly"
          href="/natural-gas"
          footer={<SourceAttribution metrics={GAS_CARDS} />}
        >
          <div className="space-y-2">
            <TimeSeriesChart
              code={METRICS.gasStorage.code}
              title={METRICS.gasStorage.label}
              height={180}
            />
            <SeriesCards metrics={GAS_CARDS} layout="stack" />
          </div>
        </Section>

        <Section
          title="Macroeconomic indicators"
          meta={FRED_LICENSING_STATUS.label}
          href="/macro"
          className="xl:col-span-2"
          footer={<FredLicensingNotice />}
        >
          <div className="space-y-2">
            <TimeSeriesChart
              code={METRICS.ust10y.code}
              title={METRICS.ust10y.label}
              height={180}
            />
            <SeriesCards metrics={MACRO_CARDS} />
          </div>
        </Section>

        <Section title="System" meta="Backend API">
          <Suspense fallback={<ApiStatusFallback />}>
            <ApiStatus />
          </Suspense>
        </Section>
      </div>
    </div>
  );
}
