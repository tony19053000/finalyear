import { useRef } from "react";

import { ApiError } from "../../api/errors";
import { EmptyState, ErrorState } from "../../components/States";
import { shortId } from "../../lib/format";
import type { GroupLabel, LabelsOut } from "./benchApi";
import { useLabels } from "./benchQueries";
import { Figure, Light, Note } from "./benchUi";
import { TH, useNodeNames, useScrolled } from "./benchShared";
import { formatMargin, labelLight, optionText, plannerAgreement } from "./campaignView";
import { formatParams } from "./chartData";

function bestConfig(g: GroupLabel, nameOf: (id: string) => string): string {
  if (g.best_config_id == null) return g.label === "tie" ? `tie: ${String(g.tied_config_ids.length)} configs` : "—";
  return optionText(g.best_strategy, g.best_node_id == null ? "?" : nameOf(g.best_node_id));
}

function agrees(value: boolean | null | undefined): string {
  return value === true ? "yes" : value === false ? "no" : "—";
}

const HEADERS = ["Family", "Parameters", "Condition", "Label", "Best config", "Margin", "Planner agrees"] as const;

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

function LabelsTable({ groups }: { groups: readonly GroupLabel[] }) {
  const nameOf = useNodeNames();
  const scroller = useRef<HTMLDivElement>(null);
  const scrolled = useScrolled(scroller);
  return (
    <section aria-label="Labels" className="ov-card ov-frost ov-table-fit nd-table bm-labels overflow-hidden pb-3">
      <div ref={scroller} className="jb-scroll bm-tab-scroll overflow-auto" data-scrolled={scrolled ? "true" : "false"}>
        <table className="w-full border-collapse">
          <caption className="sr-only">Per-group labels</caption>
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
            {groups.map((g) => (
              <tr key={g.group_id}>
                <td>
                  <span className="flex flex-col">
                    <span className="font-medium text-fg">{g.family}</span>
                    <span className="font-mono text-2xs text-fg-muted" title={g.group_id}>
                      {shortId(g.group_id)}
                    </span>
                  </span>
                </td>
                <td className="font-mono text-[0.8125rem] text-fg-muted">{formatParams(g.params)}</td>
                <td className="font-mono text-[0.8125rem]">{g.condition}</td>
                <td>
                  <span className="flex flex-col">
                    <span className="inline-flex items-center gap-2.5">
                      <Light className={labelLight(g.label)} small />
                      <span>{g.label}</span>
                    </span>
                    {g.reason != null && <span className="mt-1 text-2xs text-fg-muted">{g.reason}</span>}
                  </span>
                </td>
                <td>
                  <span className="text-[0.875rem]" title={g.best_config_id ?? undefined}>
                    {bestConfig(g, nameOf)}
                  </span>
                </td>
                <td className="font-mono text-[0.8125rem] tabular-nums">{formatMargin(g.margin_pct)}</td>
                <td>
                  <span
                    className={g.planner_agrees === false ? "text-status-degraded" : undefined}
                    title={g.planner_node_id != null ? `planner prefers ${nameOf(g.planner_node_id)}` : undefined}
                  >
                    {agrees(g.planner_agrees)}
                  </span>
                </td>
              </tr>
            ))}
            {groups.length === 0 && (
              <tr>
                <td colSpan={HEADERS.length} className="text-center text-fg-muted">
                  No groups yet: labels appear once the campaign has run records.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
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
  const planner = plannerAgreement(data);
  return (
    <div className="flex flex-col gap-8">
      <dl className="nd-stagger grid grid-cols-5 gap-x-12 px-2">
        <Figure label="Groups" value={String(data.groups.length)} sub={`from ${String(data.rows)} run records`} />
        <Figure label="Best" value={String(data.counts.best)} sub="a clear winner" />
        <Figure label="Tie" value={String(data.counts.tie)} sub="overlapping CIs" />
        <Figure label="Open" value={String(data.counts.insufficient + data.counts.pending)} sub={`${String(data.counts.insufficient)} insufficient · ${String(data.counts.pending)} pending`} />
        <Figure
          label="Planner agrees"
          value={planner.judged === 0 ? "—" : String(Math.round((planner.agrees * 100) / planner.judged))}
          unit="%"
          sub={planner.judged === 0 ? "no clear best yet" : `${String(planner.agrees)} of ${String(planner.judged)} groups`}
        />
      </dl>
      <div className="px-2">
        <LabelsSummary labels={data} />
      </div>
      <LabelsTable groups={data.groups} />
      <Note caps={false} className="max-w-5xl px-2 leading-5">
        best = the lowest-median configuration&apos;s CI (total time) lies strictly below every other configuration&apos;s
        CI; tie = the CIs overlap; insufficient = a configuration has fewer than 3 measured repetitions; pending = runs of
        the group are still open. Margin = how much higher the runner-up&apos;s median is, in % of the best&apos;s. Planner
        agrees = the planner&apos;s preferred node for the group is the best one.
      </Note>
    </div>
  );
}
