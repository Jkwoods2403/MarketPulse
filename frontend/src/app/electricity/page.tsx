import type { Metadata } from "next";

import { Section } from "@/components/dashboard/section";
import { StateMessage } from "@/components/data/state-message";
import { PageHeader } from "@/components/shell/page-header";

export const metadata: Metadata = { title: "Electricity" };

/** ERCOT data is not requested or shown until ERCOT's written licensing answer (ADR-0014). */
export default function ElectricityPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        status="blocked"
        title="Electricity"
        description="ERCOT prices, load, and forecasts (Milestone 4)."
      />
      <Section title="ERCOT" meta="On hold">
        <StateMessage tone="unavailable" title="ERCOT data is not available">
          ERCOT licensing clarification is pending (ERCOT ticket SS0007218). Until ERCOT&apos;s
          written answer and the project owner&apos;s decision are recorded, Market Pulse does
          not request, store, or display any ERCOT data. See ADR-0014 and docs/data-sources.md.
        </StateMessage>
      </Section>
    </div>
  );
}
