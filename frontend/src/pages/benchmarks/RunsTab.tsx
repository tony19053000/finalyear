import { useInfiniteQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import { Button } from "../../components/Button";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { shortId } from "../../lib/format";
import { RUN_STATES, listRuns, type RunList, type RunOut, type RunState } from "./benchApi";
import { benchKeys } from "./benchKeys";
import { runTone } from "./campaignView";

const COLUMNS: Column<RunOut>[] = [
  { id: "seq", header: "#", align: "right", mono: true, cell: (r) => r.seq },
  {
    id: "group",
    header: "Group",
    mono: true,
    cell: (r) => (
      <span title={r.group_id}>
        {shortId(r.group_id)} <span className="font-sans text-fg-muted">{r.family}</span>
      </span>
    ),
  },
  { id: "config", header: "Config", mono: true, cell: (r) => <span title={r.config_id}>{shortId(r.config_id)}</span> },
  { id: "node", header: "Node", mono: true, cell: (r) => <span title={r.node_id}>{shortId(r.node_id)}</span> },
  { id: "strategy", header: "Strategy", cell: (r) => r.strategy },
  { id: "rep", header: "Rep", align: "right", mono: true, cell: (r) => r.repetition },
  {
    id: "state",
    header: "State",
    cell: (r) => (
      <span className="inline-flex flex-col">
        <Badge tone={runTone(r.state)}>{r.state}</Badge>
        {r.detail != null && <span className="text-2xs text-fg-muted">{r.detail}</span>}
      </span>
    ),
  },
  { id: "tries", header: "Tries", align: "right", mono: true, cell: (r) => r.tries },
  {
    id: "failure",
    header: "Last failure",
    className: "max-w-64 break-words",
    cell: (r) => (r.last_failure != null ? <span className="text-xs text-status-failed">{r.last_failure}</span> : "—"),
  },
  {
    id: "job",
    header: "Job",
    mono: true,
    cell: (r) =>
      r.job_id != null ? (
        <Link className="text-accent underline-offset-2 hover:underline" to={`/jobs/${encodeURIComponent(r.job_id)}`}>
          {shortId(r.job_id)}
        </Link>
      ) : (
        "—"
      ),
  },
];

/** Runs in execution order, keyset-paged by seq (`after`), with a state filter. */
export function RunsTab({ campaignId, runCount }: { campaignId: string; runCount: number }) {
  const [state, setState] = useState<RunState | null>(null);
  const filterId = useId();
  const runs = useInfiniteQuery({
    queryKey: benchKeys.runs(campaignId, state),
    queryFn: ({ pageParam, signal }) => listRuns(campaignId, pageParam, state, signal),
    initialPageParam: 0,
    getNextPageParam: (last: RunList) => last.next_after ?? undefined,
  });
  const rows = runs.data?.pages.flatMap((p) => p.runs) ?? [];
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={filterId} className="text-sm text-fg-muted">
          State
        </label>
        <select
          id={filterId}
          className="h-8 rounded-md border border-border-strong bg-surface px-2 text-sm text-fg"
          value={state ?? ""}
          onChange={(e) => {
            const v = e.target.value;
            setState((RUN_STATES as readonly string[]).includes(v) ? (v as RunState) : null);
          }}
        >
          <option value="">all</option>
          {RUN_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className="text-sm text-fg-muted" data-testid="runs-shown">
          {`${String(rows.length)} shown${state === null ? ` of ${String(runCount)}` : ""}`}
        </span>
      </div>
      {runs.isError ? (
        <ErrorState title="Could not load runs" error={runs.error} onRetry={() => void runs.refetch()} />
      ) : runs.isPending ? (
        <p role="status" className="text-sm text-fg-muted">
          Loading runs…
        </p>
      ) : (
        <Table
          caption="Campaign runs in execution order"
          columns={COLUMNS}
          rows={rows}
          rowKey={(r) => r.run_id}
          empty={state === null ? "This campaign has no runs." : `No ${state} runs.`}
        />
      )}
      {runs.hasNextPage && (
        <div>
          <Button
            loading={runs.isFetchingNextPage}
            onClick={() => {
              void runs.fetchNextPage();
            }}
          >
            Load more runs
          </Button>
        </div>
      )}
    </div>
  );
}
