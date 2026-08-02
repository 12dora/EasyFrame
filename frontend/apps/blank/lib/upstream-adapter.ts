import type { EnterpriseUpstreamHealthAdapter, EnterpriseUpstreamHealthItem } from "@easy-enterprise/ui/enterprise";
import { platformRequest } from "./platform-api";

export const upstreamHealthAdapter: EnterpriseUpstreamHealthAdapter = {
  load: () => platformRequest<EnterpriseUpstreamHealthItem[]>("/api/v1/ops/upstream-health"),
  runChecks: () => platformRequest<EnterpriseUpstreamHealthItem[]>("/api/v1/ops/upstream-health/checks", { method: "POST", body: "{}" }),
};
