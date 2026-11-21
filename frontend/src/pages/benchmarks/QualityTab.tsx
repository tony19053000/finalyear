/**
 * Quality (D220, G5c's report): the campaign summary with the criterion
 * verdicts, then every configuration - flagged ones (CV over the threshold,
 * fewer than 3 repetitions, missing fields) first. Every record bucket of
 * the summary is shown with its total, so a reader can check they add up.
 * Read through `qualityApi.ts`; a controller without the route says so
 * (never numbers it did not send).
 *
 * Layout: the verdicts as an open facts strip, the record buckets as a
 * hairline list in a frosted card, the configurations in the frosted table;
 * flags are plain coloured words, never badges.
 */
import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";

import { EmptyState, ErrorState } from "../../components/States";
import { cn } from "../../lib/cn";
import { formatMs, shortId } from "../../lib/format";
import { benchKeys } from "./benchKeys";
import { Fact, FrostCard, Light, Note } from "./benchUi";
import { TH, dash, useNodeNames, useScrolled } from "./benchShared";
import { formatFraction, strategyText } from "./campaignView";
import { flaggedFirst, getQuality, missingFields, recordBuckets, type QualityMetric, type QualityReport } from "./qualityApi";

function cv(metric: QualityMetric | null): string {
  return metric?.cv == null ? "—" : `${(metric.cv * 100).toFixed(1)} %`;
}

function ci(metric: QualityMetric | null, format: (v: number) => string): string {
  if (metric === null) return "—";
  return `${format(metric.ci_low)} – ${format(metric.ci_high)}`;
}

function ns(value: number): string {
  return `${value.toPrecision(3)} ns`;
}

function verdict(value: boolean | null, pending: boolean): { text: string; light: string } {
  if (value === null) return { text: pending ? "pending" : "not reported", light: "bg-fg-subtle" };
  return value ? { text: pending ? "ok so far" : "met", light: "bg-status-ok" } : { text: "not met", light: "bg-status-failed" };
}

function Verdict({ value, pending, testId }: { value: boolean | null; pending: boolean; testId: string }) {
  const v = verdict(value, pending);
  return (
    <span className="inline-flex items-center gap-2.5">
      <Light className={v.light} />
      <span data-testid={testId}>{v.text}</span>
    </span>
  );
}

function countList(counts: Readonly<Record<string, number>>): string {
  const entries = Object.entries(counts).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return entries.length === 0 ? "none" : entries.map(([k, v]) => `${k}: ${String(v)}`).join(", ");
}

const HEADERS = ["Group / config", "Runs", "n", "Median total", "CV", "CI (total)", "Per iteration", "Outliers", "Flags", "Missing fields"] as const;

function Configs({ report }: { report: QualityReport }) {
  const nameOf = useNodeNames();
  const scroller = useRef<HTMLDivElement>(null);
  const scrolled = useScrolled(scroller);
  const s = report.summary;
  const rows = flaggedFirst(report.configs);
  return (
    <FrostCard
      title="Configurations"
      meta={<Note>{`${String(s.configs)} configurations · ${String(s.flagged_configs)} flagged`}</Note>}
      className="nd-glass ov-table-fit nd-table bm-quality bm-q2 overflow-hidden pb-3"
    >
      <div ref={scroller} className="jb-scroll bm-tab-scroll overflow-auto" data-scrolled={scrolled ? "true" : "false"}>
        <table className="w-full border-collapse">
          <caption className="sr-only">Data quality per configuration, flagged first</caption>
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
            {rows.map((c) => {
              const missing = Object.keys(missingFields(c)).length > 0;
              return (
                <tr key={`${c.group_id}|${c.config_id}`} className="align-top">
                  <td className="font-mono text-[0.8125rem]">
                    <span className="flex flex-col">
                      <span title={c.group_id}>{shortId(c.group_id)}</span>
                      <span className="text-fg-muted" title={c.config_id}>
                        {shortId(c.config_id)}
                      </span>
                    </span>
                  </td>
                  <td>
                    <span className="flex flex-col text-[0.8125rem]">
                      <span>
                        {c.family} · {strategyText(c.strategy)} · <span title={c.node_id}>{nameOf(c.node_id)}</span>
                      </span>
                      <span className="text-fg-muted">{`condition ${c.condition}`}</span>
                      <span className="text-fg-muted">{`${String(c.done)} / ${String(c.planned)} done · ${String(c.records_checked)} records`}</span>
                    </span>
                  </td>
                  <td className="font-mono text-[0.8125rem] tabular-nums">{c.n}</td>
                  <td className="font-mono text-[0.8125rem] tabular-nums">{formatMs(c.total_ms?.median ?? null)}</td>
                  <td className={cn("font-mono text-[0.8125rem] tabular-nums", c.cv_flag && "text-status-degraded")}>{cv(c.total_ms)}</td>
                  <td className="font-mono text-[0.8125rem] text-fg-muted tabular-nums">{ci(c.total_ms, formatMs)}</td>
                  <td className="font-mono text-[0.8125rem] text-fg-muted tabular-nums">
                    {c.compute_per_iter_ns !== null ? `${ns(c.compute_per_iter_ns.median)} (CV ${cv(c.compute_per_iter_ns)})` : "—"}
                  </td>
                  <td className="font-mono text-[0.8125rem] tabular-nums">
                    <span title={c.outlier_attempt_ids.join(", ") || undefined}>{c.outlier_attempt_ids.length}</span>
                  </td>
                  <td>
                    <span className="flex flex-col gap-1">
                      {c.state === "unsupported" && <span className="jb-tag text-fg-muted">unsupported</span>}
                      {c.cv_flag && <span className="jb-tag text-status-degraded">high CV</span>}
                      {c.reps_flag && <span className="jb-tag text-status-failed">n &lt; 3</span>}
                      {missing && <span className="jb-tag text-status-degraded">missing fields</span>}
                      {!c.flagged && c.state !== "unsupported" && dash}
                    </span>
                  </td>
                  <td className="text-[0.8125rem] text-fg-muted">{countList(missingFields(c)).replace(/^none$/, "—")}</td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={HEADERS.length} className="text-center text-fg-muted">
                  No configurations in the report.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </FrostCard>
  );
}

/** Every record bucket, their sum and `records`: the reader can check they add up. */
function Records({ report }: { report: QualityReport }) {
  const s = report.summary;
  const buckets = recordBuckets(s);
  const total = buckets.reduce((a, b) => a + b.count, 0);
  return (
    <FrostCard title="Run records" className="bm-plain bm-q-records">
      <div className="px-10 pt-5 pb-8">
        <table className="w-full border-collapse" aria-label="Run records by bucket">
          <tbody>
            {buckets.map((b) => (
              <tr key={b.id} className="nd-hair-b">
                <th scope="row" className="py-3 pr-6 text-left text-[0.9375rem] font-normal text-fg">
                  {b.label}
                  {b.by !== undefined && b.count > 0 && <span className="mt-0.5 block font-mono text-xs text-fg-muted">{countList(b.by)}</span>}
                </th>
                <td
                  className={cn("py-3 text-right align-top font-mono text-[0.9375rem] tabular-nums", b.count === 0 ? "text-fg-muted" : "text-fg")}
                  data-testid={`quality-bucket-${b.id}`}
                >
                  {b.count}
                </td>
              </tr>
            ))}
            <tr>
              <th scope="row" className="pt-4 pr-6 text-left text-[0.9375rem] font-medium text-fg">
                Records (fillers and warm-ups excluded)
              </th>
              <td className="pt-4 text-right font-mono text-[0.9375rem] font-medium tabular-nums" data-testid="quality-records">
                {s.records}
              </td>
            </tr>
          </tbody>
        </table>
        {total !== s.records && (
          <p role="alert" className="mt-4 text-sm text-status-failed">
            {`The buckets add up to ${String(total)}, not ${String(s.records)}: this report is inconsistent.`}
          </p>
        )}
        <p className="mt-5 font-mono text-[0.6875rem] tracking-[0.04em] text-fg-muted" data-testid="quality-fillers">
          {`Fillers / warm-ups, never in any statistic: ${countList(s.bench_role_records)}`}
        </p>
      </div>
    </FrostCard>
  );
}

function Report({ report }: { report: QualityReport }) {
  const s = report.summary;
  return (
    <div className="bm-ov nd-stagger flex flex-col gap-12">
      {s.pending && (
        <p role="status" className="px-2 text-sm text-fg-muted">
          The campaign is still running: these numbers and verdicts are provisional.
        </p>
      )}
      <section aria-label="Summary">
        <dl className="nd-hair-y grid grid-cols-4 gap-x-10 gap-y-8 px-2 py-8">
          <Fact label="Criterion 1: infra failures < 2 %">
            <Verdict value={s.crit1_infra_ok} pending={s.pending} testId="quality-crit1" />
          </Fact>
          <Fact label="Criterion 3: every config n ≥ 3 with CV">
            <Verdict value={s.crit3_reps_ok} pending={s.pending} testId="quality-crit3" />
          </Fact>
          <Fact label="Infra failure rate (owner pre-emptions excluded)" testId="quality-infra-rate">
            {formatFraction(s.infra_failure_rate)}
          </Fact>
          <Fact label="CV threshold">{`${(s.cv_threshold * 100).toFixed(0)} %`}</Fact>
          <Fact label="Campaign state" testId="quality-campaign-state">
            {s.campaign_state}
          </Fact>
          <Fact label="Runs by state" className="col-span-3" testId="quality-runs-by-state">
            <span className="font-mono text-[0.8125rem]">{countList(s.runs_by_state)}</span>
          </Fact>
        </dl>
      </section>
      <Records report={report} />
      <Configs report={report} />
    </div>
  );
}

export function QualityTab({ campaignId }: { campaignId: string }) {
  const quality = useQuery({
    queryKey: benchKeys.quality(campaignId),
    queryFn: ({ signal }) => getQuality(campaignId, signal),
  });
  if (quality.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the quality report…
      </p>
    );
  }
  if (quality.isError) {
    return <ErrorState title="Could not load the quality report" error={quality.error} onRetry={() => void quality.refetch()} />;
  }
  const result = quality.data;
  if (result.kind === "unavailable") {
    return (
      <EmptyState title="Quality report not available on this controller yet">
        This controller has no data quality report (<code className="font-mono">GET …/quality</code>, V0.9 roadmap
        0.9.8). Update the controller; the other tabs work without it.
      </EmptyState>
    );
  }
  if (result.kind === "invalid") {
    return (
      <EmptyState title="Quality report could not be read">
        The controller sent a version 1 quality report that does not have the expected fields, so no number is
        shown. Rebuild the dashboard from the same release as the controller.
      </EmptyState>
    );
  }
  if (result.kind === "unsupported") {
    return (
      <EmptyState title="Quality report format not supported">
        The controller sent quality report version {result.version === undefined ? "?" : JSON.stringify(result.version)}; this dashboard reads version 1.
        Rebuild the dashboard from the same release as the controller.
      </EmptyState>
    );
  }
  return <Report report={result.report} />;
}
