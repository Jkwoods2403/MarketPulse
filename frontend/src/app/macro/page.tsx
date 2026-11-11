import type { Metadata } from "next";

import { Section } from "@/components/dashboard/section";
import { MacroStatusMessage } from "@/components/data/macro-status";
import { PageHeader } from "@/components/shell/page-header";
import { MACRO_STATUS } from "@/lib/fred-status";

export const metadata: Metadata = { title: "Macroeconomics" };

/** No macroeconomic series is available: FRED is retired and no direct source is integrated. */
export default function MacroPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        status="planned"
        title="Macroeconomics"
        description="Rates, inflation, employment, and output, from direct primary sources once integrated."
      />
      <Section title="Macroeconomic indicators" meta={MACRO_STATUS.label}>
        <MacroStatusMessage />
      </Section>
    </div>
  );
}
