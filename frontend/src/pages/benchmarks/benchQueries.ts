/** Shared queries of the campaign tabs (the Labels and Charts tabs read the same labels entry). */
import { useQuery } from "@tanstack/react-query";

import { getLabels } from "./benchApi";
import { benchKeys } from "./benchKeys";

export function useLabels(campaignId: string) {
  return useQuery({ queryKey: benchKeys.labels(campaignId), queryFn: ({ signal }) => getLabels(campaignId, signal) });
}
