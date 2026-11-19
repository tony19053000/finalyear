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
import { CodeBlock } from "../../components/CodeBlock";
import { Meter } from "../../components/Meter";
import { EmptyState, ErrorState } from "../../components/States";
import { Table, type Column } from "../../components/Table";
import { cn } from "../../lib/cn";
import { formatBytes, formatPercent, percentOf, shortId } from "../../lib/format";
import { AgentVersion } from "./AgentVersion";
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
    <span className="flex min-w-24 flex-col items-center gap-1">
      <span className="tabular-nums">{text}</span>
      <Meter percent={percent} className="w-28" />
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

/** Connected-ness is a separate fact from state (D16): say so when the dot alone would hide it. */
function silentOf(n: Pick<NodeSummary, "state" | "connected">): boolean {
  return !n.connected && n.state !== "offline" && n.state !== "revoked";
}

/** The status dot beside each node name (replaces the Status column). */
const DOT: Record<string, string> = {
  online: "bg-status-ok",
  suspect: "bg-status-degraded",
  offline: "bg-status-failed",
  draining: "bg-status-busy",
  enrolling: "bg-status-busy",
  revoked: "bg-status-unknown",
};


export function NodesPage() {
  useDocumentTitle("Nodes");
  const nodes = useNodes();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
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
        <span className="flex">
          <span className="flex flex-col">
          <span className="flex items-center gap-2">
              <Link
            to={`/nodes/${encodeURIComponent(n.node_id)}`}
            className="font-medium text-fg underline-offset-2 hover:text-accent hover:underline"
            onClick={(e) => {
              e.stopPropagation();
            }}
          >
            {n.display_name ? n.display_name : n.hostname}
          </Link>
              <span
                  data-state={n.state}
                  data-connected={n.connected}
                  title={silentOf(n) ? `${n.state} · no live stream` : n.state}
                  className={`h-2 w-2 shrink-0 rounded-full ${DOT[n.state] ?? "bg-status-unknown"}`}
                >
                  <span className="sr-only">{silentOf(n) ? `${n.state}, no live stream` : n.state}</span>
                </span>
            </span>
          <span className="font-mono text-2xs text-fg-muted" title={n.node_id}>
            {/* V0.17 G3 (Q27): the owner's name above, the hostname here */}
            {n.display_name ? `${n.hostname} · ${shortId(n.node_id)}` : shortId(n.node_id)}
          </span>
          </span>
        </span>
      ),
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
      id: "version",
      header: "Agent",
      cell: (n) => <AgentVersion node={n} />,
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
    const shown = sortNodes(
      status === null ? all : all.filter((n) => n.state === status),
      sort.key,
      sort.dir,
    );
    content = (
      <div className="nl-plain ov-table-fit ov-table-big">
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
      </div>
    );
  }

  // Frame-B style text tabs beside the title: All · online · suspect · offline …
  const tabNodes = nodes.data?.nodes ?? [];
  const tabCounts = countByState(tabNodes);
  // every state, always (0 when none), in a fixed order
  const STATES = ["online", "suspect", "offline", "draining", "enrolling", "revoked"] as const;
  const tabs = [
    { key: null, label: "All", count: tabNodes.length },
    ...STATES.map((st) => ({ key: st, label: st, count: tabCounts.get(st) ?? 0 })),
  ];

  return (
    <>
      <div className="mt-3 mb-16 grid grid-cols-1 items-end gap-y-4 lg:grid-cols-[1fr_auto_1fr]">
        <h1 className="font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">Nodes</h1>
        {tabNodes.length > 0 && (
          <div role="group" aria-label="Filter by status" className="flex items-end justify-center gap-10 pb-1.5">
            {tabs.map((t) => {
              const on = status === t.key;
              return (
                <button
                  key={t.label}
                  type="button"
                  aria-pressed={on}
                  aria-label={`${t.label} ${String(t.count)}`}
                  onClick={() => {
                    update({ status: t.key });
                  }}
                  className={`relative pb-2 text-[0.9375rem] capitalize transition-colors duration-300 ${on ? "font-semibold text-fg" : "text-fg-muted hover:text-fg"}`}
                >
                  {t.label}
                  <span className="ml-1.5 font-mono text-[0.6875rem] text-fg-subtle tabular-nums">{t.count}</span>
                  <span
                    aria-hidden="true"
                    className={`absolute bottom-0 left-0 h-0.5 rounded-full bg-fg transition-all duration-300 ${on ? "w-5 opacity-100" : "w-0 opacity-0"}`}
                  />
                </button>
              );
            })}
          </div>
        )}
      </div>
      {content}
    </>
  );
}
