/**
 * Query keys of the Benchmarks pages, all under `queryKeys.bench`, so a
 * `campaign.progress` event (applyCampaignEvent) and a control action can
 * patch or invalidate exactly one campaign's entries. Campaign ids are
 * `camp_<ULID>`, so the literals "list" / "status" can never be an id.
 */
import { queryKeys } from "../../api/queryKeys";
import type { RunState } from "./benchApi";

export const benchKeys = {
  all: queryKeys.bench,
  list: [...queryKeys.bench, "list"] as const,
  /** Every status entry (the list rows' and the detail's). */
  statuses: [...queryKeys.bench, "status"] as const,
  status: (campaignId: string) => [...queryKeys.bench, "status", campaignId] as const,
  /** The campaign's own entries: detail, runs, labels, quality. */
  campaign: (campaignId: string) => [...queryKeys.bench, "campaign", campaignId] as const,
  detail: (campaignId: string) => [...queryKeys.bench, "campaign", campaignId, "detail"] as const,
  runs: (campaignId: string, state: RunState | null) =>
    [...queryKeys.bench, "campaign", campaignId, "runs", state ?? "all"] as const,
  labels: (campaignId: string) => [...queryKeys.bench, "campaign", campaignId, "labels"] as const,
  quality: (campaignId: string) => [...queryKeys.bench, "campaign", campaignId, "quality"] as const,
};
