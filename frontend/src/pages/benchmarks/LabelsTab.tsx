import { ApiError } from "../../api/errors";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { shortId } from "../../lib/format";
import type { GroupLabel, LabelsOut } from "./benchApi";
import { useLabels } from "./benchQueries";
import { formatMargin, labelTone } from "./campaignView";
import { formatParams } from "./chartData";

function bestConfig(g: GroupLabel): string {
  if (g.best_config_id == null) return g.label === "tie" ? `tie: ${String(g.tied_config_ids.length)} configs` : "—";
  const how = g.best_strategy === "local" ? "local on" : `${g.best_strategy ?? "?"} →`;
  return `${how} ${g.best_node_id ?? "?"}`;
}

function agrees(value: boolean | null | undefined): string {
  return value === true ? "yes" : value === false ? "no" : "—";
}

const COLUMNS: Column<GroupLabel>[] = [
  {
    id: "family",
    header: "Family",
    cell: (g) => (
      <span className="flex flex-col">
        <span>{g.family}</span>
        <span className="font-mono text-2xs text-fg-muted" title={g.group_id}>
          {shortId(g.group_id)}
        </span>
      </span>
    ),
  },
  { id: "params", header: "Parameters", className: "text-xs", cell: (g) => formatParams(g.params) },
  { id: "condition", header: "Condition", mono: true, cell: (g) => g.condition },
  {
    id: "label",
    header: "Label",
    cell: (g) => (
      <span className="flex flex-col">
        <Badge tone={labelTone(g.label)}>{g.label}</Badge>
        {g.reason != null && <span className="text-2xs text-fg-muted">{g.reason}</span>}
      </span>
    ),
  },
  {
    id: "best",
    header: "Best config",
    cell: (g) => (
      <span className="font-mono text-xs" title={g.best_config_id ?? undefined}>
        {bestConfig(g)}
      </span>
    ),
  },
  { id: "margin", header: "Margin", align: "right", mono: true, cell: (g) => formatMargin(g.margin_pct) },
  {
    id: "planner",
    header: "Planner agrees",
    cell: (g) => (
      <span title={g.planner_node_id != null ? `planner prefers ${g.planner_node_id}` : undefined}>{agrees(g.planner_agrees)}</span>
    ),
  },
];

export function LabelsSummary({ labels }: { labels: LabelsOut }) {
  return (
    <p className="text-sm text-fg-muted" data-testid="labels-summary">
      {`${String(labels.groups.length)} groups from ${String(labels.rows)} run records: ${String(labels.counts.best)} best, ${String(labels.counts.tie)} tie, ${String(labels.counts.insufficient)} insufficient, ${String(labels.counts.pending)} pending.`}
    </p>
  );
}

/** A labels load failure: 413 (too many records for one request) says so. */
export function LabelsError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.status === 413) {
    return (
      <EmptyState title="Too many records to label in one request">
        {error.message}. Use <code className="font-mono">compute bench labels</code> or the Download tab&apos;s records
        file.
      </EmptyState>
    );
  }
  return <ErrorState title="Could not load labels" error={error} onRetry={onRetry} />;
}

/** Per-group labels (derived on demand from the run records; docs/TELEMETRY.md "Labels"). */
export function LabelsTab({ campaignId }: { campaignId: string }) {
  const labels = useLabels(campaignId);
  if (labels.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading labels…
      </p>
    );
  }
  if (labels.isError) return <LabelsError error={labels.error} onRetry={() => void labels.refetch()} />;
  const data = labels.data;
  return (
    <div className="flex flex-col gap-3">
      <LabelsSummary labels={data} />
      <Table
        caption="Per-group labels"
        columns={COLUMNS}
        rows={data.groups}
        rowKey={(g) => g.group_id}
        empty="No groups yet: labels appear once the campaign has run records."
      />
      <p className="text-xs text-fg-muted">
        best = the lowest-median configuration&apos;s CI (total time) lies strictly below every other
        configuration&apos;s CI; tie = the CIs overlap; insufficient = a configuration has fewer than 3 measured
        repetitions; pending = runs of the group are still open. Margin = how much higher the runner-up&apos;s median
        is, in % of the best&apos;s. Planner agrees = the planner&apos;s preferred node for the group is the best one.
      </p>
    </div>
  );
}
