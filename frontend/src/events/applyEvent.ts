/**
 * What a fabric event does to the TanStack Query cache (D172: the client
 * keeps no second store - it invalidates/patches the cache the pages read).
 *
 * G2 invalidates by key prefix; G3/G4 replace entries with in-place patches
 * where refetching would be too heavy (node.metrics at 1 Hz per node).
 */
import type { QueryClient } from "@tanstack/react-query";

import { queryKeys } from "../api/queryKeys";
import type { FabricEvent } from "./eventStream";

export function applyEvent(client: QueryClient, event: FabricEvent): void {
  switch (event.type) {
    case "node.status": {
      void client.invalidateQueries({ queryKey: queryKeys.nodes });
      return;
    }
    case "job.state":
    case "attempt.state": {
      const jobId = event.data.job_id;
      if (typeof jobId === "string") {
        void client.invalidateQueries({ queryKey: queryKeys.job(jobId) });
      }
      void client.invalidateQueries({ queryKey: queryKeys.jobs, exact: true });
      return;
    }
    default:
      // node.metrics and alert are consumed by the G3 pages (sparklines,
      // alerts panel); unknown future types are ignored, never an error.
      return;
  }
}

/** A resync: everything shown may be stale - refetch all active queries. */
export function applyResync(client: QueryClient): void {
  void client.invalidateQueries();
}
