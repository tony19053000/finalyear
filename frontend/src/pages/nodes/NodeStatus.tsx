import type { NodeSummary } from "../../api/endpoints";
import { StatusDot } from "../../components/Status";
import { stateTone } from "./nodeView";

/**
 * The node's state, plus "no live stream" when the state alone would hide
 * that the node is not connected right now (D16: state and connection are
 * separate facts, never folded into one "online" flag).
 */
export function NodeStatus({ node }: { node: Pick<NodeSummary, "state" | "connected"> }) {
  const silent = !node.connected && node.state !== "offline" && node.state !== "revoked";
  return (
    <span className="inline-flex flex-col" data-state={node.state} data-connected={node.connected}>
      <StatusDot tone={stateTone(node)} label={node.state} />
      {silent && <span className="pl-3.5 text-2xs text-fg-muted">no live stream</span>}
    </span>
  );
}
