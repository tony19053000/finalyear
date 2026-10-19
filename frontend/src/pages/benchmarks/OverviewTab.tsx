import type { ReactNode } from "react";

import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { Meter } from "../../components/Meter";
import { formatTime } from "../../components/jobs/format";
import type { CampaignDetail, CampaignStatus } from "./benchApi";
import { RUN_STATES } from "./benchApi";
import { formatDonePercent, formatEta, formatFraction, specBudget } from "./campaignView";

function Field({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">{label}</dt>
      <dd className="min-w-0 text-sm break-words" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

const grid = "grid grid-cols-[repeat(auto-fill,minmax(12rem,1fr))] gap-x-6 gap-y-3";

function seconds(value: number | null | undefined): string {
  return value == null ? "—" : `${String(value)} s`;
}

/** Overview: run counts, progress, ETA, failure numbers, budget, throttle, how it finished, the spec. */
export function OverviewTab({ campaign, status }: { campaign: CampaignDetail; status: CampaignStatus | undefined }) {
  const budget = specBudget(campaign.spec);
  const counts = status?.counts ?? campaign.counts;
  return (
    <div className="flex flex-col gap-4">
      {status?.runner_degraded === true && (
        <p role="alert" className="rounded-md border border-status-degraded bg-status-degraded-soft px-3 py-2 text-sm text-fg">
          The controller&apos;s benchmark runner is degraded (its last ticks crashed); it retries with backoff. See the
          controller log (<code className="font-mono">controller.bench.runner</code>).
        </p>
      )}
      <Card title="Progress">
        <dl className={grid}>
          {RUN_STATES.map((state) => (
            <Field key={state} label={state} testId={`count-${state}`}>
              <span className="font-mono tabular-nums">{counts[state]}</span>
            </Field>
          ))}
          <Field label="Done" testId="overview-done">
            <span className="flex flex-col gap-1">
              <span className="tabular-nums">{formatDonePercent(status?.done_percent)}</span>
              <Meter percent={status?.done_percent ?? null} className="max-w-40" />
            </span>
          </Field>
          <Field label="ETA" testId="overview-eta">
            {formatEta(status?.eta_s)}
          </Field>
          <Field label="Median run">{status?.median_run_s == null ? "—" : `${status.median_run_s.toFixed(1)} s`}</Field>
        </dl>
      </Card>
      <Card title="Failures">
        <dl className={grid}>
          <Field label="Infra failure rate (owner pre-emptions excluded)" testId="overview-infra-rate">
            {formatFraction(status?.infra_failure_rate)}
          </Field>
          <Field label="Run records">{status?.records ?? "—"}</Field>
          <Field label="Infra failures">{status?.infra_failures ?? "—"}</Field>
          <Field label="Stopped by owner (requeued)">{status?.preempted ?? "—"}</Field>
          <Field label="Infra retries spent">{status?.retries ?? "—"}</Field>
        </dl>
      </Card>
      <Card title="Budget and throttle">
        <dl className={grid}>
          <Field label="Planned runs">{campaign.run_count}</Field>
          <Field label="Estimated">{`${campaign.summary.estimated_hours.toFixed(1)} h`}</Field>
          <Field label="Budget (spec)" testId="overview-budget">
            {budget === null
              ? "see the spec below"
              : [
                  budget.maxRuns !== null ? `max ${String(budget.maxRuns)} runs` : null,
                  budget.maxHours !== null ? `max ${String(budget.maxHours)} h` : null,
                ]
                  .filter((x) => x !== null)
                  .join(", ")}
          </Field>
          <Field label="Per node at once">{status?.max_concurrent_per_node ?? "—"}</Field>
          <Field label="Cool-down">{seconds(status?.cooldown_s)}</Field>
          <Field label="Infra retries">{status?.max_infra_retries ?? "—"}</Field>
          <Field label="Started">
            {status === undefined ? "—" : status.started_at != null ? formatTime(status.started_at) : "not started (manual submit)"}
          </Field>
          <Field label="Finished" testId="overview-finished">
            {status?.finished_reason ?? (campaign.state === "done" || campaign.state === "cancelled" ? campaign.state : "not finished")}
          </Field>
        </dl>
      </Card>
      <Card title="Campaign">
        <dl className={grid}>
          <Field label="Groups / configs">{`${String(campaign.summary.groups)} / ${String(campaign.summary.configs)}`}</Field>
          <Field label="Dropped / duplicates">{`${String(campaign.summary.dropped)} / ${String(campaign.summary.duplicates)}`}</Field>
          <Field label="Unsupported (skipped)">{campaign.summary.unsupported}</Field>
          <Field label="Session label">{campaign.summary.session_label ?? "—"}</Field>
          <Field label="Seed">
            <span className="font-mono">{campaign.seed}</span>
          </Field>
          <Field label="Created">
            <span className="font-mono text-xs">{formatTime(campaign.created_at)}</span>
          </Field>
          <Field label="Nodes">
            <ul>
              {campaign.summary.nodes.map((n) => (
                <li key={n} className="font-mono text-xs">
                  {n}
                </li>
              ))}
            </ul>
          </Field>
          <Field label="Spec digest">
            <span className="font-mono text-2xs break-all">{campaign.spec_digest}</span>
          </Field>
        </dl>
        <CodeBlock className="mt-4" label="campaign spec (YAML)" code={campaign.spec} />
      </Card>
    </div>
  );
}
