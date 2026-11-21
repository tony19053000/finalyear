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
import { ErrorState } from "../../components/States";
import { Table, type Column } from "../../components/Table";
import { formatAgo, formatMs, formatRate, shortId } from "../../lib/format";
import { FrostTable } from "./FrostTable";
import { peerRows, type PeerRow } from "./networkView";

function PeerName({ id, names }: { id: string; names: Map<string, string> }) {
  return (
    <Link
      to={`/nodes/${encodeURIComponent(id)}`}
      className="font-medium text-fg underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline"
      title={id}
    >
      {names.get(id) ?? shortId(id)}
    </Link>
  );
}

export function NodeNetwork({ nodeId, nodes }: { nodeId: string; nodes: readonly NodeSummary[] }) {
  const network = useNetwork();
  const names = new Map(nodes.map((n) => [n.node_id, n.hostname]));

  if (network.isPending) {
    return (
      <p role="status" className="py-10 text-sm text-fg-muted">
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
  const measured = rows.filter((r) => r.link !== null).length;

  const columns: Column<PeerRow>[] = [
    { id: "peer", header: "Peer", cell: (r) => <PeerName id={r.peer} names={names} /> },
    {
      id: "direction",
      header: "Direction",
      cell: (r) =>
        r.direction === "from_peer" ? (
          <span title={`${self} downloads from it`}>
            <span aria-hidden="true" className="mr-2 text-fg-subtle">↓</span>download
          </span>
        ) : (
          <span title={`it downloads from ${self}`}>
            <span aria-hidden="true" className="mr-2 text-fg-subtle">↑</span>upload
          </span>
        ),
    },
    {
      id: "bw",
      header: "Bandwidth",
      align: "right",
      mono: true,
      cell: (r) => (r.link ? formatRate(r.link.bw_bytes_s) : <span className="text-fg-subtle">not measured</span>),
    },
    {
      id: "rtt",
      header: "RTT",
      align: "right",
      mono: true,
      cell: (r) => (r.link ? formatMs(r.link.rtt_ms) : <span className="text-fg-subtle">—</span>),
    },
    {
      id: "age",
      header: "Measured",
      cell: (r): ReactNode =>
        r.link ? (
          <span className="inline-flex items-center gap-2.5">
            <span className="text-fg-muted">{formatAgo(r.link.age_s)}</span>
            {r.link.stale && (
              <span className="inline-flex items-center gap-1.5 text-status-degraded">
                <span aria-hidden="true" className="nd-light nd-light-sm bg-status-degraded" />
                stale
              </span>
            )}
          </span>
        ) : (
          <span className="text-fg-subtle">unknown</span>
        ),
    },
    {
      id: "failure",
      header: "Last probe failure",
      cell: (r) =>
        r.failure ? (
          <span className="inline-flex items-center gap-2 text-status-failed" title={r.failure.at}>
            <span aria-hidden="true" className="nd-light nd-light-sm bg-status-failed" />
            {r.failure.error}
          </span>
        ) : (
          <span className="text-fg-subtle">—</span>
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-12">
      <FrostTable
        title="Peer links"
        testId="network-card"
        className="nd-net"
        meta={
          <span className="font-mono text-[0.6875rem] tracking-[0.12em] text-fg-subtle uppercase">
            {measured} of {rows.length} measured
          </span>
        }
      >
        <Table
          caption={`Measured links between ${self} and the other nodes`}
          columns={columns}
          rows={rows}
          rowKey={(r) => `${r.peer}-${r.direction}`}
          empty="No other node in the fabric to measure against."
        />
      </FrostTable>

      <div className="grid grid-cols-1 gap-10 px-10 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section aria-label="Controller to this node">
          <h2 className="nd-label">Controller → this node</h2>
          {controller ? (
            <>
              <p className="ov-figure mt-4 text-[2.25rem] whitespace-nowrap text-fg sm:text-[2.75rem]">{formatRate(controller.bw_bytes_s)}</p>
              <p className="mt-2.5 text-sm text-fg-muted">
                Median of {controller.samples} recent transfers · last {formatAgo(controller.age_s)}
              </p>
            </>
          ) : (
            <p className="mt-4 text-sm text-fg-muted">Not measured yet - no transfer from the controller to this node so far.</p>
          )}
        </section>
        <section aria-label="Staleness" className="md:text-right">
          <h2 className="nd-label">Staleness</h2>
          <p className="ov-figure mt-4 text-[2.25rem] whitespace-nowrap text-fg sm:text-[2.75rem]">
            {Math.round(data.stale_after_s)}
            <span className="ml-1.5 text-[1.25rem] font-light tracking-normal text-fg-muted">s</span>
          </p>
          <p className="mt-2.5 text-sm text-fg-muted md:ml-auto md:max-w-[26rem]">
            Measurements older than this are stale: the planner ignores them until the next probe.
          </p>
        </section>
      </div>
    </div>
  );
}
