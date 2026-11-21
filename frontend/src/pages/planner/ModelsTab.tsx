/**
 * The Planner page's **Models** tab (V0.15 G5b, D374): is the ML model better
 * where it is used, and silent where it is not?
 *
 * * **Model in use**: `auto` / `off` / `pinned`, the set planning now, why the
 *   serving set is not loaded, the dispatcher's live numbers (last predict
 *   time - wall clock, so a model near its budget can flip a decision to
 *   `predictor_error` - and predictor errors), the retrain task. Admins get
 *   **Train now** as the title row's round button (confirm dialog).
 * * **Model sets**: the history with status and reason (incl. the gate's
 *   rule, e.g. `regret_worse`); admins can **pin** / **unpin** (confirm).
 * * **Predicted vs actual** (admin; the route names every user's decisions):
 *   per term, a log-log scatter of the calibrated prediction (grey) and -
 *   where ML corrected the term - the ML prediction (honey) against the
 *   actual, with the y = x line; the planner's estimate error as a
 *   distribution; how often ML acted, by reason; the points as a table
 *   (screen readers, and checkable).
 * * **Selected set**: per term the cross-validation numbers (rows, selected
 *   algorithm, inner log error ± SE, interval width) next to the held-out
 *   slice's (model vs calibration vs reference log error, coverage, usable
 *   rate, rules failed) and the gate line with its regret check.
 *
 * Empty states: a controller without the registry (404), ML off, no `ml`
 * extra, no model yet. Selection lives in the URL (`?set=`).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type ReactNode } from "react";

import { describeError } from "../../api/errors";
import { useSession } from "../../api/hooks";
import { CheckIcon } from "../../app/icons";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { EmptyState, ErrorState } from "../../components/States";
import { formatDayTime, formatTime } from "../../components/jobs/format";
import { ListboxField } from "../../components/jobs/ListboxField";
import { useToast } from "../../components/toastContext";
import { cn } from "../../lib/cn";
import { Legend } from "../benchmarks/BenchCharts";
import { Light } from "../benchmarks/benchUi";
import { dash, useNodeNames } from "../benchmarks/benchShared";
import {
  PREDICTIONS_LIMITS,
  getMlPredictions,
  getMlSet,
  isMlUnavailable,
  isSetVersion,
  mlKeys,
  pinMlSet,
  startMlTrain,
  type MlModelsOut,
  type MlPredictionPointOut,
  type MlSetDetailOut,
  type MlSetOut,
  type PredictionsLimit,
} from "./mlApi";
import {
  busiestTerm,
  calibrationGapText,
  canPin,
  canUnpin,
  errorBins,
  estimateErrors,
  fallbackRows,
  gateText,
  inUseText,
  learnedText,
  liveText,
  mlState,
  plottedText,
  retrainText,
  scatterOf,
  secondsText,
  setLabel,
  setTone,
  share,
  termMetricRows,
  termsOf,
  unitOf,
  withinShare,
  type FallbackRow,
  type Scatter as ScatterData,
  type TermMetricsRow,
} from "./mlView";
import { ErrorHistogram, ScatterLog } from "./PlannerCharts";
import { useMlModels } from "./plannerQueries";
import { Clip, PlCard, PlNote, PlTable, StatusWord, type PlColumn } from "./plannerUi";
import { toneLight } from "./plannerView";

/** The estimate error band the page judges against (the planner's "within 15 %"). */
const BAND_PCT = 15;

/* ------------------------------------------------------------ actions */

type Action = { kind: "train" } | { kind: "pin" | "unpin"; set: MlSetOut };

function actionTitle(a: Action): string {
  if (a.kind === "train") return "Train a new model set now?";
  return a.kind === "pin" ? `Pin set ${setLabel(a.set.set_version)}?` : `Unpin set ${setLabel(a.set.set_version)}?`;
}

function actionText(a: Action): string {
  if (a.kind === "train") {
    return (
      "The controller builds the dataset from the recorded runs and trains in the background (this can take minutes). " +
      "The new set is used only if it passes the activation gate (held-out error and regret)."
    );
  }
  if (a.kind === "pin") {
    return (
      `The planner will use ${a.set.set_version} for every decision until it is unpinned, even when a newer set ` +
      "is activated. Use it to roll back to a set you trust."
    );
  }
  return `${a.set.set_version} stops being pinned: the planner goes back to the active set (mode auto).`;
}

function useMlAction(onDone: () => void) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationKey: [...mlKeys.all, "action"],
    mutationFn: async (a: Action): Promise<string> => {
      if (a.kind === "train") {
        const out = await startMlTrain();
        return out.started ? "Retrain started." : "A retrain was already running.";
      }
      const out = await pinMlSet(a.set.set_version, a.kind === "pin");
      return `Set ${setLabel(out.set_version)} is ${out.status}.`;
    },
    onSuccess: (message, a) => {
      void client.invalidateQueries({ queryKey: mlKeys.all });
      toast({ title: a.kind === "train" ? "Training" : a.kind === "pin" ? "Pinned" : "Unpinned", description: message, tone: "ok" });
      onDone();
    },
    onError: (error, a) => {
      const { message, code } = describeError(error);
      toast({
        title: `${a.kind === "train" ? "Train" : a.kind === "pin" ? "Pin" : "Unpin"} refused`,
        description: code !== undefined ? `${message} (${code})` : message,
        tone: "failed",
      });
      onDone();
    },
  });
}

function ConfirmAction({ action, onClose, onConfirm, pending }: { action: Action | null; onClose: () => void; onConfirm: () => void; pending: boolean }) {
  return (
    <Dialog
      open={action !== null}
      onClose={() => {
        if (!pending) onClose();
      }}
      title={action === null ? "" : actionTitle(action)}
      description={action === null ? undefined : actionText(action)}
      footer={
        <>
          <Button onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="primary" loading={pending} onClick={onConfirm}>
            {action?.kind === "train" ? "Train now" : action?.kind === "pin" ? "Pin" : "Unpin"}
          </Button>
        </>
      }
    />
  );
}

const RetrainIcon = () => (
  <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 11a8 8 0 0 0-14.3-4.9L4 8" />
    <path d="M4 3v5h5" />
    <path d="M4 13a8 8 0 0 0 14.3 4.9L20 16" />
    <path d="M20 21v-5h-5" />
  </svg>
);

/**
 * **Train now** (admins): the title row's round honey button with its
 * confirmation; disabled while a retrain runs or the controller cannot train.
 */
export function TrainButton() {
  const query = useMlModels();
  const [action, setAction] = useState<Action | null>(null);
  const run = useMlAction(() => {
    setAction(null);
  });
  const r = query.data?.retrain;
  if (r === undefined) return null;
  const title = !r.available ? "Train now (the controller has no ml extra)" : r.running ? "A retrain is running" : "Train now";
  return (
    <>
      <button
        type="button"
        className="jb-submit-btn pl-round"
        aria-label="Train now"
        title={title}
        disabled={!r.available || r.running}
        onClick={() => {
          setAction({ kind: "train" });
        }}
      >
        {r.running ? <span aria-hidden="true" className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" /> : <RetrainIcon />}
      </button>
      <ConfirmAction
        action={action}
        pending={run.isPending}
        onClose={() => {
          setAction(null);
        }}
        onConfirm={() => {
          if (action !== null) run.mutate(action);
        }}
      />
    </>
  );
}

/* ------------------------------------------------------------ in use */

const STATE_WORD: Record<ReturnType<typeof mlState>, { tone: string; text: string }> = {
  in_use: { tone: "ok", text: "ML in use" },
  off: { tone: "neutral", text: "ML off" },
  no_model: { tone: "neutral", text: "no model yet" },
  not_loaded: { tone: "degraded", text: "set not loaded" },
  unavailable_extra: { tone: "degraded", text: "ml extra missing" },
};

function InUse({ models }: { models: MlModelsOut }) {
  const state = mlState(models);
  const word = STATE_WORD[state];
  const r = models.retrain;
  return (
    <PlCard
      title="Model in use"
      hint="What the planner predicts with now: the configured mode, the set it serves and the dispatcher's live numbers; the retrain task builds a new set from the recorded runs."
      meta={<StatusWord light={toneLight(word.tone)}>{word.text}</StatusWord>}
      plain
      className="pl-top pl-meta-left flex flex-col"
    >
      <div className="pl-body flex min-h-0 flex-1 flex-col justify-center gap-4 pb-4" data-testid="ml-in-use">
        <p className="text-[1.0625rem] leading-7 text-fg">{inUseText(models)}</p>
        <PlNote>{liveText(models.in_use)}</PlNote>
        <div className="nd-hair-t order-last mt-6 flex flex-col gap-1.5 pt-6">
          <p className="inline-flex items-center gap-2.5 text-sm text-fg">
            <Light className={r.running ? "bg-status-busy" : r.available ? "bg-fg-subtle" : "bg-status-degraded"} small breathe={r.running} />
            <span>{retrainText(r)}</span>
          </p>
          {r.available && (
            <PlNote className="pl-4">{`Retrains: ${String(r.retrains)}, failures ${String(r.failures)}; automatic every ${String(Math.round(r.interval_s / 60))} min.`}</PlNote>
          )}
        </div>
        {state === "off" && (
          <p className="text-xs text-fg-muted">
            Set <code className="font-mono">[controller.planner] ml = &quot;auto&quot;</code> to let the planner use the active model set.
          </p>
        )}
        {state === "unavailable_extra" && (
          <p className="text-xs text-fg-muted">
            Install the controller with the ml extra (<code className="font-mono">uv sync --extra ml</code>) to train models; until
            then the planner uses the calibration only.
          </p>
        )}
        {state === "no_model" && (
          <p className="text-xs text-fg-muted">
            No set has passed the activation gate yet. Sets are trained from recorded runs every interval, or now with{" "}
            <code className="font-mono">compute planner ml train</code>.
          </p>
        )}
      </div>
    </PlCard>
  );
}

/* ------------------------------------------------------------ sets */

/** A cell's text on at most two lines, the full text in its tooltip. */
function Wrap2({ text, className }: { text: string; className?: string }) {
  return (
    <span className={cn("line-clamp-2 cursor-help leading-5 whitespace-normal [overflow-wrap:anywhere]", className)} title={text}>
      {text}
    </span>
  );
}

function setLight(status: string): string {
  return toneLight(setTone(status));
}

function setColumns(shown: string | null, onSelect: (v: string) => void, admin: boolean, ask: (a: Action) => void): PlColumn<MlSetOut>[] {
  const cols: PlColumn<MlSetOut>[] = [
    {
      id: "set",
      header: "Set",
      cell: (s) => (
        <span className="flex flex-col">
          <button
            type="button"
            className={cn(
              "w-fit font-mono text-[0.8125rem] underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline",
              s.set_version === shown ? "font-medium text-[#ffc93c]" : "text-fg",
            )}
            aria-pressed={s.set_version === shown}
            aria-label={`Show set ${s.set_version}`}
            title={s.set_version}
            onClick={() => {
              onSelect(s.set_version);
            }}
          >
            {setLabel(s.set_version)}
          </button>
          <time className="font-mono text-2xs whitespace-nowrap text-fg-muted" dateTime={s.created_at} title={formatTime(s.created_at)}>
            {formatDayTime(s.created_at)}
          </time>
        </span>
      ),
    },
    { id: "status", header: "Status", cell: (s) => <StatusWord light={setLight(s.status)}>{s.status}</StatusWord> },
    {
      id: "learned",
      header: "Learned terms",
      cell: (s) => <Wrap2 text={learnedText(s.members)} className="font-mono text-[0.8125rem] text-fg-muted" />,
    },
    {
      id: "reason",
      header: "Reason",
      cell: (s) => (s.reason == null ? dash : <Wrap2 text={s.reason} className="font-mono text-2xs text-fg-muted" />),
    },
  ];
  if (admin) {
    cols.push({
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (s) =>
        canUnpin(s) ? (
          <button type="button" className="bm-act" aria-label={`Unpin set ${s.set_version}`} onClick={() => { ask({ kind: "unpin", set: s }); }}>
            Unpin
          </button>
        ) : canPin(s) ? (
          <button type="button" className="bm-act" aria-label={`Pin set ${s.set_version}`} onClick={() => { ask({ kind: "pin", set: s }); }}>
            Pin
          </button>
        ) : (
          dash
        ),
    });
  }
  return cols;
}

const METRIC_COLUMNS: PlColumn<TermMetricsRow>[] = [
  { id: "term", header: "Term", mono: true, cell: (r) => r.term },
  { id: "algorithm", header: "Algorithm", mono: true, cell: (r) => <Clip text={r.algorithm} /> },
  { id: "rows", header: "CV rows", full: "Cross-validation rows", mono: true, cell: (r) => r.rows },
  { id: "families", header: "Families", mono: true, cell: (r) => r.families },
  { id: "selected", header: "Selected", hint: "The algorithm cross-validation selected.", mono: true, cell: (r) => <Clip text={r.selected} /> },
  { id: "inner", header: "CV error", full: "CV log error", hint: "Cross-validation log error ± its standard error.", mono: true, cell: (r) => r.innerMale },
  { id: "width", header: "Interval", full: "Interval width", mono: true, cell: (r) => r.intervalWidth },
  { id: "held", header: "Held rows", full: "Held-out rows", mono: true, cell: (r) => r.heldRows },
  {
    id: "male",
    header: "Error m / c / r",
    full: "Held-out error m / c / r",
    hint: "Held-out log error: model / calibration / reference",
    mono: true,
    cell: (r) => `${r.maleModel} / ${r.maleCalibration} / ${r.maleReference}`,
  },
  { id: "coverage", header: "Coverage", hint: "Share of held-out actuals inside the prediction interval.", mono: true, cell: (r) => r.coverage },
  { id: "usable", header: "Usable", hint: "Share of held-out rows the model could predict.", mono: true, cell: (r) => r.usable },
  { id: "failed", header: "Failed", full: "Rules failed", mono: true, cell: (r) => <Clip text={r.failed} /> },
  { id: "reason", header: "Reason", cell: (r) => <Wrap2 text={r.reason} className="font-mono text-2xs text-fg-muted" /> },
];

function SetDetail({ version }: { version: string }) {
  const query = useQuery({ queryKey: mlKeys.set(version), queryFn: ({ signal }) => getMlSet(version, signal) });
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading set {setLabel(version)}…
      </p>
    );
  }
  if (query.isError) {
    return <ErrorState title={`Could not load set ${setLabel(version)}`} error={query.error} onRetry={() => void query.refetch()} />;
  }
  const set: MlSetDetailOut = query.data;
  return (
    <PlCard
      title={
        <span className="inline-flex items-baseline gap-4">
          <span title={set.set_version}>Set {setLabel(set.set_version)}</span>
          <StatusWord light={setLight(set.status)} className="text-[0.875rem] font-normal text-fg-muted">
            {set.status}
          </StatusWord>
        </span>
      }
      label={`Set ${setLabel(set.set_version)}`}
      hint="Per term: the cross-validation numbers of the fit next to the held-out slice the activation gate judged (model vs calibration vs reference log error, interval coverage, usable rate, the rules it failed)."
      meta={
        <PlNote title={`${set.set_version}\ndataset ${set.dataset_digest}`}>
          {`dataset ${set.dataset_digest.slice(7, 19)} · features v${String(set.feature_version)} · created ${formatTime(set.created_at)}`}
        </PlNote>
      }
      testId="ml-set-detail"
    >
      <p className="pl-body pt-1 pb-2 text-[0.9375rem] leading-6 text-fg" data-testid="ml-gate">
        {gateText(set.gate)}
      </p>
      {set.reason != null && <PlNote className="pl-body pb-2">{set.reason}</PlNote>}
      <PlTable
        caption={`Per-term numbers of set ${set.set_version}: cross-validation and held-out slice`}
        columns={METRIC_COLUMNS}
        rows={termMetricRows(set.members, set.gate)}
        rowKey={(r) => r.term}
        empty="This set has no members."
        className="pl-metrics"
      />
    </PlCard>
  );
}

/* ------------------------------------------------------------ predictions */

/** Both charts share one height, so they sit side by side at equal size. */
const CHART_H = 300;

function ScatterPlot({ data }: { data: ScatterData }) {
  const nameOf = useNodeNames();
  const unit = unitOf(data.term);
  const title = `${data.term}: predicted vs actual (${unit}, log axes)`;
  if (data.domain === null) {
    return (
      <p className="py-10 text-sm text-fg-muted">
        No point of {data.term} can be plotted (every value is missing or zero). <span data-testid="ml-plotted">{plottedText(data)}</span>.
      </p>
    );
  }
  return (
    <figure className="flex min-w-0 flex-col" data-testid="ml-scatter">
      <figcaption className="pl-chart-cap">
        <span className="nd-label truncate">{title}</span>
        <Legend
          items={[
            { key: "cal", label: `calibration ${String(data.calibration.length)}`, className: "text-fg-muted" },
            { key: "ml", label: `ML, where used ${String(data.ml.length)}`, className: "text-[#ffc93c]" },
          ]}
        />
      </figcaption>
      <ScatterLog
        calibration={data.calibration}
        ml={data.ml}
        domain={data.domain}
        unit={unit}
        nameOf={nameOf}
        height={CHART_H}
        label={`${title}: ${String(data.calibration.length)} calibration points and ${String(data.ml.length)} ML points; the numbers are in the points table.`}
      />
      <PlNote
        className="mt-3 truncate"
        testId="ml-plotted"
        title="The dashed line is y = x, a perfect prediction. A missing or zero value has no place on log axes, so its point is counted but not drawn."
      >
        {plottedText(data)}
        {data.dropped > 0 && ` · ${String(data.dropped)} point(s) not plotted`}
        {data.noCalibrated > 0 && ` · ${String(data.noCalibrated)} without a calibrated value (only their ML point, if any, is shown)`}
      </PlNote>
    </figure>
  );
}

function ErrorDistribution({ points, term, note }: { points: readonly MlPredictionPointOut[]; term: string; note: ReactNode }) {
  const errors = estimateErrors(points, term);
  const within = withinShare(errors, BAND_PCT);
  return (
    <figure className="flex min-w-0 flex-col" data-testid="ml-error-distribution">
      <figcaption
        className="pl-chart-cap cursor-help"
        title={`The planner's estimate for ${term} (ML where it acted, else the calibrated one) against the measured time: (estimate / actual - 1) in %. The dashed lines mark ±${String(BAND_PCT)} %.`}
      >
        <span className="nd-label truncate">estimate error · {term}</span>
        <span className="flex items-baseline gap-2">
          <span className="ov-figure text-[1.5rem] font-light text-fg">{within === null ? "—" : (within * 100).toFixed(0)}</span>
          {within !== null && <span className="text-sm text-fg-muted">%</span>}
          <span className="nd-label ml-1">within ±{BAND_PCT} %</span>
        </span>
      </figcaption>
      {errors.length === 0 ? (
        <p className="pl-chart-empty text-sm text-fg-muted">No point of {term} has both an estimate and a measured time.</p>
      ) : (
        <ErrorHistogram
          bins={errorBins(errors)}
          band={BAND_PCT}
          height={CHART_H}
          label={`Estimate error of ${term}: ${String(errors.length)} points, ${within === null ? "—" : share(within)} within ±${String(BAND_PCT)} %.`}
        />
      )}
      {note}
    </figure>
  );
}

function PointsTable({ points, term }: { points: readonly MlPredictionPointOut[]; term: string }) {
  const [open, setOpen] = useState(false);
  const nameOf = useNodeNames();
  const rows = points.filter((p) => p.term === term);
  const unit = unitOf(term);
  const columns: PlColumn<MlPredictionPointOut>[] = [
    { id: "decision", header: "decision", mono: true, cell: (p) => `#${String(p.decision_id)}` },
    { id: "node", header: "node", cell: (p) => <Clip text={nameOf(p.node_id)} /> },
    { id: "family", header: "family", mono: true, cell: (p) => <Clip text={p.family ?? "—"} /> },
    { id: "analytical", header: "analytical", mono: true, cell: (p) => secondsText(p.analytical_s, unit) },
    { id: "calibrated", header: "calibrated", mono: true, cell: (p) => secondsText(p.calibrated_s, unit) },
    { id: "ml", header: "ML", mono: true, cell: (p) => secondsText(p.ml_s, unit) },
    { id: "actual", header: "actual", mono: true, cell: (p) => secondsText(p.actual_s, unit) },
    { id: "used", header: "ML used", cell: (p) => <Clip text={p.used ? "yes" : `no${p.reason != null ? ` (${p.reason})` : ""}`} /> },
    { id: "set", header: "set", mono: true, cell: (p) => (p.ml_model_version == null ? "none" : setLabel(p.ml_model_version)) },
  ];
  return (
    <details
      className="pl-points mt-6"
      onToggle={(e) => {
        setOpen(e.currentTarget.open);
      }}
    >
      <summary className="jb-clear-btn nd-hair-t mx-9 cursor-pointer pt-6 text-sm">Points as a table ({rows.length})</summary>
      {open && (
        <PlTable
          caption={`Predicted vs actual points of ${term}`}
          columns={columns}
          rows={rows}
          rowKey={(p) => `${String(p.decision_id)}|${p.node_id}|${p.term}`}
          className="pl-pointsTable mt-2"
          scrollClassName="pl-points-scroll"
          testId="ml-points"
        />
      )}
    </details>
  );
}

const FALLBACK_COLUMNS = (title: string): PlColumn<FallbackRow>[] => [
  { id: "what", header: title, cell: (r) => <Clip text={r.label} /> },
  { id: "n", header: "Points", mono: true, align: "right", cell: (r) => String(r.count) },
  { id: "share", header: "Share", mono: true, align: "right", cell: (r) => share(r.share) },
];

function Toggle({ on, disabled, onChange, children }: { on: boolean; disabled: boolean; onChange: (v: boolean) => void; children: string }) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={on}
      disabled={disabled}
      className="pl-toggle"
      onClick={() => {
        onChange(!on);
      }}
    >
      <span aria-hidden="true" className={cn("pl-toggle-box", on && "pl-toggle-on")}>
        {on && <CheckIcon />}
      </span>
      <span className="whitespace-nowrap">{children}</span>
    </button>
  );
}

function Predictions({ serving }: { serving: string | null }) {
  const [limit, setLimit] = useState<PredictionsLimit>(PREDICTIONS_LIMITS[0]);
  const [term, setTerm] = useState<string | null>(null);
  const [onlyServing, setOnlyServing] = useState(false);
  const ids = { term: useId(), limit: useId() };
  const query = useQuery({ queryKey: mlKeys.predictions(limit), queryFn: ({ signal }) => getMlPredictions(limit, signal) });
  const all = query.data?.points ?? [];
  const points = onlyServing && serving !== null ? all.filter((p) => p.ml_model_version === serving) : all;
  const terms = termsOf(points);
  const shown = term !== null && terms.includes(term) ? term : busiestTerm(points);
  const controls = (
    <div className="flex flex-nowrap items-end gap-x-8">
      <div className="flex w-40 flex-col gap-1">
        <label htmlFor={ids.term} className="nd-label">
          Term
        </label>
        <ListboxField
          id={ids.term}
          value={shown ?? ""}
          options={terms.map((t) => ({ value: t, label: t }))}
          disabled={terms.length === 0}
          onChange={(v) => {
            setTerm(v);
          }}
        />
      </div>
      <div className="flex w-28 flex-col gap-1">
        <label htmlFor={ids.limit} className="nd-label">
          Newest runs
        </label>
        <ListboxField
          id={ids.limit}
          value={String(limit)}
          options={PREDICTIONS_LIMITS.map((n) => ({ value: String(n), label: String(n) }))}
          onChange={(v) => {
            setLimit(Number(v) as PredictionsLimit);
          }}
        />
      </div>
      <div className="pb-2.5">
        <Toggle on={onlyServing} disabled={serving === null} onChange={setOnlyServing}>
          {`Only decisions planned with the serving set${serving !== null ? ` (${setLabel(serving)})` : ""}`}
        </Toggle>
      </div>
    </div>
  );
  let body;
  if (query.isPending) {
    body = (
      <p role="status" className="pl-body py-8 text-sm text-fg-muted">
        Loading predictions…
      </p>
    );
  } else if (query.isError) {
    body = (
      <div className="pl-body pt-2">
        <ErrorState title="Could not load predicted vs actual" error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  } else {
    const gap = calibrationGapText(query.data);
    const note = (
      <div className="mt-3 flex min-w-0 flex-col gap-1">
        <PlNote className="truncate">{`${String(query.data.decisions_read)} matched runs read · ${String(points.length)} points`}</PlNote>
        {gap !== null && (
          <PlNote className="truncate text-status-degraded" testId="ml-calibration-gap" title={gap}>
            {gap}
          </PlNote>
        )}
      </div>
    );
    body =
      shown === null ? (
        <div className="pl-body flex flex-col gap-4 pt-3">
          {note}
          <EmptyState title="No predicted-vs-actual points yet">
            Points appear once dispatched decisions have finished runs with a measured time for a learnable term.
          </EmptyState>
        </div>
      ) : (
        <>
          <div className="pl-body pl-split2 grid grid-cols-2 gap-x-14 pt-5">
            <ScatterPlot data={scatterOf(points, shown)} />
            <ErrorDistribution points={points} term={shown} note={note} />
          </div>
          <div className="nd-hair-t mx-9 mt-8 grid grid-cols-2 gap-x-14 pt-5">
            <PlTable caption={`How often ML acted for ${shown}`} columns={FALLBACK_COLUMNS(shown)} rows={fallbackRows(points, shown)} rowKey={(r) => r.key} className="pl-mini" />
            <PlTable caption="How often ML acted, all terms" columns={FALLBACK_COLUMNS("All terms")} rows={fallbackRows(points)} rowKey={(r) => r.key} className="pl-mini" />
          </div>
          <PointsTable points={points} term={shown} />
        </>
      );
  }
  return (
    <PlCard
      title="Predicted vs actual"
      hint="Every matched decision's predicted time per term against what was measured: grey the calibrated estimate for every point, honey the ML prediction where ML corrected the term."
      meta={controls}
      plain
      testId="ml-predictions"
    >
      {body}
    </PlCard>
  );
}

/* ------------------------------------------------------------ tab */

function ModelsBody({ models, admin, selected, onSelect }: { models: MlModelsOut; admin: boolean; selected: string | null; onSelect: (v: string) => void }) {
  const [action, setAction] = useState<Action | null>(null);
  const run = useMlAction(() => {
    setAction(null);
  });
  const shown =
    selected !== null && isSetVersion(selected)
      ? selected
      : (models.in_use.in_use_set ?? models.serving_set ?? models.sets[0]?.set_version ?? null);
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.75fr)] gap-6">
        <InUse models={models} />
        <PlCard
          title="Model sets"
          hint="The newest trained sets, newest first: the active one, retired (superseded) ones, sets the activation gate rejected (with its rule), a pinned override. Pick one to see its numbers."
          meta={
            admin ? (
              <PlNote>{`${String(models.sets.length)} sets`}</PlNote>
            ) : (
              <PlNote title="Pin, unpin and train are for admins.">{`${String(models.sets.length)} sets · read-only view`}</PlNote>
            )
          }
          className="pl-top flex flex-col"
        >
          <PlTable
            caption="Model sets, newest first"
            columns={setColumns(shown, onSelect, admin, setAction)}
            rows={models.sets}
            rowKey={(s) => s.set_version}
            empty="No model set has been trained yet."
            className={cn("pl-sets flex-1", admin && "pl-sets-admin")}
            selected={(s) => s.set_version === shown}
            onRowClick={(s) => {
              onSelect(s.set_version);
            }}
          />
        </PlCard>
      </div>
      {admin ? (
        <Predictions serving={models.serving_set} />
      ) : (
        <PlCard title="Predicted vs actual" plain>
          <p className="pl-body pt-2 text-sm text-fg-muted">Predicted vs actual is shown to admins only: its points name every user&apos;s decisions and nodes.</p>
        </PlCard>
      )}
      {shown !== null && <SetDetail version={shown} />}
      <ConfirmAction
        action={action}
        pending={run.isPending}
        onClose={() => {
          setAction(null);
        }}
        onConfirm={() => {
          if (action !== null) run.mutate(action);
        }}
      />
    </div>
  );
}

export function ModelsTab({ selected, onSelect }: { selected: string | null; onSelect: (v: string) => void }) {
  const session = useSession();
  const admin = session.data?.user.role === "admin";
  const query = useMlModels();
  if (query.isPending) {
    return (
      <p role="status" className="py-8 text-center text-sm text-fg-muted">
        Loading models…
      </p>
    );
  }
  if (query.isError) {
    if (isMlUnavailable(query.error)) {
      return (
        <EmptyState title="ML models are not available">
          This controller has no model registry (it arrives with V0.15). The planner uses its calibration only.
        </EmptyState>
      );
    }
    return <ErrorState title="Could not load the models" error={query.error} onRetry={() => void query.refetch()} />;
  }
  return <ModelsBody models={query.data} admin={admin} selected={selected} onSelect={onSelect} />;
}
