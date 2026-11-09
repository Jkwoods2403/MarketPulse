import type { Metadata } from "next";

import { ModulePlaceholder } from "@/components/dashboard/module-placeholder";
import { PageHeader } from "@/components/shell/page-header";

export const metadata: Metadata = { title: "News" };

export default function NewsPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="News"
        status="planned"
        description="News and the daily market briefing."
      />
      <ModulePlaceholder
        milestone="Milestone 7"
        summary="Headlines with original source links and publication times, and a daily briefing that separates confirmed facts from interpretation."
        planned={[
          "Headline, link, publisher, and timestamp only (no republished articles)",
          "Daily briefing: what happened, why it matters, what to watch",
          "Confirmed facts labeled separately from analysis",
          "Links to the affected markets and series",
        ]}
      />
    </div>
  );
}
