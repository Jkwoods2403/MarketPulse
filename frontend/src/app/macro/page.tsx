import type { Metadata } from "next";

import { TimeSeriesChart } from "@/components/charts/time-series-chart";
import { Section } from "@/components/dashboard/section";
import { SeriesCards } from "@/components/dashboard/series-cards";
import { FreshnessLegend } from "@/components/data/freshness-badge";
import { FredLicensingLink } from "@/components/data/fred-licensing";
import { StateMessage } from "@/components/data/state-message";
import { PageHeader } from "@/components/shell/page-header";
import { ECONOMY, RATES } from "@/lib/dashboard";
import { FRED_LICENSING_STATUS } from "@/lib/fred-status";

export const metadata: Metadata = { title: "Macroeconomics" };

export default function MacroPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Macroeconomics"
        description={
          <>
            Rates, the dollar, and economic indicators. {FRED_LICENSING_STATUS.summary} No FRED
            values are shown or attributed. Licensing details: <FredLicensingLink />.
          </>
        }
        aside={<FreshnessLegend />}
      />
      <div className="grid grid-cols-1 gap-3">
        <Section title="Rates & dollar" meta={FRED_LICENSING_STATUS.label}>
          <div className="space-y-2">
            <SeriesCards metrics={RATES} />
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-2 2xl:grid-cols-3">
              {RATES.map((m) => (
                <TimeSeriesChart key={m.code} code={m.code} title={m.label} note={m.note} />
              ))}
            </div>
          </div>
        </Section>
        <Section title="Economy" meta={FRED_LICENSING_STATUS.label}>
          <div className="space-y-2">
            <SeriesCards metrics={ECONOMY} />
            <div className="grid grid-cols-1 gap-2 lg:grid-cols-2 2xl:grid-cols-3">
              {ECONOMY.map((m) => (
                <TimeSeriesChart key={m.code} code={m.code} title={m.label} note={m.note} />
              ))}
            </div>
          </div>
        </Section>
        <Section title={FRED_LICENSING_STATUS.metricsHeading} meta={FRED_LICENSING_STATUS.label}>
          <StateMessage tone="unavailable" title={FRED_LICENSING_STATUS.note}>
            {FRED_LICENSING_STATUS.metrics} Licensing details: <FredLicensingLink />.
          </StateMessage>
        </Section>
      </div>
    </div>
  );
}
