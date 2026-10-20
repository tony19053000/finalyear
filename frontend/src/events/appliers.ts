/**
 * The app's event -> cache pipeline (D172 - no second store): every live
 * event goes through each applier, in order; each patches or invalidates the
 * TanStack Query cache. The shell passes this list to its one event stream.
 *
 * `applyEvent` invalidates the job's detail, the plain jobs list and the
 * Overview's active-job counts on `job.state` / `attempt.state`;
 * `applyJobEvent` (V0.8 G4) patches the jobs list pages in place;
 * `applySettingsEvent` (V0.8 G5) refetches the invite list on `node.status`
 * (a laptop that used a code connects; invites themselves have no event);
 * `applyCampaignEvent` (V0.9 G6) patches the Benchmarks pages on
 * `campaign.progress`; `applySplitEvent` (V0.10 G7) patches a split job's
 * Chunks tab on `split.progress`.
 * Keep it a module constant: a new array identity reconnects the stream.
 */
import { applyCampaignEvent } from "../pages/benchmarks/applyCampaignEvent";
import { applyJobEvent } from "../pages/jobs/applyJobEvent";
import { applySplitEvent } from "../pages/jobs/applySplitEvent";
import { applySettingsEvent } from "../pages/settings/applySettingsEvent";
import { applyEvent, type EventApplier } from "./applyEvent";

export const EVENT_APPLIERS: readonly EventApplier[] = [
  applyEvent,
  applyJobEvent,
  applySettingsEvent,
  applyCampaignEvent,
  applySplitEvent,
];
