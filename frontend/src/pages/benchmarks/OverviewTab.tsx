/**
 * Overview: progress (the big light percent over the honey bar, the run
 * counts), the campaign's result (its best measured speed-up as comparison
 * bars, read from the labels), failure numbers, budget and throttle, how it
 * finished, the spec. Open figures and facts; frosted cards; no boxes inside.
 */
import { useId } from "react";

import { formatMs } from "../../lib/format";
import type { CampaignDetail, CampaignStatus } from "./benchApi";
import { RUN_STATES } from "./benchApi";
import { CompareBars, type CompareBar } from "./BenchCharts";
import { useLabels } from "./benchQueries";
import { Fact, FrostCard, Note } from "./benchUi";
import { useNodeNames } from "./benchShared";
import {
  bestSpeedUp,
  formatDonePercent,
  formatEta,
  formatFactor,
  formatFraction,
  specBudget,
  strategyText,
} from "./campaignView";
import { formatParams } from "./chartData";

function seconds(value: number | null | undefined): string {
  return value == null ? "—" : `${String(value)} s`;
}

const BAR_H = 8;

/** Frame B's thick bar: a deep→honey fill over the hatched track (SVG width attribute: no inline style). */
function HoneyBar({ percent }: { percent: number | null }) {
  const p = percent === null ? 0 : Math.max(0, Math.min(100, percent));
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <div role="progressbar" aria-label="Campaign progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent === null ? undefined : Math.round(p)}>
      <svg aria-hidden="true" className="ov-hatch block h-[8px] w-full rounded-full">
        <defs>
          <linearGradient id={`bmfill${id}`} x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" className="ov-stop-deep" />
            <stop offset="1" className="ov-stop-honey" />
          </linearGradient>
        </defs>
        {p > 0 && (
          <svg x="0" y="0" width={`${String(p)}%`} height={BAR_H} overflow="visible">
            <rect className="ov-bar-fill" x="0" y="0" width="100%" height={BAR_H} rx={BAR_H / 2} fill={`url(#bmfill${id})`} />
          </svg>
        )}
      </svg>
    </div>
  );
}

const COUNT_LABEL: Record<string, string> = { planned: "Planned", submitted: "Submitted", done: "Done", failed: "Failed", skipped: "Skipped" };

function Progress({ campaign, status }: { campaign: CampaignDetail; status: CampaignStatus | undefined }) {
  const counts = status?.counts ?? campaign.counts;
  const pct = formatDonePercent(status?.done_percent);
  const number = pct.endsWith(" %") ? pct.slice(0, -2) : pct;
  return (
    <section aria-label="Progress" className="ov-card ov-frost nd-glass flex flex-col gap-7 px-10 pt-7 pb-8">
      <div className="flex flex-col gap-4">
        <div className="flex items-end justify-between gap-6">
          <p className="ov-figure flex items-baseline text-[3.5rem] font-light text-fg" data-testid="overview-done">
            {number}
            {pct.endsWith(" %") && <span className="ml-1.5 text-xl font-light text-fg-muted">{" %"}</span>}
          </p>
          <dl className="mb-2 grid grid-cols-2 gap-x-12 text-right">
            <div>
              <dt className="nd-label">ETA</dt>
              <dd className="mt-2 text-[0.9375rem] text-fg tabular-nums" data-testid="overview-eta">
                {formatEta(status?.eta_s)}
              </dd>
            </div>
            <div>
              <dt className="nd-label">Median run</dt>
              <dd className="mt-2 text-[0.9375rem] text-fg tabular-nums">{status?.median_run_s == null ? "—" : `${status.median_run_s.toFixed(1)} s`}</dd>
            </div>
          </dl>
        </div>
        <HoneyBar percent={status?.done_percent ?? null} />
      </div>
      <dl className="grid grid-cols-5 gap-x-10">
        {RUN_STATES.map((state) => (
          <div key={state} className="min-w-0">
            <dt className="nd-label">{COUNT_LABEL[state] ?? state}</dt>
            <dd
              className={`ov-figure mt-2 text-[1.75rem] font-light ${state === "failed" && counts.failed > 0 ? "text-status-failed" : "text-fg"}`}
              data-testid={`count-${state}`}
            >
              {counts[state]}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** The campaign's best measured speed-up: the local baseline vs every option of that group. */
function Result({ campaignId }: { campaignId: string }) {
  const labels = useLabels(campaignId);
  const nameOf = useNodeNames();
  if (labels.data === undefined) return null;
  const speed = bestSpeedUp(labels.data);
  if (speed === null) return null;
  const group = labels.data.groups.find((g) => g.group_id === speed.groupId);
  const bars: CompareBar[] = (group?.configs ?? [])
    .filter((c) => c.state === "measured" && c.total_ms != null)
    .sort((a, b) => (a.total_ms?.median ?? 0) - (b.total_ms?.median ?? 0))
    .map((c) => ({
      key: c.config_id,
      label: nameOf(c.node_id),
      sub: c.config_id === speed.baseline.config_id ? `${strategyText(c.strategy)} · baseline` : strategyText(c.strategy),
      value: c.total_ms?.median ?? 0,
      ciLow: c.total_ms?.ci_low,
      ciHigh: c.total_ms?.ci_high,
      tone: c.config_id === speed.best.config_id ? "hive" : c.config_id === speed.baseline.config_id ? "base" : "other",
    }));
  return (
    <section aria-label="Result" className="grid grid-cols-[minmax(0,16rem)_minmax(0,1fr)] items-center gap-x-14 py-6" data-testid="overview-result">
      <div className="flex flex-col gap-3">
        <p className="nd-label">Best speed-up</p>
        <p className="ov-figure flex items-baseline text-[4.25rem] font-light text-[#ffc93c]" data-testid="overview-speedup">
          {formatFactor(speed.factor)}
          <span className="ml-1 text-2xl font-light text-[#ffc93c]/70">×</span>
        </p>
        <p className="text-sm text-fg-muted">
          {speed.family} · {formatParams(speed.params)}
        </p>
        <Note className="mt-1">{labels.data.campaign_state === "done" ? "server medians" : "so far · server medians"}</Note>
      </div>
      <CompareBars bars={bars} format={(v) => formatMs(v)} label={`Median total time per option, ${speed.family}`} />
    </section>
  );
}

const grid = "grid gap-x-10 gap-y-8";

/** Overview: run counts, progress, ETA, failure numbers, budget, throttle, how it finished, the spec. */
export function OverviewTab({ campaign, status }: { campaign: CampaignDetail; status: CampaignStatus | undefined }) {
  const budget = specBudget(campaign.spec);
  return (
    <div className="bm-ov nd-stagger flex flex-col gap-12">
      {status?.runner_degraded === true && (
        <p role="alert" className="text-sm text-status-degraded">
          The controller&apos;s benchmark runner is degraded (its last ticks crashed); it retries with backoff. See the
          controller log (<code className="font-mono">controller.bench.runner</code>).
        </p>
      )}
      <Progress campaign={campaign} status={status} />
      <Result campaignId={campaign.campaign_id} />
      <div className="bm-pair grid grid-cols-2">
        <FrostCard title="Failures" className="bm-plain pr-12">
          <dl className={`${grid} grid-cols-3 px-10 pt-6 pb-9`}>
            <Fact label="Infra failure rate" testId="overview-infra-rate">
              {formatFraction(status?.infra_failure_rate)}
            </Fact>
            <Fact label="Infra failures">{status?.infra_failures ?? "—"}</Fact>
            <Fact label="Infra retries spent">{status?.retries ?? "—"}</Fact>
            <Fact label="Run records">{status?.records ?? "—"}</Fact>
            <Fact label="Stopped by owner">
              {status?.preempted ?? "—"}
              <span className="block font-mono text-2xs text-fg-muted">requeued</span>
            </Fact>
          </dl>
          <Note caps={false} className="px-10 pb-8">The infra failure rate excludes owner pre-emptions.</Note>
        </FrostCard>
        <FrostCard title="Budget and throttle" className="bm-plain bm-plain-divided pl-12">
          <dl className={`${grid} grid-cols-3 px-10 pt-6 pb-9`}>
            <Fact label="Planned runs">{campaign.run_count}</Fact>
            <Fact label="Estimated">{`${campaign.summary.estimated_hours.toFixed(1)} h`}</Fact>
            <Fact label="Budget (spec)" testId="overview-budget">
              {budget === null
                ? "see the spec below"
                : [
                    budget.maxRuns !== null ? `max ${String(budget.maxRuns)} runs` : null,
                    budget.maxHours !== null ? `max ${String(budget.maxHours)} h` : null,
                  ]
                    .filter((x) => x !== null)
                    .join(", ")}
            </Fact>
            <Fact label="Per node at once">{status?.max_concurrent_per_node ?? "—"}</Fact>
            <Fact label="Cool-down">{seconds(status?.cooldown_s)}</Fact>
            <Fact label="Infra retries">{status?.max_infra_retries ?? "—"}</Fact>
            <Fact label="Finished" testId="overview-finished">
              {status?.finished_reason ?? (campaign.state === "done" || campaign.state === "cancelled" ? campaign.state : "not finished")}
            </Fact>
          </dl>
        </FrostCard>
      </div>
      <FrostCard
        title="Spec"
        className="nd-glass"
        meta={
          <Note caps={false}>
            {`${String(campaign.summary.dropped)} dropped · ${String(campaign.summary.duplicates)} duplicates · ${String(campaign.summary.unsupported)} unsupported (skipped)`}
          </Note>
        }
      >
        <div className="px-10 pt-5 pb-9">
          <pre aria-label="campaign spec (YAML)" tabIndex={0} className="jb-log max-h-[28rem] text-fg">
            <code>{campaign.spec}</code>
          </pre>
        </div>
      </FrostCard>
    </div>
  );
}
