import { useInfiniteQuery } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ErrorState } from "../../components/States";
import { ListboxField, type ListboxOption } from "../../components/jobs/ListboxField";
import { shortId } from "../../lib/format";
import { RUN_STATES, listRuns, type RunList, type RunState } from "./benchApi";
import { benchKeys } from "./benchKeys";
import { Light, Note } from "./benchUi";
import { TH, dash, useNodeNames, useScrolled } from "./benchShared";
import { runLight, strategyText } from "./campaignView";

const HEADERS = ["#", "Group", "Config", "Node", "Strategy", "Rep", "State", "Tries", "Last failure", "Job"] as const;

const STATE_OPTIONS: ListboxOption[] = [{ value: "", label: "all" }, ...RUN_STATES.map((s) => ({ value: s, label: s }))];

/** Runs in execution order, keyset-paged by seq (`after`), with a state filter, in the frosted table. */
export function RunsTab({ campaignId, runCount }: { campaignId: string; runCount: number }) {
  const [state, setState] = useState<RunState | null>(null);
  const filterId = useId();
  const nameOf = useNodeNames();
  const scroller = useRef<HTMLDivElement>(null);
  const scrolled = useScrolled(scroller);
  const runs = useInfiniteQuery({
    queryKey: benchKeys.runs(campaignId, state),
    queryFn: ({ pageParam, signal }) => listRuns(campaignId, pageParam, state, signal),
    initialPageParam: 0,
    getNextPageParam: (last: RunList) => last.next_after ?? undefined,
  });
  const rows = runs.data?.pages.flatMap((p) => p.runs) ?? [];
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-6 px-2">
        <div className="flex w-56 flex-col gap-1.5">
          <label htmlFor={filterId} className="nd-label">
            State
          </label>
          <ListboxField
            id={filterId}
            value={state ?? ""}
            options={STATE_OPTIONS}
            onChange={(v) => {
              setState((RUN_STATES as readonly string[]).includes(v) ? (v as RunState) : null);
            }}
          />
        </div>
        <Note testId="runs-shown" className="pb-2">
          {`${String(rows.length)} shown${state === null ? ` of ${String(runCount)}` : ""}`}
        </Note>
      </div>
      {runs.isError ? (
        <ErrorState title="Could not load runs" error={runs.error} onRetry={() => void runs.refetch()} />
      ) : runs.isPending ? (
        <p role="status" className="px-2 text-sm text-fg-muted">
          Loading runs…
        </p>
      ) : (
        <section aria-label="Runs" className="ov-card ov-frost ov-table-fit nd-table bm-runs overflow-hidden pb-3">
          <div ref={scroller} className="jb-scroll bm-tab-scroll overflow-auto" data-scrolled={scrolled ? "true" : "false"}>
            <table className="w-full border-collapse">
              <caption className="sr-only">Campaign runs in execution order</caption>
              <thead>
                <tr>
                  {HEADERS.map((h) => (
                    <th key={h} scope="col" className={TH}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.run_id}>
                    <td className="font-mono text-[0.8125rem] text-fg-subtle tabular-nums">{r.seq}</td>
                    <td className="font-mono text-[0.8125rem]">
                      <span title={r.group_id}>
                        {shortId(r.group_id)} <span className="font-sans text-fg-muted">{r.family}</span>
                      </span>
                    </td>
                    <td className="font-mono text-[0.8125rem] text-fg-muted">
                      <span title={r.config_id}>{shortId(r.config_id)}</span>
                    </td>
                    <td>
                      <span className="block truncate" title={r.node_set != null && r.node_set.length > 0 ? r.node_set.map(nameOf).join(", ") : r.node_id}>
                        {nameOf(r.node_id)}
                      </span>
                    </td>
                    <td>
                      <span className="jb-tag text-fg-muted">{strategyText(r.strategy)}</span>
                    </td>
                    <td className="font-mono text-[0.8125rem] tabular-nums">{r.repetition}</td>
                    <td>
                      <span className="inline-flex flex-col">
                        <span className="inline-flex items-center gap-2.5">
                          <Light className={runLight(r.state)} small />
                          <span>{r.state}</span>
                        </span>
                        {r.detail != null && <span className="mt-1 text-2xs text-fg-muted">{r.detail}</span>}
                      </span>
                    </td>
                    <td className="font-mono text-[0.8125rem] tabular-nums">{r.tries}</td>
                    <td className="break-words">{r.last_failure != null ? <span className="text-xs text-status-failed">{r.last_failure}</span> : dash}</td>
                    <td className="font-mono text-[0.8125rem]">
                      {r.job_id != null ? (
                        <Link className="text-fg-muted underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline" to={`/jobs/${encodeURIComponent(r.job_id)}`}>
                          {shortId(r.job_id)}
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={HEADERS.length} className="text-center text-fg-muted">
                      {state === null ? "This campaign has no runs." : `No ${state} runs.`}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {runs.hasNextPage && (
        <div className="flex justify-end px-2">
          <button
            type="button"
            className="jb-quiet-btn"
            disabled={runs.isFetchingNextPage}
            aria-busy={runs.isFetchingNextPage || undefined}
            onClick={() => {
              void runs.fetchNextPage();
            }}
          >
            Load more runs
          </button>
        </div>
      )}
    </div>
  );
}
