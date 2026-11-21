import type { NodeSummary } from "../../api/endpoints";
import { isSilent, STATE_LIGHT } from "./nodeLight";

/**
 * The node's state, plus "no live stream" when the state alone would hide
 * that the node is not connected right now (D16: state and connection are
 * separate facts, never folded into one "online" flag).
 */
export function NodeStatus({ node }: { node: Pick<NodeSummary, "state" | "connected"> }) {
  const silent = isSilent(node);
  return (
    <span className="inline-flex flex-col" data-state={node.state} data-connected={node.connected}>
      <span className="inline-flex items-center gap-2 capitalize">
        <span aria-hidden="true" className={`nd-light nd-light-sm ${STATE_LIGHT[node.state] ?? "bg-status-unknown"}`} />
        {node.state}
      </span>
      {silent && <span className="mt-1 pl-3.5 text-2xs text-fg-muted">no live stream</span>}
    </span>
  );
}
