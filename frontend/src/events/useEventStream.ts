import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { queryKeys } from "../api/queryKeys";
import { applyEvent, applyResync, type EventApplier } from "./applyEvent";
import { EventStreamClient, type EventStreamOptions, type EventStreamStatus } from "./eventStream";

export interface UseEventStreamOptions {
  /** Only connect while true (i.e. while a session exists). */
  enabled: boolean;
  /** The stream ended the session (4401): leave for the login page. */
  onUnauthorized: () => void;
  /**
   * What each event does to the query cache, in order (default: the fabric
   * applier). Must be a stable array (module constant): a new one reconnects.
   */
  appliers?: readonly EventApplier[];
  /** Tests inject a fake socket. */
  socketFactory?: EventStreamOptions["socketFactory"];
  baseUrl?: string;
}

/**
 * One event stream for the whole app (mounted once, by the shell). Returns
 * its connection status for the header; events go into the query cache.
 */
const DEFAULT_APPLIERS: readonly EventApplier[] = [applyEvent];

export function useEventStream({
  enabled,
  onUnauthorized,
  socketFactory,
  baseUrl,
  appliers = DEFAULT_APPLIERS,
}: UseEventStreamOptions): EventStreamStatus {
  const client = useQueryClient();
  const [status, setStatus] = useState<EventStreamStatus>("stopped");

  useEffect(() => {
    if (!enabled) return undefined;
    const stream = new EventStreamClient({
      baseUrl,
      socketFactory,
      onEvent: (event) => {
        for (const apply of appliers) {
          try {
            apply(client, event);
          } catch (err) {
            // One applier's bug must not stop the stream or the others.
            console.warn("event applier failed", event.type, err);
          }
        }
      },
      onResync: () => {
        applyResync(client);
      },
      onStatus: setStatus,
      onUnauthorized,
      // A refused handshake may be a dead session: re-check it (a 401 there
      // sends the user to the login page through the session query).
      onHandshakeFailed: () => {
        void client.invalidateQueries({ queryKey: queryKeys.session });
      },
    });
    stream.start();
    return () => {
      stream.stop();
      setStatus("stopped");
    };
  }, [enabled, client, onUnauthorized, socketFactory, baseUrl, appliers]);

  return status;
}
