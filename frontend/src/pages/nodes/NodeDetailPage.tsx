/**
 * Node detail (0.8.4): `GET /v1/nodes/{id}` (live through node.status /
 * node.metrics events), live sparklines, hardware + disks + scores, network
 * links, caches, the owner policy (read-only, D142) and - for an admin -
 * Revoke. Tabs are Radix Tabs (arrow keys move between them).
 */
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { useNode } from "../../api/fabric";
import { useNodes, useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Card } from "../../components/Card";
import { EmptyState, ErrorState } from "../../components/States";
import { Tabs } from "../../components/Tabs";
import { formatAgo, formatDateTime, formatMs, secondsSince } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";
import { PageHeader } from "../PageHeader";
import { LiveMetrics } from "./LiveMetrics";
import { NodeCaches, NodePolicy } from "./NodeCachesPolicy";
import { NodeHardware } from "./NodeHardware";
import { NodeNetwork } from "./NodeNetwork";
import { NodeStatus } from "./NodeStatus";
import { RevokeNode } from "./RevokeNode";

function Fact({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-3 py-1">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className="min-w-0 text-sm break-words text-fg" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

const unknown = <span className="text-fg-subtle">not reported</span>;

function BackLink() {
  return (
    <Link to="/nodes" className="text-sm text-accent underline-offset-2 hover:underline">
      ← All nodes
    </Link>
  );
}

export function NodeDetailPage() {
  const { nodeId = "" } = useParams();
  const node = useNode(nodeId);
  const nodes = useNodes();
  const session = useSession();
  const now = useServerNow();
  useDocumentTitle(node.data?.hostname ?? "Node");
  const isAdmin = session.data?.user.role === "admin";

  if (node.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the node…
      </p>
    );
  }
  if (node.isError) {
    const missing = node.error instanceof ApiError && (node.error.status === 404 || node.error.status === 400);
    return (
      <>
        <PageHeader title="Node" />
        {missing ? (
          <EmptyState title="No such node" action={<BackLink />}>
            The fabric has no node <span className="font-mono">{nodeId}</span>.
          </EmptyState>
        ) : (
          <ErrorState
            title="Could not load the node"
            error={node.error}
            onRetry={() => {
              void node.refetch();
            }}
          />
        )}
      </>
    );
  }

  const n = node.data;
  const others = nodes.data?.nodes ?? [];
  return (
    <>
      <div className="mb-2">
        <BackLink />
      </div>
      <PageHeader
        title={n.hostname}
        description={`${n.os} · ${n.platform}`}
        actions={isAdmin && n.state !== "revoked" ? <RevokeNode nodeId={n.node_id} hostname={n.hostname} /> : undefined}
      />
      <div className="flex flex-col gap-5">
        <Card title="Summary" data-testid="summary-card">
          <dl className="grid grid-cols-1 gap-x-8 lg:grid-cols-2">
            <Fact label="Status" testId="node-status">
              <NodeStatus node={n} />
            </Fact>
            <Fact label="Node id">
              <span className="font-mono text-xs">{n.node_id}</span>
            </Fact>
            <Fact label="Last heartbeat" testId="node-heartbeat">
              <time dateTime={n.last_seen_at} title={formatDateTime(n.last_seen_at)}>
                {formatAgo(secondsSince(n.last_seen_at, now))}
              </time>
            </Fact>
            <Fact label="First seen">{formatDateTime(n.first_seen_at)}</Fact>
            <Fact label="Agent version">{n.agent_version ?? unknown}</Fact>
            <Fact label="Protocol">{n.protocol_version ?? unknown}</Fact>
            <Fact label="Address">{n.peer_address ?? unknown}</Fact>
            <Fact label="Round trip">
              {n.rtt_ms == null ? (
                unknown
              ) : (
                <>
                  {formatMs(n.rtt_ms)} <span className="text-fg-muted">(average {formatMs(n.rtt_ewma_ms)})</span>
                </>
              )}
            </Fact>
            <Fact label="Certificate">
              {n.certificate ? (
                <>
                  <span className="font-mono text-xs">{n.certificate.serial}</span>
                  <span className="text-fg-muted"> · valid until {formatDateTime(n.certificate.not_after)}</span>
                </>
              ) : (
                <span className="text-fg-muted">never enrolled</span>
              )}
            </Fact>
          </dl>
        </Card>
        <Tabs
          label="Node details"
          items={[
            { value: "live", label: "Live", content: <LiveMetrics nodeId={n.node_id} /> },
            { value: "hardware", label: "Hardware", content: <NodeHardware node={n} /> },
            { value: "network", label: "Network", content: <NodeNetwork nodeId={n.node_id} nodes={others} /> },
            {
              value: "caches",
              label: "Caches & policy",
              content: (
                <div className="flex flex-col gap-5">
                  <NodeCaches node={n} />
                  <NodePolicy node={n} />
                </div>
              ),
            },
          ]}
        />
      </div>
    </>
  );
}
