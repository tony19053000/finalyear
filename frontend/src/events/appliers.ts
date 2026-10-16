/**
 * The app's event -> cache pipeline (D172 - no second store): every live
 * event goes through each applier, in order; each patches or invalidates the
 * TanStack Query cache. The shell passes this list to its one event stream.
 *
 * `applyEvent` invalidates the job's detail, the plain jobs list and the
 * Overview's active-job counts on `job.state` / `attempt.state`;
 * `applyJobEvent` (V0.8 G4) patches the jobs list pages in place.
 * Keep it a module constant: a new array identity reconnects the stream.
 */
import { applyJobEvent } from "../pages/jobs/applyJobEvent";
import { applyEvent, type EventApplier } from "./applyEvent";

export const EVENT_APPLIERS: readonly EventApplier[] = [applyEvent, applyJobEvent];
