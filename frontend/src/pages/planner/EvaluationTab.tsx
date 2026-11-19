/**
 * The Planner's **Evaluation** tab (V0.14 G4 / G5, D300, D318, D322, D378):
 * the newest offline-evaluation reports in a frosted table and, open beside
 * it at the same height, the selected report - criteria 1-3 in three even
 * columns, one row of figures (groups, records, decision latency), what it
 * evaluated and its settings. Below, one frosted card shows one detail at a
 * time behind text tabs: the mean regret per policy (bars with the bootstrap
 * CI, the report's own `settings.ci_level`), per policy, criterion 2's paired
 * comparisons, per family, the calibrations used. A report made with
 * `--allow-leak` says LEAKED: its verdicts are not evidence.
 *
 * A 404 without the `unknown_eval_report` reason means the controller has no
 * evaluator ("not available"); with it, the report id does not exist ("not
 * found", D321). **Run evaluation** (admins, the title row's round button)
 * starts one in the background (202) and selects it; one runs at a time.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useId, useRef, useState, type ReactNode } from "react";

import { describeError } from "../../api/errors";
import { EmptyState, ErrorState } from "../../components/States";
import { formatDayTime, formatTime } from "../../components/jobs/format";
import { useOutsideClose } from "../../components/jobs/useOutsideClose";
import { useToast } from "../../components/toastContext";
import { formatAgo, formatMs, secondsSince, shortId } from "../../lib/format";
import { cn } from "../../lib/cn";
import { type CompareBar } from "../benchmarks/BenchCharts";
import { Fact, Light, TextTabs, type TabItem } from "../benchmarks/benchUi";
import { dash } from "../benchmarks/benchShared";
import {
  EVAL_POLL_MS,
  failedText,
  isDone,
  isEvalAlreadyRunning,
  isEvalReportNotFound,
  isEvalUnavailable,
  parseCampaignIds,
  startEvalReport,
  type CalibrationUsedOut,
  type DoneEvalRecord,
  type EvalReportRecordOut,
  type EvalReportSummaryOut,
  type FamilyResultOut,
  type PairedComparisonOut,
  type PolicyResultOut,
  type VerdictOut,
} from "./plannerApi";
import { plannerKeys, useEvalReport, useEvalReports } from "./plannerQueries";
import { Clip, PlCard, PlFigure, PlNote, PlTable, StatusWord, type PlColumn } from "./plannerUi";
import {
  ciLabel,
  ciText,
  judgedText,
  latencyBuckets,
  latencyText,
  ms,
  pct,
  points,
  pointsCi,
  regretBars,
  toneLight,
  verdictLight,
  verdictText,
} from "./plannerView";

/* ------------------------------------------------------------ list */

function ReportStatus({ status, reason, leaked }: { status: EvalReportSummaryOut["status"]; reason: string | null; leaked: boolean }) {
  return (
    <span className="flex min-w-0 flex-col" title={status === "failed" && reason !== null ? `failed: ${reason}` : undefined}>
      {status === "running" ? (
        <StatusWord light={toneLight("busy")} breathe>
          running…
        </StatusWord>
      ) : status === "failed" ? (
        <StatusWord light={toneLight("failed")}>{`failed${reason !== null ? `: ${reason}` : ""}`}</StatusWord>
      ) : (
        <StatusWord light={toneLight("ok")}>done</StatusWord>
      )}
      {leaked && <span className="pl-4 font-mono text-2xs tracking-[0.12em] text-status-failed uppercase">leaked</span>}
    </span>
  );
}

function reportColumns(selected: number | null, select: (id: number) => void): PlColumn<EvalReportSummaryOut>[] {
  return [
    {
      id: "id",
      header: "Report",
      cell: (r) => (
        <span className="flex flex-col">
          <button
            type="button"
            className={cn(
              "w-fit font-mono text-[0.875rem] underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline",
              r.id === selected ? "font-medium text-[#ffc93c]" : "text-fg",
            )}
            aria-pressed={r.id === selected}
            onClick={() => {
              select(r.id);
            }}
          >
            #{r.id}
          </button>
          <time className="font-mono text-2xs whitespace-nowrap text-fg-muted" dateTime={r.created_at} title={formatTime(r.created_at)}>
            {formatDayTime(r.created_at)}
          </time>
        </span>
      ),
    },
    { id: "status", header: "Status", cell: (r) => <ReportStatus status={r.status} reason={r.reason} leaked={r.leaked} /> },
    {
      id: "campaigns",
      header: "Campaigns",
      cell: (r) => (
        <span className="flex min-w-0 flex-col" title={r.campaign_ids.join("\n")}>
          <span className="truncate font-mono text-[0.8125rem]">{r.campaign_ids.map((c) => shortId(c)).join(", ")}</span>
          <span className="truncate font-mono text-2xs text-fg-muted">{r.holdout === null ? "each family held out" : `held out ${r.holdout}`}</span>
        </span>
      ),
    },
    { id: "groups", header: "Groups", hint: "Groups evaluated (done reports).", mono: true, cell: (r) => (r.status === "done" ? String(r.evaluated_groups) : dash) },
    {
      id: "verdicts",
      header: "Verdicts",
      cell: (r) =>
        r.verdicts.length === 0 ? (
          dash
        ) : (
          <span className="inline-flex items-center gap-3">
            {r.verdicts.map((v) => (
              <span key={v.criterion} className="inline-flex cursor-help items-center gap-1.5 font-mono text-[0.75rem] text-fg-muted" title={verdictText(v)}>
                <Light className={verdictLight(v.status)} small />
                <span aria-hidden="true">{v.criterion}</span>
                <span className="sr-only">{`${verdictText(v)};`}</span>
              </span>
            ))}
          </span>
        ),
    },
  ];
}

/* ------------------------------------------------------------ one report */

function LeakedNote() {
  return (
    <div role="alert" className="flex flex-col gap-1 pb-2">
      <p className="font-mono text-[0.6875rem] font-medium tracking-[0.14em] text-status-failed uppercase">LEAKED</p>
      <p className="text-sm text-fg-muted">
        Made with <code className="font-mono">--allow-leak</code>: its calibrated rows used the live calibration, which may
        have been fitted on these very runs. Its verdicts are not evidence for the success criteria.
      </p>
    </div>
  );
}

/** One criterion: the mono label, the verdict word beside its light, one short line (the detail, full in its tooltip). */
function Criterion({ v }: { v: VerdictOut }) {
  const would = v.status === "leaked" && v.would_be != null ? `would be ${v.would_be}` : null;
  return (
    <li className="flex min-w-0 flex-col">
      <span className="sr-only">{verdictText(v)}</span>
      <span aria-hidden="true" className="nd-label">{`criterion ${String(v.criterion)}`}</span>
      <span aria-hidden="true" className="mt-3 inline-flex min-w-0 items-center gap-3 text-[1.75rem] leading-none font-light tracking-[-0.02em] text-fg">
        <Light className={verdictLight(v.status)} />
        <span className="truncate">{v.status.replaceAll("_", " ")}</span>
        {would !== null && <span className="shrink-0 text-[0.9375rem] text-fg-muted">{would}</span>}
      </span>
      <span className="mt-3 line-clamp-2 cursor-help font-mono text-2xs leading-5 text-fg-muted" title={v.detail}>
        {v.detail}
      </span>
    </li>
  );
}

/** The report's figures in one row: groups, records, then decision latency (criterion 3's numbers). */
function Figures({ record }: { record: DoneEvalRecord }) {
  const r = record.report;
  const c = r.counts;
  const l = r.latency;
  const labels = Object.entries(c.labels)
    .map(([label, n]) => `${label} ${String(n)}`)
    .join(" · ");
  const recordsSub = `no replay ${String(c.no_replay_groups)}${c.non_finite_configs > 0 ? ` · non-finite configurations dropped ${String(c.non_finite_configs)}` : ""}`;
  const buckets = l == null ? [] : latencyBuckets(l);
  return (
    <div className="pl-figs grid grid-cols-5 gap-x-8">
      <dl className="contents">
        <PlFigure
          size="mid"
          label="Groups"
          value={String(c.evaluated_groups)}
          unit={`of ${String(c.groups)}`}
          sub={labels === "" ? `no replay ${String(c.no_replay_groups)}` : labels}
          hint={`${String(c.evaluated_groups)} of ${String(c.groups)} groups evaluated${labels === "" ? "" : ` (${labels})`}; no replay ${String(c.no_replay_groups)}.`}
        />
        <PlFigure
          size="mid"
          label="Records"
          value={c.records_used.toLocaleString()}
          unit={`of ${c.records.toLocaleString()}`}
          sub={recordsSub}
          hint={`${c.records_used.toLocaleString()} of ${c.records.toLocaleString()} recorded runs used; ${recordsSub}.`}
        />
      </dl>
      <div className="contents" data-testid="eval-latency">
        {l != null ? (
          <>
            <span className="sr-only">{latencyText(l)}</span>
            <span className="sr-only">{buckets.join("; ")}</span>
            <span className="sr-only">{judgedText(l)}</span>
            <dl aria-hidden="true" className="contents">
              <PlFigure size="mid" label="Decision p50" value={l.p50_ms == null ? "—" : l.p50_ms.toFixed(1)} unit="ms" sub={`max ${ms(l.max_ms)}`} hint={latencyText(l)} />
              <PlFigure
                size="mid"
                label="Decision p99"
                value={l.p99_ms == null ? "—" : l.p99_ms.toFixed(1)}
                unit="ms"
                sub={buckets.length === 0 ? `budget ${String(l.budget_ms)} ms` : buckets.join(" · ")}
                hint={[judgedText(l), ...buckets].join("\n")}
              />
              <PlFigure
                size="mid"
                label="Over budget"
                value={String(l.over_budget)}
                unit={`of ${String(l.decisions)}`}
                sub={`budget ${String(l.budget_ms)} ms`}
                hint={judgedText(l)}
              />
            </dl>
          </>
        ) : (
          <dl className="col-span-3">
            <PlFigure size="mid" label="Decision latency" value="—" sub="not recorded (report made before decision times were stored)" />
          </dl>
        )}
      </div>
    </div>
  );
}

/** The selected report, open on the page beside the list: criteria 1-3, its figures, what it evaluated and how. */
function Summary({ record }: { record: DoneEvalRecord }) {
  const r = record.report;
  const s = r.settings;
  const ci = ciLabel(s.ci_level);
  const settings =
    `within ${String(s.regret_threshold_pct)} % in ≥ ${String(s.within_target_pct)} % of groups · min ${String(s.min_groups)} groups · ` +
    `${String(s.bootstrap_resamples)} resamples, seed ${String(s.bootstrap_seed)}, ${ci} · judged row ${s.criterion_policy}`;
  return (
    <PlCard
      plain
      title={`Report #${String(record.id)}`}
      hint="How good the planner's choices were: every recorded run replayed under every policy, against the best measured configuration of each group."
      meta={<PlNote>{formatTime(record.created_at)}</PlNote>}
      className="pl-fill flex flex-col"
    >
      <div className="pl-body flex min-h-0 flex-1 flex-col justify-center gap-12">
        {r.leaked && <LeakedNote />}
        <ul aria-label="Verdicts" className="grid grid-cols-3 gap-x-10 pt-5">
          {r.verdicts.map((v) => (
            <Criterion key={v.criterion} v={v} />
          ))}
        </ul>
        <div className="nd-hair-t pt-8">
          <Figures record={record} />
        </div>
        <dl className="nd-hair-t grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,2.6fr)] gap-x-10 pt-6">
          <Fact label="Campaigns">
            <span className="line-clamp-2 cursor-help font-mono text-[0.8125rem] leading-5 break-all" title={r.campaign_ids.join("\n")}>
              {r.campaign_ids.map((id) => shortId(id)).join(", ")}
            </span>
            <span className="sr-only">{r.campaign_ids.join(", ")}</span>
          </Fact>
          <Fact label="Held-out family">
            <span className="block cursor-help truncate font-mono text-[0.8125rem]" title={r.holdout ?? "each family, with its own leave-one-family-out calibration"}>
              {r.holdout ?? "each, in turn"}
            </span>
          </Fact>
          <Fact label="Settings">
            <span className="block font-mono text-2xs leading-5 text-fg-muted">{settings}</span>
          </Fact>
        </dl>
      </div>
    </PlCard>
  );
}

function RegretChart({ record }: { record: DoneEvalRecord }) {
  const r = record.report;
  const ci = ciLabel(r.settings.ci_level);
  const judged = r.settings.criterion_policy;
  const bars: CompareBar[] = regretBars(r.policies).map((b) => ({
    key: b.key,
    label: b.key,
    sub: b.key === judged ? `${b.kind} · judged` : b.kind,
    value: b.y,
    ciLow: b.low ?? undefined,
    ciHigh: b.high ?? undefined,
    tone: b.key === judged ? "hive" : b.kind === "oracle" ? "base" : "other",
  }));
  return (
    <figure className="pl-regret flex flex-col gap-7" data-testid="regret-chart">
      <figcaption
        className="nd-label w-fit cursor-help"
        title="How much slower each policy's pick was than the best measured configuration, on average over the counted groups; the faint line is the bootstrap CI. The oracle always picks the best. Only the judged row is honey."
      >
        {`Mean regret per policy (bootstrap ${ci})`}
      </figcaption>
      {bars.length === 0 ? (
        <p className="text-sm text-fg-muted">No policy has a counted group, so there is no regret to chart.</p>
      ) : (
        <RegretBars bars={bars} />
      )}
    </figure>
  );
}

/** Regret bars: the bar to its mean, the bootstrap CI as a bracket on the same track, the number in its own column. */
function RegretBars({ bars }: { bars: readonly CompareBar[] }) {
  const max = Math.max(...bars.map((b) => Math.max(b.value, b.ciHigh ?? 0)), 1);
  const at = (v: number) => `${String((Math.max(0, v) / max) * 100)}%`;
  return (
    <ul aria-label={`Mean regret per policy: ${String(bars.length)} policies; the numbers are in the per-policy table.`} className="flex flex-col gap-6" data-testid="compare-bars">
      {bars.map((b) => (
        <li key={b.key} className="pl-rbar" data-tone={b.tone}>
          <span className="flex min-w-0 flex-col">
            <span className={cn("truncate text-[0.9375rem]", b.tone === "hive" ? "font-medium text-fg" : "text-fg-muted")} title={b.label}>
              {b.label}
            </span>
            {b.sub !== undefined && <span className={cn("jb-tag mt-0.5 text-[0.625rem]", b.tone === "hive" ? "text-[#ffc93c]" : "text-fg-subtle")}>{b.sub}</span>}
          </span>
          <svg aria-hidden="true" className="block h-4 w-full overflow-visible">
            <rect x="0" y="5" width="100%" height="6" rx="3" className="pl-rtrack" />
            <rect x="0" y="5" width={at(b.value)} height="6" rx="3" className="pl-rfill" />
            {b.ciLow !== undefined && b.ciHigh !== undefined && b.ciHigh > b.ciLow && (
              <g className="pl-rci">
                <line x1={at(b.ciLow)} x2={at(b.ciHigh)} y1="8" y2="8" />
                <line x1={at(b.ciLow)} x2={at(b.ciLow)} y1="2" y2="14" />
                <line x1={at(b.ciHigh)} x2={at(b.ciHigh)} y1="2" y2="14" />
              </g>
            )}
          </svg>
          <span className={cn("text-right font-mono text-[0.8125rem] tabular-nums", b.tone === "hive" ? "text-[#ffc93c]" : "text-fg-muted")}>{pct(b.value)}</span>
        </li>
      ))}
    </ul>
  );
}

function policyColumns(ci: string): PlColumn<PolicyResultOut>[] {
  return [
    { id: "key", header: "Policy", cell: (p) => <Clip text={p.key} className="font-mono text-[0.8125rem]" /> },
    { id: "kind", header: "Kind", cell: (p) => <span className="text-fg-muted">{p.kind}</span> },
    { id: "counted", header: "Counted", hint: "Groups counted for this policy.", mono: true, cell: (p) => String(p.counted_groups) },
    { id: "unmeasured", header: "Unmeasured", hint: "Groups where this policy's pick was never measured.", mono: true, cell: (p) => String(p.unmeasured_groups) },
    { id: "noreplay", header: "No replay", hint: "Groups the policy could not be replayed on.", mono: true, cell: (p) => String(p.no_replay_groups) },
    { id: "mean", header: "Mean regret", mono: true, cell: (p) => pct(p.mean_regret_pct) },
    { id: "ci", header: ci, mono: true, cell: (p) => ciText(p.ci_pct.low, p.ci_pct.high) },
    { id: "ms", header: "Regret time", full: "Mean regret (time)", mono: true, cell: (p) => formatMs(p.mean_regret_ms) },
    { id: "within", header: "Within", full: "Within threshold", hint: "Within threshold: the share of counted groups whose pick was within the regret threshold of the best.", mono: true, cell: (p) => pct(p.within_threshold_pct) },
  ];
}

/** Criterion 2 (D322): rule policy regret minus the criterion policy's, per rule policy, on common groups. */
function pairedColumns(ci: string): PlColumn<PairedComparisonOut>[] {
  return [
    { id: "key", header: "Rule policy", cell: (c) => <Clip text={c.key} className="font-mono text-[0.8125rem]" /> },
    { id: "common", header: "Common groups", hint: "Groups counted for both this rule policy and the judged row.", mono: true, cell: (c) => String(c.common_groups) },
    { id: "mean", header: "Mean diff (pts)", full: "Mean difference (% points)", mono: true, cell: (c) => points(c.mean_diff_pct) },
    { id: "ci", header: ci, mono: true, cell: (c) => pointsCi(c.ci_low, c.ci_high) },
    { id: "unmeasured", header: "Unmeasured", mono: true, cell: (c) => String(c.unmeasured_groups) },
  ];
}

const FAMILY_COLUMNS: PlColumn<FamilyResultOut>[] = [
  { id: "family", header: "Family", cell: (f) => <Clip text={f.family} className="font-mono text-[0.8125rem]" /> },
  { id: "key", header: "Policy", cell: (f) => <Clip text={f.key} className="font-mono text-[0.8125rem]" /> },
  { id: "counted", header: "Counted", mono: true, cell: (f) => String(f.counted_groups) },
  { id: "mean", header: "Mean regret", mono: true, cell: (f) => pct(f.mean_regret_pct) },
  { id: "ms", header: "Regret time", full: "Mean regret (time)", mono: true, cell: (f) => formatMs(f.mean_regret_ms) },
  { id: "within", header: "Within", full: "Within threshold", mono: true, cell: (f) => pct(f.within_threshold_pct) },
];

const CALIBRATION_USED_COLUMNS: PlColumn<CalibrationUsedOut>[] = [
  { id: "family", header: "Family", cell: (c) => <Clip text={c.family} className="font-mono text-[0.8125rem]" /> },
  { id: "source", header: "Calibration", cell: (c) => (c.source === "active" && c.version != null ? `active v${String(c.version)}` : c.source) },
  { id: "fitted", header: "Fitted runs", mono: true, cell: (c) => String(c.fitted_runs) },
  { id: "excluded", header: "Held out", hint: "Runs of this family left out of its calibration.", mono: true, cell: (c) => String(c.excluded_runs) },
  { id: "factors", header: "Factors", mono: true, cell: (c) => String(c.factors) },
];

type DetailTab = "regret" | "policies" | "paired" | "families" | "calibrations";

const DETAIL_HINT: Record<DetailTab, string> = {
  regret: "Mean regret per policy as bars; the numbers are under Per policy.",
  policies: "The numbers behind the bars: groups counted per policy, the mean regret with its CI, in time, and the share of groups within the threshold.",
  paired: "Each rule policy's regret minus the judged row's, on the groups counted for both (% points; above 0 = the judged row picked better). Criterion 2 passes only if every difference's CI lies entirely above 0.",
  families: "The judged row and its rule rivals per benchmark family.",
  calibrations: "Which calibration the calibrated rows used per family: refit without that family's runs (leave-one-family-out), or the live one (a leaked report).",
};

/** The report's detail, one view at a time in one frosted card: regret bars, per policy, paired, per family, calibrations used. */
function Details({ record }: { record: DoneEvalRecord }) {
  const r = record.report;
  const ci = ciLabel(r.settings.ci_level);
  const paired = r.verdicts.find((v) => v.criterion === 2)?.comparisons ?? [];
  const items: TabItem[] = [
    { value: "regret", label: "Regret" },
    { value: "policies", label: "Per policy" },
    ...(paired.length > 0 ? [{ value: "paired", label: "Paired" }] : []),
    { value: "families", label: "Per family" },
    { value: "calibrations", label: "Calibrations used" },
  ];
  const [picked, setPicked] = useState<DetailTab>("regret");
  const tab: DetailTab = items.some((t) => t.value === picked) ? picked : "regret";
  let body: ReactNode;
  if (tab === "policies") {
    body = <PlTable caption="Regret per policy" columns={policyColumns(ci)} rows={r.policies} rowKey={(p) => p.key} className="pl-policies" scrollClassName="pl-detail-scroll" />;
  } else if (tab === "paired") {
    body = (
      <PlTable
        caption="Criterion 2: paired regret difference per rule policy"
        columns={pairedColumns(ci)}
        rows={paired}
        rowKey={(c) => c.key}
        className="pl-paired"
        scrollClassName="pl-detail-scroll"
      />
    );
  } else if (tab === "families") {
    body = (
      <PlTable
        caption="Regret per family and policy"
        columns={FAMILY_COLUMNS}
        rows={r.families}
        rowKey={(f) => `${f.family}|${f.key}`}
        empty="No family results."
        className="pl-families"
        scrollClassName="pl-detail-scroll"
      />
    );
  } else if (tab === "calibrations") {
    body = (
      <PlTable
        caption="Calibration used per family"
        columns={CALIBRATION_USED_COLUMNS}
        rows={r.calibrations}
        rowKey={(c) => c.family}
        empty="No calibrated rows."
        className="pl-calused"
        scrollClassName="pl-detail-scroll"
      />
    );
  } else {
    body = (
      <div className="pl-body pt-4 pb-6">
        <RegretChart record={record} />
      </div>
    );
  }
  return (
    <section aria-label="Report details" className="ov-card ov-frost col-span-2 min-w-0 pb-4">
      <header className="pl-head flex items-end justify-between gap-x-8">
        <TextTabs
          label="Report details"
          idPrefix="planner-report"
          items={items}
          value={tab}
          onChange={(v) => {
            setPicked(v as DetailTab);
          }}
        />
        <PlNote className="pb-2.5" title={DETAIL_HINT[tab]}>
          {`report #${String(record.id)}`}
        </PlNote>
      </header>
      <div role="tabpanel" id={`planner-report-panel-${tab}`} aria-labelledby={`planner-report-tab-${tab}`} key={tab} className="nd-in pt-2">
        {body}
      </div>
    </section>
  );
}

/** V0.15 G6b (D378): a report still evaluating - its progress state, open beside the list. */
function RunningReport({ record, checkedAt }: { record: EvalReportRecordOut; checkedAt: number }) {
  return (
    <PlCard plain title={`Report #${String(record.id)}`} meta={<PlNote>running</PlNote>} className="pl-fill">
      <div
        role="status"
        aria-live="polite"
        className="pl-body flex cursor-help flex-col gap-2 pt-5 text-[0.9375rem]"
        data-testid="eval-report-running"
        title={`The controller trains one ML fold model per family and replays every record under every policy, so this can take minutes. This view checks every ${String(EVAL_POLL_MS / 1000)} s and shows the report when it is done.`}
      >
        <p className="inline-flex items-center gap-2.5">
          <Light className="bg-status-busy" breathe />
          <span>
            Evaluating <span className="font-mono text-[0.8125rem]">{record.campaign_ids.join(", ")}</span>
            {record.holdout !== null && <> (held-out family {record.holdout})</>} - started{" "}
            {formatAgo(secondsSince(record.created_at, checkedAt))}.
          </span>
        </p>
        <PlNote className="pl-[1.125rem]">{`checks every ${String(EVAL_POLL_MS / 1000)} s`}</PlNote>
      </div>
    </PlCard>
  );
}

/** V0.15 G6b (D378): a report that ended without a result. */
function FailedReport({ record }: { record: EvalReportRecordOut }) {
  return (
    <PlCard plain title={`Report #${String(record.id)}`} meta={<PlNote>failed</PlNote>} className="pl-fill">
      <div role="alert" className="pl-body flex cursor-help flex-col gap-2 pt-5" data-testid="eval-report-failed" title="No report was stored. Run the evaluation again once the cause is fixed.">
        <p className="inline-flex items-center gap-2.5 text-[0.9375rem] text-fg">
          <Light className="bg-status-failed" />
          <span>
            Report #{record.id} failed: {failedText(record.reason)}
            {record.reason !== null && <span className="font-mono text-[0.8125rem] text-fg-muted"> ({record.reason})</span>}
          </span>
        </p>
        {record.detail !== null && <p className="pl-[1.125rem] text-sm text-fg-muted">{record.detail}</p>}
      </div>
    </PlCard>
  );
}

/** A 404 without the unknown-report reason: the controller has no such route (D321). */
function EvalUnavailable() {
  return (
    <EmptyState title="Evaluation reports are not available">
      This controller has no offline evaluator (it arrives with V0.14 G4). Once it does, reports made with{" "}
      <code className="font-mono">compute planner eval</code> appear here.
    </EmptyState>
  );
}

function Report({ id }: { id: number }) {
  const query = useEvalReport(id);
  if (query.isPending) {
    return (
      <p role="status" className="pl-fill pl-body pt-7 text-sm text-fg-muted">
        Loading report #{id}…
      </p>
    );
  }
  if (query.isError) {
    let body: ReactNode;
    if (isEvalReportNotFound(query.error)) {
      body = (
        <EmptyState title={`Report #${String(id)} not found`}>
          This controller has no evaluation report #{id} - pick one from the list.
        </EmptyState>
      );
    } else if (isEvalUnavailable(query.error)) body = <EvalUnavailable />;
    else body = <ErrorState title={`Could not load report #${String(id)}`} error={query.error} onRetry={() => void query.refetch()} />;
    return <div className="pl-fill min-w-0">{body}</div>;
  }
  const record = query.data;
  if (record.status === "running") return <RunningReport record={record} checkedAt={query.dataUpdatedAt} />;
  if (!isDone(record)) return <FailedReport record={record} />;
  return (
    <div className="contents" data-testid="eval-report-detail">
      <Summary record={record} />
      <Details record={record} />
    </div>
  );
}

export function EvaluationTab({ selected, onSelect }: { selected: number | null; onSelect: (id: number) => void }) {
  const list = useEvalReports();
  if (list.isPending) {
    return (
      <p role="status" className="py-8 text-center text-sm text-fg-muted">
        Loading evaluation reports…
      </p>
    );
  }
  if (list.isError) {
    // the list route never answers 404 itself: a 404 means this controller has no such route
    if (isEvalUnavailable(list.error)) return <EvalUnavailable />;
    return <ErrorState title="Could not load evaluation reports" error={list.error} onRetry={() => void list.refetch()} />;
  }
  const reports = list.data.reports;
  if (reports.length === 0) {
    return (
      <EmptyState title="No evaluation reports yet">
        Evaluate benchmark campaigns with <code className="font-mono">compute planner eval --campaign ID</code> (admins: or
        the round button above): the report - regret per policy against the best measured configuration, and the
        criterion verdicts - appears here.
      </EmptyState>
    );
  }
  const shown = selected ?? reports[0]?.id ?? null;
  return (
    <div className="grid grid-cols-[minmax(0,38fr)_minmax(0,62fr)] gap-6">
      <PlCard
        title="Reports"
        hint="The newest evaluation reports, newest first. Verdicts: criterion 1 (picks within the threshold of the best), 2 (better than every rule policy), 3 (decision time)."
        meta={<PlNote>{`${String(reports.length)} newest`}</PlNote>}
        className="pl-fill flex flex-col"
      >
        <PlTable
          caption="Evaluation reports, newest first"
          columns={reportColumns(shown, onSelect)}
          rows={reports}
          rowKey={(r) => String(r.id)}
          className="pl-reports flex-1"
          selected={(r) => r.id === shown}
          onRowClick={(r) => {
            onSelect(r.id);
          }}
        />
      </PlCard>
      {shown !== null && <Report id={shown} />}
    </div>
  );
}

/* ------------------------------------------------------------ run evaluation */

/** Most campaigns one evaluation may take (planner_eval_models `MAX_EVAL_CAMPAIGNS`). */
const MAX_EVAL_CAMPAIGNS = 20;

const PlusIcon = () => (
  <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round">
    <path d="M12 5v14M5 12h14" />
  </svg>
);

/**
 * V0.15 G6b (D378): **Run evaluation** (admins) - the title row's round honey
 * button opens a frosted form. Starts the evaluation in the background
 * (`POST`, 202) and selects the new report, whose view shows the progress
 * state; while one runs, the form says so (one at a time).
 */
export function RunEvaluation({ onStarted }: { onStarted: (id: number) => void }) {
  const list = useEvalReports();
  const reports = list.data?.reports ?? [];
  const running = reports.find((r) => r.status === "running") ?? null;
  const [open, setOpen] = useState(false);
  const [campaigns, setCampaigns] = useState("");
  const [holdout, setHoldout] = useState("");
  // the report this page just started, until the list shows it (G6b tester T6)
  const [awaiting, setAwaiting] = useState<number | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const ids = { panel: useId(), campaigns: useId(), holdout: useId() };
  const client = useQueryClient();
  const toast = useToast();
  const campaignIds = parseCampaignIds(campaigns);
  const close = useCallback(() => {
    setOpen(false);
  }, []);
  useOutsideClose(open, root, close);
  const start = useMutation({
    mutationFn: () => startEvalReport({ campaign_ids: campaignIds, holdout: holdout.trim() === "" ? null : holdout.trim(), allow_leak: false }),
    onSuccess: (out) => {
      setAwaiting(out.report_id);
      void client.invalidateQueries({ queryKey: plannerKeys.reports });
      toast({ title: "Evaluation started", description: `Report #${String(out.report_id)} is running.`, tone: "ok" });
      setOpen(false);
      onStarted(out.report_id);
    },
    onError: (error) => {
      // another admin's evaluation runs: re-read the list so it (and its note) appear (T5)
      if (isEvalAlreadyRunning(error)) void client.invalidateQueries({ queryKey: plannerKeys.reports });
    },
  });
  // the started report, from the same cache as its view: once it ended, stop waiting
  const started = useEvalReport(awaiting);
  const waiting =
    awaiting !== null &&
    !reports.some((r) => r.id === awaiting) &&
    !started.isError &&
    started.data?.status !== "done" &&
    started.data?.status !== "failed";
  const tooMany = campaignIds.length > MAX_EVAL_CAMPAIGNS;
  const busy = running !== null || start.isPending || waiting;
  const label = running !== null ? `Report #${String(running.id)} running…` : start.isPending || waiting ? "Starting…" : "Run evaluation";
  return (
    <div ref={root} className="relative shrink-0">
      <button
        type="button"
        className="jb-submit-btn pl-round relative"
        aria-label="New evaluation"
        title={running !== null ? `New evaluation (report #${String(running.id)} is running)` : "New evaluation"}
        aria-expanded={open}
        aria-controls={ids.panel}
        onClick={() => {
          setOpen((v) => !v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
      >
        <PlusIcon />
        {running !== null && <span aria-hidden="true" className="jb-filter-dot pl-run-dot nd-breathe" />}
      </button>
      {open && (
        <div
          id={ids.panel}
          className="ov-card ov-frost jb-filter-pop pl-run-pop"
          onKeyDown={(e) => {
            if (e.key === "Escape") setOpen(false);
          }}
        >
          <p className="nd-label">Run evaluation</p>
          <form
            className="flex flex-col gap-5"
            aria-label="Run an evaluation"
            onSubmit={(e) => {
              e.preventDefault();
              if (campaignIds.length > 0 && !tooMany && !busy) start.mutate();
            }}
          >
            <div className="flex flex-col gap-1.5">
              <label htmlFor={ids.campaigns} className="text-[0.8125rem] text-fg-muted">
                Campaigns (ids, comma or space separated)
              </label>
              <input
                id={ids.campaigns}
                className="jb-field w-full font-mono"
                value={campaigns}
                spellCheck={false}
                autoCapitalize="none"
                placeholder="camp_…"
                onChange={(e) => {
                  setCampaigns(e.target.value);
                }}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor={ids.holdout} className="text-[0.8125rem] text-fg-muted">
                Held-out family (optional)
              </label>
              <input
                id={ids.holdout}
                className="jb-field w-full font-mono"
                value={holdout}
                maxLength={64}
                spellCheck={false}
                autoCapitalize="none"
                placeholder="each, in turn"
                onChange={(e) => {
                  setHoldout(e.target.value);
                }}
              />
            </div>
            <div className="flex items-center justify-between gap-4">
              <PlNote>{campaignIds.length === 0 ? "no campaign yet" : `${String(campaignIds.length)} campaign${campaignIds.length === 1 ? "" : "s"}`}</PlNote>
              <button type="submit" className="pl-go" disabled={campaignIds.length === 0 || tooMany || busy}>
                {busy && <span aria-hidden="true" className="size-2.5 animate-spin rounded-full border border-current border-t-transparent" />}
                {label}
              </button>
            </div>
          </form>
          {running !== null && (
            <p role="status" className="text-xs text-fg-muted" data-testid="eval-running-note">
              Report #{running.id} is evaluating {running.campaign_ids.join(", ")} (started {formatTime(running.created_at)}).
              One evaluation runs at a time; this page updates every {EVAL_POLL_MS / 1000} s.
            </p>
          )}
          {tooMany && <p className="text-xs text-status-failed">At most {MAX_EVAL_CAMPAIGNS} campaigns per evaluation.</p>}
          {start.isError && (
            <p role="alert" className="text-sm text-status-failed">
              Could not start the evaluation: {describeError(start.error).message}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
