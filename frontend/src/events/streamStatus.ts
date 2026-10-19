/**
 * The app's event stream status, for pages that must fall back to polling
 * while live events are not arriving (V0.9 G6: the Benchmarks pages). The
 * shell provides it; outside a shell (a page rendered alone in a test) it is
 * "stopped", i.e. "no live events": the page polls.
 */
import { createContext, useContext } from "react";

import type { EventStreamStatus } from "./eventStream";

export const StreamStatusContext = createContext<EventStreamStatus>("stopped");

export function useStreamStatus(): EventStreamStatus {
  return useContext(StreamStatusContext);
}

/** True while events flow (the stream said hello); anything else may be missing updates. */
export function useStreamLive(): boolean {
  return useStreamStatus() === "live";
}
