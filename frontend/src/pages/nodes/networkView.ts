/** Pure view of the network matrix for one node (unit-tested). */
import type { NetworkOut } from "../../api/fabric";

type Link_ = NetworkOut["links"][number];

export interface PeerRow {
  peer: string;
  /** "download from peer" = this node pulled from the peer's server. */
  direction: "from_peer" | "to_peer";
  link: Link_ | null;
  failure: { error: string; at: string } | null;
}

/** Every other node, both directions; a pair never probed has `link: null`. */
export function peerRows(network: NetworkOut, nodeId: string, others: readonly string[]): PeerRow[] {
  const rows: PeerRow[] = [];
  for (const peer of others) {
    for (const direction of ["from_peer", "to_peer"] as const) {
      const [src, dst] = direction === "from_peer" ? [nodeId, peer] : [peer, nodeId];
      const link = network.links.find((l) => l.src_node_id === src && l.dst_node_id === dst) ?? null;
      const failed = network.failures.find((f) => f.src_node_id === src && f.dst_node_id === dst);
      const failure = link?.last_failure ?? (failed ? { error: failed.error, at: failed.at } : null);
      rows.push({ peer, direction, link, failure });
    }
  }
  return rows;
}
