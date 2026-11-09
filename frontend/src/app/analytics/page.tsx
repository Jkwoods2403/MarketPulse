import type { Metadata } from "next";

import { ModulePlaceholder } from "@/components/dashboard/module-placeholder";
import { PageHeader } from "@/components/shell/page-header";

export const metadata: Metadata = { title: "Analytics" };

export default function AnalyticsPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Analytics"
        status="planned"
        description="Market Connections and the Analytics Lab."
      />
      <ModulePlaceholder
        milestone="Milestone 9"
        summary="Statistical analysis of relationships between markets, with out-of-sample validation. Correlations are never presented as causation."
        planned={[
          "Relationship explorer across commodities, rates, and macro series",
          "Statistical tests with stated assumptions",
          "Point-in-time (as-of) data to avoid look-ahead bias",
          "Model results compared against simple baselines",
        ]}
      />
    </div>
  );
}
