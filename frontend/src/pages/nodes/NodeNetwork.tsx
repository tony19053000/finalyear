/**
 * The node's rows of the measured network matrix (`GET /v1/network`, D111):
 * peer links in both directions, the controller -> node bandwidth, probe
 * failures - and, for every other node, "not measured yet" rather than a
 * blank. Stale measurements (older than the controller's `stale_after_s`, so
 * the planner ignores them) are labelled stale, never shown as current.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { NodeSummary } from "../../api/endpoints";
import { useNetwork } from "../../api/fabric";
import { Card } from "../../components/Card";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatAgo, formatMs, formatRate, shortId } from "../../lib/format";
import { peerRows, type PeerRow } from "./networkView";

function PeerName({ id, names }: { id: string; names: Map<string, string> }) {
  return (
    <Link to={`/nodes/${encodeURIComponent(id)}`} className="text-accent underline-offset-2 hover:underline" title={id}>
      {names.get(id) ?? shortId(id)}
    </Link>
  );
}

export function NodeNetwork({ nodeId, nodes }: { nodeId: string; nodes: readonly NodeSummary[] }) {
  const network = useNetwork();
  const names = new Map(nodes.map((n) => [n.node_id, n.hostname]));

  if (network.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the network matrix…
      </p>
    );
  }
  if (network.isError) {
    return (
      <ErrorState
        title="Could not load the network matrix"
        error={network.error}
        onRetry={() => {
          void network.refetch();
        }}
      />
    );
  }
  const data = network.data;
  const others = nodes.filter((n) => n.node_id !== nodeId && n.state !== "revoked").map((n) => n.node_id);
  const rows = peerRows(data, nodeId, others);
  const controller = data.controller_links.find((l) => l.node_id === nodeId);
  const self = names.get(nodeId) ?? "this node";

  const columns: Column<PeerRow>[] = [
    { id: "peer", header: "Peer", cell: (r) => <PeerName id={r.peer} names={names} /> },
    {
      id: "direction",
      header: "Direction",
      cell: (r) => (r.direction === "from_peer" ? `${self} downloads from it` : `it downloads from ${self}`),
    },
    {
      id: "bw",
      header: "Bandwidth",
      align: "right",
      mono: true,
      cell: (r) => (r.link ? formatRate(r.link.bw_bytes_s) : <span className="text-fg-muted">not measured</span>),
    },
    { id: "rtt", header: "RTT", align: "right", mono: true, cell: (r) => (r.link ? formatMs(r.link.rtt_ms) : "—") },
    {
      id: "age",
      header: "Measured",
      cell: (r): ReactNode =>
        r.link ? (
          <span className="inline-flex items-center gap-2">
            {formatAgo(r.link.age_s)}
            {r.link.stale && <Badge tone="degraded">stale</Badge>}
          </span>
        ) : (
          <Badge tone="unknown">unknown</Badge>
        ),
    },
    {
      id: "failure",
      header: "Last probe failure",
      cell: (r) =>
        r.failure ? (
          <span className="text-status-failed" title={r.failure.at}>
            {r.failure.error}
          </span>
        ) : (
          <span className="text-fg-muted">—</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <Card title="Peer links" flush data-testid="network-card">
        <Table
          caption={`Measured links between ${self} and the other nodes`}
          columns={columns}
          rows={rows}
          rowKey={(r) => `${r.peer}-${r.direction}`}
          empty="No other node in the fabric to measure against."
        />
      </Card>
      <Card title="Controller to this node">
        {controller ? (
          <p className="text-sm">
            <span className="font-mono tabular-nums">{formatRate(controller.bw_bytes_s)}</span>{" "}
            <span className="text-fg-muted">
              (median of {controller.samples} recent transfers, last {formatAgo(controller.age_s)})
            </span>
          </p>
        ) : (
          <p className="text-sm text-fg-muted">Not measured yet - no transfer from the controller to this node so far.</p>
        )}
        <p className="mt-2 text-xs text-fg-muted">
          Measurements older than {Math.round(data.stale_after_s)} s are stale: the planner ignores them until the next probe.
        </p>
      </Card>
    </div>
  );
}
