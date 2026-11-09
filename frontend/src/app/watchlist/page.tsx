import type { Metadata } from "next";

import { ModulePlaceholder } from "@/components/dashboard/module-placeholder";
import { PageHeader } from "@/components/shell/page-header";

export const metadata: Metadata = { title: "Watchlist" };

export default function WatchlistPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Watchlist"
        status="planned"
        description="Watchlists and the events calendar."
      />
      <ModulePlaceholder
        milestone="Milestone 6"
        summary="Custom lists of series and companies, alongside scheduled releases such as EIA reports, economic data, and earnings."
        planned={[
          "Custom watchlists of series and companies",
          "Calendar of EIA, economic, and earnings releases",
          "Alerts on new releases and stale data",
          "Every item linked to its source",
        ]}
      />
    </div>
  );
}
