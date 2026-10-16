/**
 * Live updates for the Settings pages (V0.8 G5 item 2, D172).
 *
 * The event bus carries no invite events (an invite being used or revoked is
 * not published), but a laptop that joins with a code connects right after,
 * and that IS published: `node.status`. So on every `node.status` the invite
 * list is invalidated - refetched at once only when an Invites page has it
 * mounted, otherwise just marked stale (no request). The node itself appears
 * through the generic applier (events/applyEvent.ts), which refetches the
 * node list. Never throws; returns whether it acted.
 */
import type { QueryClient } from "@tanstack/react-query";

import { queryKeys } from "../../api/queryKeys";
import type { FabricEvent } from "../../events/eventStream";

export function applySettingsEvent(client: QueryClient, event: FabricEvent): boolean {
  if (event.type !== "node.status") return false;
  void client.invalidateQueries({ queryKey: queryKeys.invites, exact: true });
  return true;
}
