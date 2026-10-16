/**
 * The dashboard's five status tones (D175) and how fabric states map onto them.
 * A tone only picks a colour; the UI always prints the real state next to it.
 */
export type StatusTone = "ok" | "busy" | "degraded" | "failed" | "unknown";

/** Node states (controller/liveness.py NodeState). */
const NODE_TONES: Record<string, StatusTone> = {
  online: "ok",
  enrolling: "busy",
  draining: "busy",
  suspect: "degraded",
  offline: "failed",
  revoked: "failed",
};

/** Job states (common/states.py JobState). */
const JOB_TONES: Record<string, StatusTone> = {
  submitted: "unknown",
  planning: "busy",
  queued: "unknown",
  preparing: "busy",
  running: "busy",
  collecting: "busy",
  retrying: "degraded",
  succeeded: "ok",
  failed: "failed",
  cancelled: "unknown",
};

export function nodeTone(state: string | null | undefined): StatusTone {
  return (state != null ? NODE_TONES[state] : undefined) ?? "unknown";
}

export function jobTone(state: string | null | undefined): StatusTone {
  return (state != null ? JOB_TONES[state] : undefined) ?? "unknown";
}
