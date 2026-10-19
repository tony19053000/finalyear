/**
 * When the Benchmarks pages poll (D221: "live update from campaign.progress,
 * with a fallback poll when the event stream is down").
 *
 * - Stream not live (connecting, reconnecting, stopped): poll every
 *   `STREAM_DOWN_POLL_MS` while something can still move.
 * - Stream live: `campaign.progress` only exists for **runner-owned**
 *   campaigns; an active campaign driven by `compute bench submit` (G3's
 *   manual driver) sends no event, so it is polled at `MANUAL_POLL_MS`.
 * - Nothing open (every campaign done / cancelled): no polling at all.
 */
export const STREAM_DOWN_POLL_MS = 5_000;
export const MANUAL_POLL_MS = 15_000;

export interface PollNeed {
  /** Some campaign shown is active or paused. */
  open: boolean;
  /** Some active campaign shown is not (known to be) runner-owned. */
  unowned: boolean;
}

export function pollInterval(live: boolean, need: PollNeed): number | false {
  if (!need.open) return false;
  if (!live) return STREAM_DOWN_POLL_MS;
  return need.unowned ? MANUAL_POLL_MS : false;
}
