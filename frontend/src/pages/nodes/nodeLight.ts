import type { NodeSummary } from "../../api/endpoints";

/** Flat status light colour per node state (the Nodes list's dots). */
export const STATE_LIGHT: Record<string, string> = {
  online: "bg-status-ok",
  suspect: "bg-status-degraded",
  offline: "bg-status-failed",
  draining: "bg-status-busy",
  enrolling: "bg-status-busy",
  revoked: "bg-status-unknown",
};

/** Connected-ness is a separate fact from state (D16): true when the state alone would hide it. */
export function isSilent(node: Pick<NodeSummary, "state" | "connected">): boolean {
  return !node.connected && node.state !== "offline" && node.state !== "revoked";
}
