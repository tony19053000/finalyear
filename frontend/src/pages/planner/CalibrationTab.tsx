/**
 * The Planner's **Calibration** tab (V0.14 G2 / G5, D300): the shown
 * version's factors - per term, key level, node, family, with `n` - in a
 * frosted table filling the window, and beside it the version history: the
 * held-out error of every fit against the active one at the time (the gate's
 * two numbers, D306) and the versions with their status and the reason a fit
 * was rejected. Terms that are never scaled are named in a tooltip (the
 * API's `not_calibrated`). Every number is the API's; `?calibration=N`
 * selects a version.
 */
import { EmptyState, ErrorState } from "../../components/States";
import { formatDayTime, formatTime } from "../../components/jobs/format";
import { cn } from "../../lib/cn";
import { Legend, LineChart, type LineSeries } from "../benchmarks/BenchCharts";
import { dash, useNodeNames } from "../benchmarks/benchShared";
import type { CalibrationSummary, CalibrationView, FactorOut } from "./plannerApi";
import { useCalibration } from "./plannerQueries";
import { Clip, PlCard, PlNote, PlTable, StatusWord, type PlColumn } from "./plannerUi";
import { calibrationLight, errorText, factorText } from "./plannerView";

const LEVEL_TEXT: Record<string, string> = { global: "every node", node: "node", node_family: "node · family" };

/** What the in-use line says (mode, pinned, in use, active), as `compute planner calibration` prints it. */
function inUseLine(view: CalibrationView): string {
  const u = view.in_use;
  const v = (n: number | null | undefined) => (n == null ? "none" : `v${String(n)}`);
  return [
    `mode ${u.mode}`,
    ...(u.pinned_version != null ? [`pinned v${String(u.pinned_version)}`] : []),
    `in use ${v(u.in_use_version)}`,
    `active ${v(view.active_version)}`,
  ].join(" · ");
}

function termsHint(view: CalibrationView): string {
  const never = Object.entries(view.not_calibrated)
    .map(([term, why]) => `${term} (${why})`)
    .join(", ");
  const scaled = Object.entries(view.term_phase)
    .map(([term, phase]) => `${term} ↔ ${phase}`)
    .join(", ");
  return `Never scaled: ${never}. Scaled against the measured phase: ${scaled}.`;
}

function factorColumns(phase: Readonly<Record<string, string>>, nameOf: (id: string) => string): PlColumn<FactorOut>[] {
  return [
    {
      id: "term",
      header: "Term",
      cell: (f) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-mono text-[0.8125rem] text-fg">{f.term}</span>
          {phase[f.term] !== undefined && <span className="truncate font-mono text-2xs text-fg-subtle">↔ {phase[f.term]}</span>}
        </span>
      ),
    },
    {
      id: "level",
      header: "Key level",
      hint: "What the factor is fitted per: every node, one node, or one node and benchmark family.",
      cell: (f) => <span className="text-fg-muted">{LEVEL_TEXT[f.key_level] ?? f.key_level}</span>,
    },
    {
      id: "node",
      header: "Node",
      cell: (f) =>
        f.node_id == null ? (
          <span className="text-fg-subtle">all nodes</span>
        ) : (
          <span className="flex min-w-0 flex-col" title={f.node_id}>
            <span className="truncate">{nameOf(f.node_id)}</span>
            {nameOf(f.node_id) !== f.node_id && <span className="sr-only">{f.node_id}</span>}
          </span>
        ),
    },
    { id: "family", header: "Family", cell: (f) => (f.family == null ? dash : <span className="font-mono text-[0.8125rem]">{f.family}</span>) },
    {
      id: "factor",
      header: "Factor",
      mono: true,
      cell: (f) => <span className={cn(Math.abs(f.factor - 1) < 0.005 && "text-fg-muted")}>{factorText(f.factor)}</span>,
    },
    { id: "n", header: "n", hint: "Matched runs the factor was fitted from.", mono: true, cell: (f) => String(f.n) },
    { id: "median", header: "Median A/P", full: "Median actual / predicted", mono: true, cell: (f) => f.median.toFixed(3) },
  ];
}

function FactorsCard({ view }: { view: CalibrationView }) {
  const nameOf = useNodeNames();
  const shown = view.calibration;
  const meta = (
    <div className="flex items-baseline gap-8">
      <PlNote testId="calibration-in-use" title="What the planner uses now: the configured mode, a pinned version, the version in use and the active one.">
        {inUseLine(view)}
      </PlNote>
      <PlNote testId="calibration-terms" title={termsHint(view)}>
        {`${String(Object.keys(view.term_phase).length)} terms scaled · ${String(Object.keys(view.not_calibrated).length)} never`}
      </PlNote>
    </div>
  );
  return (
    <PlCard
      title={
        shown == null ? (
          "Calibration factors"
        ) : (
          <span className="inline-flex items-baseline gap-4">
            <span>Factors · v{shown.version}</span>
            <StatusWord light={calibrationLight(shown.status)} className="text-[0.875rem] font-normal text-fg-muted">
              {shown.status}
            </StatusWord>
          </span>
        )
      }
      label="Calibration factors"
      hint="Each factor scales the planner's estimate of a term: actual ≈ factor × predicted, fitted per key (every node, one node, or one node and family) from the matched runs; n is how many."
      meta={meta}
      className="nd-glass pl-fill flex flex-col"
    >
      {shown == null ? (
        <EmptyState title="No calibration yet" className="m-8">
          The planner predicts with its uncalibrated estimates until enough finished runs are matched (refreshed every
          30 min, or now with <code className="font-mono">compute planner calibrate</code>).
        </EmptyState>
      ) : !shown.readable ? (
        <p role="alert" className="px-9 py-6 text-sm text-status-failed">
          The factors of v{shown.version} could not be read, so the planner never uses this version.
        </p>
      ) : (
        <PlTable
          caption={`Calibration factors of version ${String(shown.version)}`}
          columns={factorColumns(view.term_phase, nameOf)}
          rows={shown.factors}
          rowKey={(f) => `${f.term}|${f.key_level}|${f.node_id ?? ""}|${f.family ?? ""}`}
          empty="This version has no factors."
          className="pl-factors flex-1"
        />
      )}
    </PlCard>
  );
}

/** The gate's two numbers per version: the fit's held-out error (honey) and the active one's then (grey). */
function ErrorChart({ recent }: { recent: readonly CalibrationSummary[] }) {
  const ordered = [...recent].sort((a, b) => a.version - b.version);
  const point = (v: number, y: number) => ({ x: v, y, lo: y, hi: y, n: 1 });
  const series: LineSeries[] = [
    {
      key: "active",
      label: "Active then",
      className: "text-fg-subtle",
      points: ordered.flatMap((v) => (v.holdout_error_active == null ? [] : [point(v.version, v.holdout_error_active)])),
    },
    {
      key: "new",
      label: "This fit",
      className: "text-[#ffc93c]",
      strong: true,
      points: ordered.flatMap((v) => (v.holdout_error_new == null ? [] : [point(v.version, v.holdout_error_new)])),
    },
  ];
  const plotted = series.some((s) => s.points.length > 0);
  return (
    <PlCard
      title="Held-out error"
      hint="Mean |ln(predicted / actual)| on the newest fifth of matched runs, for each fitted version and for the version that was active when it was fitted. A fit goes live only if its error is at most 1.05 × the active one's."
      meta={<Legend items={series.map((s) => ({ key: s.key, label: s.label, className: s.className }))} />}
      plain
      className="shrink-0"
      testId="calibration-error-chart"
    >
      <div className="pl-chart pt-3">
        {plotted ? (
          <LineChart
            series={series}
            logX={false}
            xLabel="version"
            formatX={(v) => `v${String(v)}`}
            formatY={(v) => v.toFixed(2)}
            label={`Held-out error per calibration version: ${String(ordered.length)} versions; the numbers are in the versions table.`}
            height={210}
          />
        ) : (
          <p className="py-8 text-sm text-fg-muted">No version has a measured held-out error yet.</p>
        )}
      </div>
    </PlCard>
  );
}

function versionColumns(shown: number | null, select: (v: number) => void): PlColumn<CalibrationSummary>[] {
  return [
    {
      id: "version",
      header: "Version",
      cell: (v) => (
        <button
          type="button"
          className={cn(
            "font-mono text-[0.8125rem] underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline",
            v.version === shown ? "font-medium text-[#ffc93c]" : "text-fg",
          )}
          aria-pressed={v.version === shown}
          onClick={() => {
            select(v.version);
          }}
        >
          v{v.version}
        </button>
      ),
    },
    {
      id: "created",
      header: "Created",
      cell: (v) => (
        <time className="whitespace-nowrap text-fg-muted" dateTime={v.created_at} title={formatTime(v.created_at)}>
          {formatDayTime(v.created_at)}
        </time>
      ),
    },
    { id: "status", header: "Status", cell: (v) => <StatusWord light={calibrationLight(v.status)}>{v.status}</StatusWord> },
    {
      id: "error",
      header: "Held-out error",
      full: "Held-out error: this fit / the active one then",
      mono: true,
      cell: (v) => `${errorText(v.holdout_error_new)} / ${errorText(v.holdout_error_active)}`,
    },
    {
      id: "reason",
      header: "Reason",
      cell: (v) =>
        v.reason == null ? dash : <Clip text={v.reason} className="font-mono text-2xs text-fg-muted" />,
    },
  ];
}

function VersionsCard({ view, onSelect }: { view: CalibrationView; onSelect: (v: number | null) => void }) {
  const shown = view.calibration?.version ?? null;
  return (
    <PlCard
      title="Versions"
      hint="The latest fitted versions, newest first: the active one, the ones it superseded, and fits the held-out gate rejected (with why). Pick one to see its factors."
      meta={<PlNote>{`${String(view.recent.length)} fitted`}</PlNote>}
      className="nd-glass pl-versions-card flex min-h-0 flex-1 flex-col"
    >
      <PlTable
        caption="Calibration versions, newest first"
        columns={versionColumns(shown, onSelect)}
        rows={view.recent}
        rowKey={(v) => String(v.version)}
        empty="No calibration version has been fitted yet."
        className="pl-versions flex-1"
        selected={(v) => v.version === shown}
        onRowClick={(v) => {
          onSelect(v.version);
        }}
      />
    </PlCard>
  );
}

export function CalibrationTab({ version, onSelect }: { version: number | null; onSelect: (v: number | null) => void }) {
  const query = useCalibration(version);
  if (query.isPending) {
    return (
      <p role="status" className="py-8 text-center text-sm text-fg-muted">
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
  const view = query.data;
  return (
    <div className="grid grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)] gap-6">
      <FactorsCard view={view} />
      <div className="pl-fill flex min-h-0 flex-col gap-6">
        <ErrorChart recent={view.recent} />
        <VersionsCard view={view} onSelect={onSelect} />
      </div>
    </div>
  );
}
