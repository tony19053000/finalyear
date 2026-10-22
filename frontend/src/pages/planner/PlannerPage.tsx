/**
 * Planner (V0.14 G5, D300): how the planner corrects itself and how good it is.
 *
 * * **Calibration** (`GET /v1/planner/calibration`): what the planner uses now
 *   (auto / off / pinned, the version in use), the shown version's factors -
 *   per term, key level, node, family, with `n` - and the latest versions with
 *   their status and the reason a fit was rejected. Terms that are never
 *   scaled are listed with why (the API's `not_calibrated`).
 * * **Evaluation reports** (`GET /v1/planner/eval-reports`, D318): the newest
 *   reports with their criterion verdicts; the selected one's per-policy mean
 *   regret with its bootstrap CI (bars + whiskers, and the numbers as a
 *   table; the CI level is the report's `settings.ci_level`), criterion 2's
 *   paired comparisons (D322: rule policy regret minus the judged row's, per
 *   rule policy on the common groups), per-family results, the calibrations
 *   used and the counts. A 404 with no `unknown_eval_report` reason means the
 *   controller has no evaluator ("not available"); with it, the report id does
 *   not exist ("not found", D321). A report
 *   made with `--allow-leak` carries a LEAKED banner: its verdicts are not
 *   evidence.
 *
 * Every number is the API's. Selection lives in the URL
 * (`?calibration=N`, `?report=ID`), so a view can be linked.
 */
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ErrorBar, XAxis, YAxis } from "recharts";
import { useSearchParams } from "react-router-dom";

import { queryKeys } from "../../api/queryKeys";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Card } from "../../components/Card";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatTime } from "../../components/jobs/format";
import { formatMs } from "../../lib/format";
import { PageHeader } from "../PageHeader";
import {
  getCalibration,
  getEvalReport,
  isEvalReportNotFound,
  isEvalUnavailable,
  listEvalReports,
  type CalibrationSummary,
  type CalibrationView,
  type EvalReportRecordOut,
  type EvalReportSummaryOut,
  type FactorOut,
  type FamilyResultOut,
  type CalibrationUsedOut,
  type PairedComparisonOut,
  type PolicyResultOut,
  type VerdictOut,
} from "./plannerApi";
import {
  VERDICT_TONE,
  ciLabel,
  ciText,
  factorText,
  judgedText,
  latencyBuckets,
  latencyText,
  pct,
  points,
  pointsCi,
  regretBars,
  verdictText,
} from "./plannerView";

const plannerKeys = {
  calibration: (version: number | null) => [...queryKeys.planner, "calibration", version ?? "active"] as const,
  reports: [...queryKeys.planner, "eval-reports"] as const,
  report: (id: number) => [...queryKeys.planner, "eval-report", id] as const,
};

function positiveInt(raw: string | null): number | null {
  if (raw === null || !/^[1-9][0-9]{0,15}$/.test(raw)) return null;
  return Number(raw);
}

function Verdicts({ verdicts }: { verdicts: readonly VerdictOut[] }) {
  if (verdicts.length === 0) return <span className="text-fg-subtle">—</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {verdicts.map((v) => (
        <Badge key={v.criterion} tone={VERDICT_TONE[v.status]}>
          {verdictText(v)}
        </Badge>
      ))}
    </span>
  );
}

/* ------------------------------------------------------------ calibration */

const FACTOR_COLUMNS: Column<FactorOut>[] = [
  { id: "term", header: "Term", mono: true, cell: (f) => f.term },
  { id: "level", header: "Key level", cell: (f) => f.key_level },
  { id: "node", header: "Node", mono: true, cell: (f) => f.node_id ?? "all nodes" },
  { id: "family", header: "Family", mono: true, cell: (f) => f.family ?? "—" },
  { id: "factor", header: "Factor", align: "right", mono: true, cell: (f) => factorText(f.factor) },
  { id: "n", header: "n", align: "right", mono: true, cell: (f) => String(f.n) },
  { id: "median", header: "Median actual / predicted", align: "right", mono: true, cell: (f) => f.median.toFixed(3) },
];

function versionColumns(shown: number | null, select: (v: number) => void): Column<CalibrationSummary>[] {
  return [
    {
      id: "version",
      header: "Version",
      mono: true,
      cell: (v) => (
        <button
          type="button"
          className="font-mono text-accent underline-offset-2 hover:underline"
          aria-pressed={v.version === shown}
          onClick={() => {
            select(v.version);
          }}
        >
          v{v.version}
        </button>
      ),
    },
    { id: "created", header: "Created", mono: true, cell: (v) => formatTime(v.created_at) },
    {
      id: "status",
      header: "Status",
      cell: (v) => (
        <Badge tone={v.status === "active" ? "ok" : v.status === "rejected" ? "failed" : v.status === "pinned" ? "accent" : "neutral"}>
          {v.status}
        </Badge>
      ),
    },
    {
      id: "error",
      header: "Held-out error (new / active)",
      mono: true,
      cell: (v) =>
        `${v.holdout_error_new == null ? "—" : v.holdout_error_new.toFixed(3)} / ${v.holdout_error_active == null ? "—" : v.holdout_error_active.toFixed(3)}`,
    },
    { id: "reason", header: "Reason", cell: (v) => v.reason ?? "—" },
  ];
}

function CalibrationPanel({ view, onSelect }: { view: CalibrationView; onSelect: (v: number | null) => void }) {
  const shown = view.calibration;
  const inUse = view.in_use;
  return (
    <div className="flex flex-col gap-4">
      <Card
        title={shown == null ? "Calibration factors" : `Calibration factors - v${String(shown.version)} (${shown.status})`}
        actions={
          <span className="text-xs text-fg-muted" data-testid="calibration-in-use">
            mode <span className="font-mono text-fg">{inUse.mode}</span>
            {inUse.pinned_version != null && <> · pinned v{inUse.pinned_version}</>} · in use{" "}
            <span className="font-mono text-fg">{inUse.in_use_version == null ? "none" : `v${String(inUse.in_use_version)}`}</span> · active{" "}
            <span className="font-mono text-fg">{view.active_version == null ? "none" : `v${String(view.active_version)}`}</span>
          </span>
        }
        flush
      >
        {shown == null ? (
          <EmptyState title="No calibration yet" className="m-4">
            The planner predicts with its uncalibrated estimates until enough finished runs are matched (refreshed every
            30 min, or now with <code className="font-mono">compute planner calibrate</code>).
          </EmptyState>
        ) : !shown.readable ? (
          <p role="alert" className="p-4 text-sm text-status-failed">
            The factors of v{shown.version} could not be read, so the planner never uses this version.
          </p>
        ) : (
          <Table
            caption={`Calibration factors of version ${String(shown.version)}`}
            columns={FACTOR_COLUMNS}
            rows={shown.factors}
            rowKey={(f) => `${f.term}|${f.key_level}|${f.node_id ?? ""}|${f.family ?? ""}`}
            empty="This version has no factors."
          />
        )}
        <p className="border-t border-border px-4 py-2 text-2xs text-fg-subtle">
          Never scaled:{" "}
          {Object.entries(view.not_calibrated)
            .map(([term, why]) => `${term} (${why})`)
            .join(", ")}
          . Scaled against: {Object.entries(view.term_phase)
            .map(([term, phase]) => `${term} ↔ ${phase}`)
            .join(", ")}
          .
        </p>
      </Card>
      <Card title="Calibration versions" flush>
        <Table
          caption="Calibration versions, newest first"
          columns={versionColumns(shown?.version ?? null, onSelect)}
          rows={view.recent}
          rowKey={(v) => String(v.version)}
          empty="No calibration version has been fitted yet."
        />
      </Card>
    </div>
  );
}

function Calibration({ version, onSelect }: { version: number | null; onSelect: (v: number | null) => void }) {
  const query = useQuery({
    queryKey: plannerKeys.calibration(version),
    queryFn: ({ signal }) => getCalibration(version, signal),
  });
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading calibration…
      </p>
    );
  }
  if (query.isError) {
    return (
      <ErrorState
        title={version === null ? "Could not load the calibration" : `Could not load calibration v${String(version)}`}
        error={query.error}
        onRetry={() => void query.refetch()}
      />
    );
  }
  return <CalibrationPanel view={query.data} onSelect={onSelect} />;
}

/* ------------------------------------------------------------ eval reports */

function reportColumns(selected: number | null, select: (id: number) => void): Column<EvalReportSummaryOut>[] {
  return [
    {
      id: "id",
      header: "Report",
      mono: true,
      cell: (r) => (
        <button
          type="button"
          className="font-mono text-accent underline-offset-2 hover:underline"
          aria-pressed={r.id === selected}
          onClick={() => {
            select(r.id);
          }}
        >
          #{r.id}
        </button>
      ),
    },
    { id: "created", header: "Created", mono: true, cell: (r) => formatTime(r.created_at) },
    { id: "campaigns", header: "Campaigns", mono: true, cell: (r) => r.campaign_ids.join(", ") },
    { id: "holdout", header: "Held-out family", mono: true, cell: (r) => r.holdout ?? "each, in turn" },
    { id: "groups", header: "Groups", align: "right", mono: true, cell: (r) => String(r.evaluated_groups) },
    {
      id: "verdicts",
      header: "Verdicts",
      cell: (r) => (
        <span className="inline-flex flex-wrap items-center gap-1">
          {r.leaked && <Badge tone="failed">LEAKED</Badge>}
          <Verdicts verdicts={r.verdicts} />
        </span>
      ),
    },
  ];
}

function policyColumns(ci: string): Column<PolicyResultOut>[] {
  return [
  { id: "key", header: "Policy", mono: true, cell: (p) => p.key },
  { id: "kind", header: "Kind", cell: (p) => p.kind },
  { id: "counted", header: "Counted", align: "right", mono: true, cell: (p) => String(p.counted_groups) },
  { id: "unmeasured", header: "Unmeasured", align: "right", mono: true, cell: (p) => String(p.unmeasured_groups) },
  { id: "noreplay", header: "No replay", align: "right", mono: true, cell: (p) => String(p.no_replay_groups) },
  { id: "mean", header: "Mean regret", align: "right", mono: true, cell: (p) => pct(p.mean_regret_pct) },
  { id: "ci", header: ci, align: "right", mono: true, cell: (p) => ciText(p.ci_pct.low, p.ci_pct.high) },
  { id: "ms", header: "Mean regret (time)", align: "right", mono: true, cell: (p) => formatMs(p.mean_regret_ms) },
  { id: "within", header: "Within threshold", align: "right", mono: true, cell: (p) => pct(p.within_threshold_pct) },
  ];
}

/** Criterion 2 (D322): rule policy regret minus the criterion policy's, per rule policy, on common groups. */
function pairedColumns(ci: string): Column<PairedComparisonOut>[] {
  return [
    { id: "key", header: "Rule policy", mono: true, cell: (c) => c.key },
    { id: "common", header: "Common groups", align: "right", mono: true, cell: (c) => String(c.common_groups) },
    { id: "mean", header: "Mean difference (% points)", align: "right", mono: true, cell: (c) => points(c.mean_diff_pct) },
    { id: "ci", header: ci, align: "right", mono: true, cell: (c) => pointsCi(c.ci_low, c.ci_high) },
    { id: "unmeasured", header: "Unmeasured", align: "right", mono: true, cell: (c) => String(c.unmeasured_groups) },
  ];
}

const FAMILY_COLUMNS: Column<FamilyResultOut>[] = [
  { id: "family", header: "Family", mono: true, cell: (f) => f.family },
  { id: "key", header: "Policy", mono: true, cell: (f) => f.key },
  { id: "counted", header: "Counted", align: "right", mono: true, cell: (f) => String(f.counted_groups) },
  { id: "mean", header: "Mean regret", align: "right", mono: true, cell: (f) => pct(f.mean_regret_pct) },
  { id: "ms", header: "Mean regret (time)", align: "right", mono: true, cell: (f) => formatMs(f.mean_regret_ms) },
  { id: "within", header: "Within threshold", align: "right", mono: true, cell: (f) => pct(f.within_threshold_pct) },
];

const CALIBRATION_USED_COLUMNS: Column<CalibrationUsedOut>[] = [
  { id: "family", header: "Family", mono: true, cell: (c) => c.family },
  { id: "source", header: "Calibration", cell: (c) => (c.source === "active" && c.version != null ? `active v${String(c.version)}` : c.source) },
  { id: "fitted", header: "Fitted runs", align: "right", mono: true, cell: (c) => String(c.fitted_runs) },
  { id: "excluded", header: "Held out", align: "right", mono: true, cell: (c) => String(c.excluded_runs) },
  { id: "factors", header: "Factors", align: "right", mono: true, cell: (c) => String(c.factors) },
];

const CHART_WIDTH = 720;
const CHART_HEIGHT = 280;
const TICK = { fill: "currentColor", fontSize: 11 };

function RegretChart({ policies, ci }: { policies: readonly PolicyResultOut[]; ci: string }) {
  const bars = regretBars(policies);
  if (bars.length === 0) {
    return <p className="text-sm text-fg-muted">No policy has a counted group, so there is no regret to chart.</p>;
  }
  return (
    <figure className="flex flex-col gap-2" data-testid="regret-chart">
      <figcaption className="text-sm font-medium text-fg">Mean regret per policy (bootstrap {ci})</figcaption>
      <div
        role="img"
        aria-label={`Mean regret per policy: ${String(bars.length)} policies; the numbers are in the policy table.`}
        className="overflow-x-auto text-fg-muted"
      >
        <BarChart width={CHART_WIDTH} height={CHART_HEIGHT} data={bars} margin={{ top: 12, right: 24, bottom: 64, left: 16 }}>
          <CartesianGrid stroke="currentColor" strokeOpacity={0.15} vertical={false} />
          <XAxis type="category" dataKey="key" tick={TICK} stroke="currentColor" interval={0} angle={-30} textAnchor="end" />
          <YAxis type="number" tick={TICK} tickFormatter={(v: number) => `${String(v)} %`} stroke="currentColor" width={56} />
          <Bar dataKey="y" name="mean regret" className="text-accent" fill="currentColor" fillOpacity={0.7} isAnimationActive={false}>
            <ErrorBar dataKey="err" direction="y" width={8} stroke="currentColor" strokeWidth={1.5} />
          </Bar>
        </BarChart>
      </div>
    </figure>
  );
}

function ReportDetail({ record }: { record: EvalReportRecordOut }) {
  const r = record.report;
  const s = r.settings;
  const ci = ciLabel(s.ci_level);
  const paired = r.verdicts.find((v) => v.criterion === 2)?.comparisons ?? [];
  return (
    <div className="flex flex-col gap-4" data-testid="eval-report-detail">
      {r.leaked && (
        <div role="alert" className="rounded-lg border border-status-failed bg-status-failed-soft px-4 py-3">
          <p className="text-sm font-semibold text-status-failed">LEAKED</p>
          <p className="text-sm text-fg">
            This report was made with <code className="font-mono">--allow-leak</code>: its calibrated rows used the live
            calibration, which may have been fitted on these very runs. Its verdicts are not evidence for the success
            criteria.
          </p>
        </div>
      )}
      <Card title={`Report #${String(record.id)} - ${formatTime(record.created_at)}`}>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-fg-muted">Campaigns</dt>
          <dd className="font-mono">{r.campaign_ids.join(", ")}</dd>
          <dt className="text-fg-muted">Held-out family</dt>
          <dd className="font-mono">{r.holdout ?? "each family, with its own leave-one-family-out calibration"}</dd>
          <dt className="text-fg-muted">Verdicts</dt>
          <dd className="flex flex-col gap-1">
            {r.verdicts.map((v) => (
              <span key={v.criterion} className="inline-flex flex-wrap items-center gap-2">
                <Badge tone={VERDICT_TONE[v.status]}>{verdictText(v)}</Badge>
                <span className="text-xs text-fg-muted">{v.detail}</span>
              </span>
            ))}
          </dd>
          <dt className="text-fg-muted">Groups</dt>
          <dd className="font-mono text-xs">
            {r.counts.evaluated_groups} evaluated of {r.counts.groups} ·{" "}
            {Object.entries(r.counts.labels)
              .map(([label, n]) => `${label} ${String(n)}`)
              .join(" · ")}{" "}
            · no replay {r.counts.no_replay_groups} · records {r.counts.records_used} used of {r.counts.records}
            {r.counts.non_finite_configs > 0 && <> · non-finite configurations dropped {r.counts.non_finite_configs}</>}
          </dd>
          <dt className="text-fg-muted">Decision latency</dt>
          <dd className="font-mono text-xs" data-testid="eval-latency">
            {r.latency != null ? (
              <>
                <span>{latencyText(r.latency)}</span>
                {latencyBuckets(r.latency).map((b) => (
                  <span key={b} className="block text-fg-muted">
                    {b}
                  </span>
                ))}
                <span className="block text-fg-muted">{judgedText(r.latency)}</span>
              </>
            ) : (
              <span className="text-fg-subtle">not recorded (report made before decision times were stored)</span>
            )}
          </dd>
          <dt className="text-fg-muted">Settings</dt>
          <dd className="font-mono text-xs">
            within {s.regret_threshold_pct} % in ≥ {s.within_target_pct} % of groups · min {s.min_groups} groups ·{" "}
            {s.bootstrap_resamples} resamples, seed {s.bootstrap_seed}, {ci} · judged row {s.criterion_policy}
          </dd>
        </dl>
      </Card>
      {paired.length > 0 && (
        <Card title="Criterion 2, paired" flush>
          <p className="px-4 pt-3 text-xs text-fg-muted">
            Each rule policy&apos;s regret minus <span className="font-mono">{s.criterion_policy}</span>&apos;s, on the groups
            counted for both (% points; above 0 = the judged row picked better). Criterion 2 passes only if every
            difference&apos;s {ci} lies entirely above 0.
          </p>
          <Table
            caption="Criterion 2: paired regret difference per rule policy"
            columns={pairedColumns(ci)}
            rows={paired}
            rowKey={(c) => c.key}
          />
        </Card>
      )}
      <Card title="Regret per policy">
        <div className="flex flex-col gap-3">
          <RegretChart policies={r.policies} ci={ci} />
          <Table caption="Regret per policy" columns={policyColumns(ci)} rows={r.policies} rowKey={(p) => p.key} />
        </div>
      </Card>
      <Card title="Per family" flush>
        <Table
          caption="Regret per family and policy"
          columns={FAMILY_COLUMNS}
          rows={r.families}
          rowKey={(f) => `${f.family}|${f.key}`}
          empty="No family results."
        />
      </Card>
      <Card title="Calibrations used" flush>
        <Table
          caption="Calibration used per family"
          columns={CALIBRATION_USED_COLUMNS}
          rows={r.calibrations}
          rowKey={(c) => c.family}
          empty="No calibrated rows."
        />
      </Card>
    </div>
  );
}

function Report({ id }: { id: number }) {
  const query = useQuery({ queryKey: plannerKeys.report(id), queryFn: ({ signal }) => getEvalReport(id, signal) });
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading report #{id}…
      </p>
    );
  }
  if (query.isError) {
    if (isEvalReportNotFound(query.error)) {
      return (
        <EmptyState title={`Report #${String(id)} not found`}>
          This controller has no evaluation report #{id} - pick one from the list above.
        </EmptyState>
      );
    }
    if (isEvalUnavailable(query.error)) return <EvalUnavailable />;
    return <ErrorState title={`Could not load report #${String(id)}`} error={query.error} onRetry={() => void query.refetch()} />;
  }
  return <ReportDetail record={query.data} />;
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

function EvalReports({ selected, onSelect }: { selected: number | null; onSelect: (id: number) => void }) {
  const list = useQuery({ queryKey: plannerKeys.reports, queryFn: ({ signal }) => listEvalReports(signal) });
  if (list.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
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
        Evaluate benchmark campaigns with <code className="font-mono">compute planner eval --campaign ID</code>: the report
        - regret per policy against the best measured configuration, and the criterion verdicts - appears here.
      </EmptyState>
    );
  }
  const shown = selected !== null ? selected : (reports[0]?.id ?? null);
  return (
    <div className="flex flex-col gap-4">
      <Card title={`Evaluation reports (${String(reports.length)})`} flush>
        <Table caption="Evaluation reports, newest first" columns={reportColumns(shown, onSelect)} rows={reports} rowKey={(r) => String(r.id)} />
      </Card>
      {shown !== null && <Report id={shown} />}
    </div>
  );
}

export function PlannerPage() {
  useDocumentTitle("Planner");
  const [params, setParams] = useSearchParams();
  const calibration = positiveInt(params.get("calibration"));
  const report = positiveInt(params.get("report"));
  const set = (key: string, value: number | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, String(value));
    setParams(next, { replace: true });
  };
  return (
    <>
      <PageHeader
        title="Planner"
        description="How the planner corrects its estimates (calibration) and how good its choices are (offline evaluation against measured benchmark runs)."
      />
      <div className="flex flex-col gap-8">
        <section aria-labelledby="planner-calibration" className="flex flex-col gap-3">
          <h2 id="planner-calibration" className="text-base font-semibold">
            Calibration
          </h2>
          <Calibration
            version={calibration}
            onSelect={(v) => {
              set("calibration", v);
            }}
          />
        </section>
        <section aria-labelledby="planner-evaluation" className="flex flex-col gap-3">
          <h2 id="planner-evaluation" className="text-base font-semibold">
            Evaluation
          </h2>
          <EvalReports
            selected={report}
            onSelect={(id) => {
              set("report", id);
            }}
          />
        </section>
      </div>
    </>
  );
}
