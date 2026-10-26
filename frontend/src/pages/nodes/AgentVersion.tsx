/**
 * V0.17 G5 (Q37): the agent's version and, when it is one minor behind the
 * controller, a badge saying so (still accepted - upgrade it soon). The status
 * comes from the API (`agent_version_status`); nothing is computed here.
 */
import type { NodeSummary } from "../../api/endpoints";
import { Badge } from "../../components/Status";

type VersionFacts = Pick<NodeSummary, "agent_version" | "agent_version_status">;

export const BEHIND_HELP = "One minor version older than the controller: still accepted, upgrade this agent soon.";
export const UNSUPPORTED_HELP =
  "The controller refuses this agent's version (version_unsupported): upgrade the agent, or the controller first if the agent is newer.";

export function AgentVersion({ node, empty = "—" }: { node: VersionFacts; empty?: string }) {
  const version = node.agent_version;
  if (version == null || version === "") {
    return <span className="text-fg-muted">{empty}</span>;
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className="font-mono text-xs">{version}</span>
      {node.agent_version_status === "behind" && (
        <span title={BEHIND_HELP}>
          <Badge tone="degraded">update available</Badge>
        </span>
      )}
      {node.agent_version_status === "unsupported" && (
        <span title={UNSUPPORTED_HELP}>
          <Badge tone="failed">unsupported</Badge>
        </span>
      )}
    </span>
  );
}
