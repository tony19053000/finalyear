/**
 * Nodes list (0.8.4): every node from `GET /v1/nodes`, live through the
 * event stream (node.status patches the state, node.metrics the load). Sort by
 * a column header, filter by status; both live in the URL (`?status=&sort=&dir=`)
 * so back/forward and a shared link keep them. Each node name is a link: the
 * table is fully keyboard-navigable (Tab to a name, Enter opens it).
 */
import type { ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import type { NodeSummary } from "../../api/endpoints";
import { useNodes } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { Meter } from "../../components/Meter";
import { EmptyState, ErrorState } from "../../components/States";
import { Table, type Column } from "../../components/Table";
import { cn } from "../../lib/cn";
import { formatAgo, formatBytes, formatDateTime, formatPercent, percentOf, secondsSince, shortId } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";
import { PageHeader } from "../PageHeader";
import { NodeStatus } from "./NodeStatus";
import { countByState, sortNodes, type NodeSortKey } from "./nodeView";

const SORT_KEYS: readonly NodeSortKey[] = ["name", "state", "cpu", "memory", "gpu", "heartbeat"];

type Direction = "ascending" | "descending";

function parseSort(params: URLSearchParams): { key: NodeSortKey; dir: Direction } {
  const key = params.get("sort");
  const dir = params.get("dir") === "desc" ? "descending" : "ascending";
  return { key: SORT_KEYS.includes(key as NodeSortKey) ? (key as NodeSortKey) : "name", dir };
}

function SortHeader({
  label,
  column,
  sort,
  onSort,
}: {
  label: string;
  column: NodeSortKey;
  sort: { key: NodeSortKey; dir: Direction };
  onSort: (key: NodeSortKey) => void;
}) {
  const active = sort.key === column;
  return (
    <button
      type="button"
      className={cn("inline-flex items-center gap-1 uppercase hover:text-fg", active && "text-fg")}
      onClick={() => {
        onSort(column);
      }}
    >
      {label}
      <span aria-hidden="true" className="text-2xs">
        {active ? (sort.dir === "ascending" ? "▲" : "▼") : "↕"}
      </span>
    </button>
  );
}

function Usage({ percent, text }: { percent: number | null; text: string }) {
  return (
    <span className="flex min-w-24 flex-col gap-1">
      <span className="tabular-nums">{text}</span>
      <Meter percent={percent} className="max-w-28" />
    </span>
  );
}

function gpuText(node: NodeSummary): ReactNode {
  const gpus = node.gpus;
  if (!node.connected || node.cpu_percent == null) return <span className="text-fg-muted">—</span>;
  if (gpus.length === 0) return <span className="text-fg-muted">none</span>;
  const mean = gpus.reduce((a, g) => a + g.util_percent, 0) / gpus.length;
  return <Usage percent={mean} text={`${String(gpus.length)} × ${formatPercent(mean)}`} />;
}

export function NodesPage() {
  useDocumentTitle("Nodes");
  const nodes = useNodes();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const now = useServerNow();
  const sort = parseSort(params);
  const status = params.get("status");

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace: true });
  };
  const onSort = (key: NodeSortKey) => {
    const dir = sort.key === key && sort.dir === "ascending" ? "desc" : "asc";
    update({ sort: key, dir });
  };

  const columns: Column<NodeSummary>[] = [
    {
      id: "name",
      header: <SortHeader label="Node" column="name" sort={sort} onSort={onSort} />,
      sort: sort.key === "name" ? sort.dir : "none",
      cell: (n) => (
        <span className="flex flex-col">
          <Link
            to={`/nodes/${encodeURIComponent(n.node_id)}`}
            className="font-medium text-fg underline-offset-2 hover:text-accent hover:underline"
            onClick={(e) => {
              e.stopPropagation();
            }}
          >
            {n.display_name ? n.display_name : n.hostname}
          </Link>
          <span className="font-mono text-2xs text-fg-muted" title={n.node_id}>
            {/* V0.17 G3 (Q27): the owner's name above, the hostname here */}
            {n.display_name ? `${n.hostname} · ${shortId(n.node_id)}` : shortId(n.node_id)}
          </span>
        </span>
      ),
    },
    {
      id: "state",
      header: <SortHeader label="Status" column="state" sort={sort} onSort={onSort} />,
      sort: sort.key === "state" ? sort.dir : "none",
      cell: (n) => <NodeStatus node={n} />,
    },
    {
      id: "os",
      header: "OS",
      cell: (n) => (
        <span className="flex flex-col">
          <span>{n.os}</span>
          <span className="text-2xs text-fg-muted">{n.platform}</span>
        </span>
      ),
    },
    {
      id: "cpu",
      header: <SortHeader label="CPU" column="cpu" sort={sort} onSort={onSort} />,
      sort: sort.key === "cpu" ? sort.dir : "none",
      cell: (n) =>
        n.connected && n.cpu_percent != null ? (
          <Usage percent={n.cpu_percent} text={formatPercent(n.cpu_percent)} />
        ) : (
          <span className="text-fg-muted">—</span>
        ),
    },
    {
      id: "memory",
      header: <SortHeader label="Memory" column="memory" sort={sort} onSort={onSort} />,
      sort: sort.key === "memory" ? sort.dir : "none",
      cell: (n) => {
        if (!n.connected || n.ram_used_bytes == null) return <span className="text-fg-muted">—</span>;
        const pct = percentOf(n.ram_used_bytes, n.ram_total_bytes);
        return <Usage percent={pct} text={`${formatBytes(n.ram_used_bytes)} / ${formatBytes(n.ram_total_bytes)}`} />;
      },
    },
    {
      id: "gpu",
      header: <SortHeader label="GPU" column="gpu" sort={sort} onSort={onSort} />,
      sort: sort.key === "gpu" ? sort.dir : "none",
      cell: gpuText,
    },
    {
      id: "heartbeat",
      header: <SortHeader label="Last heartbeat" column="heartbeat" sort={sort} onSort={onSort} />,
      sort: sort.key === "heartbeat" ? sort.dir : "none",
      cell: (n) => (
        <time dateTime={n.last_seen_at} title={formatDateTime(n.last_seen_at)} className="whitespace-nowrap">
          {formatAgo(secondsSince(n.last_seen_at, now))}
        </time>
      ),
    },
  ];

  let content: ReactNode;
  if (nodes.isPending) {
    content = (
      <p role="status" className="text-sm text-fg-muted">
        Loading nodes…
      </p>
    );
  } else if (nodes.isError) {
    content = (
      <ErrorState
        title="Could not load the nodes"
        error={nodes.error}
        onRetry={() => {
          void nodes.refetch();
        }}
      />
    );
  } else if (nodes.data.nodes.length === 0) {
    content = (
      <EmptyState title="No nodes yet - create an invite">
        <CodeBlock
          className="mt-1 text-left"
          label="Add a node"
          code={"# on the controller host\nmeshforge-controller fabric invite\n\n# on the new laptop\nmeshforge-agent join <code>"}
        />
      </EmptyState>
    );
  } else {
    const all = nodes.data.nodes;
    const counts = countByState(all);
    const shown = sortNodes(
      status === null ? all : all.filter((n) => n.state === status),
      sort.key,
      sort.dir,
    );
    content = (
      <Card flush>
        <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-1.5 border-b border-border px-3 py-2">
          <Button
            size="sm"
            variant={status === null ? "primary" : "ghost"}
            aria-pressed={status === null}
            onClick={() => {
              update({ status: null });
            }}
          >
            All ({all.length})
          </Button>
          {[...counts.entries()].map(([state, count]) => (
            <Button
              key={state}
              size="sm"
              variant={status === state ? "primary" : "ghost"}
              aria-pressed={status === state}
              onClick={() => {
                update({ status: state });
              }}
            >
              {state} ({count})
            </Button>
          ))}
          {status !== null && !counts.has(status) && (
            <Button size="sm" variant="primary" aria-pressed onClick={() => { update({ status: null }); }}>
              {status} (0)
            </Button>
          )}
        </div>
        <Table
          caption={`Nodes${status === null ? "" : ` with status ${status}`}, sorted by ${sort.key} ${sort.dir}`}
          columns={columns}
          rows={shown}
          rowKey={(n) => n.node_id}
          onRowClick={(n) => {
            void navigate(`/nodes/${encodeURIComponent(n.node_id)}`);
          }}
          empty={`No node is ${status ?? "listed"}.`}
        />
      </Card>
    );
  }

  return (
    <>
      <PageHeader title="Nodes" description="Every machine in the fabric: state, live load, last heartbeat." />
      {content}
    </>
  );
}
