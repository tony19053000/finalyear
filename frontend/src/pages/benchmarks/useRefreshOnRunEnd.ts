import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import type { RunCounts } from "./benchApi";
import { benchKeys } from "./benchKeys";

/** Runs that reached an end state (done, failed, skipped), or null. */
export function endedRuns(counts: RunCounts | undefined): number | null {
  return counts === undefined ? null : counts.done + counts.failed + counts.skipped;
}

/**
 * When the campaign's counts show that a run ended - whether a
 * `campaign.progress` event patched them or a poll refetched them - its runs,
 * labels and quality entries are invalidated (not its detail or status: they
 * are what changed). Only the tab on screen refetches, at most as often as
 * runs end; the first counts seen refresh nothing.
 */
export function useRefreshOnRunEnd(campaignId: string, counts: RunCounts | undefined): void {
  const client = useQueryClient();
  const last = useRef<number | null>(null);
  const ended = endedRuns(counts);
  useEffect(() => {
    if (ended === null) return;
    const before = last.current;
    last.current = ended;
    if (before === null || before === ended) return;
    void client.invalidateQueries({
      queryKey: benchKeys.campaign(campaignId),
      predicate: (q) => q.queryKey[3] !== "detail",
    });
  }, [client, campaignId, ended]);
}
