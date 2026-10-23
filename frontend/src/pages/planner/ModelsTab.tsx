/**
 * The Planner page's **Models** tab (V0.15 G5b, D374): is the ML model better
 * where it is used, and silent where it is not?
 *
 * * **In use**: `auto` / `off` / `pinned`, the set planning now, why the
 *   serving set is not loaded, the dispatcher's live numbers (last predict
 *   time - wall clock, so a model near its budget can flip a decision to
 *   `predictor_error` - and predictor errors), the retrain task; admins get
 *   **Train now** (confirm dialog).
 * * **Model sets**: the history with status and reason (incl. the gate's
 *   rule, e.g. `regret_worse`); admins can **pin** / **unpin** (confirm).
 * * **Selected set**: per term the cross-validation numbers (rows, selected
 *   algorithm, inner log error ± SE, interval width) next to the held-out
 *   slice's (model vs calibration vs reference log error, coverage, usable
 *   rate, rules failed) and the gate line with its regret check.
 * * **Predicted vs actual** (admin; the route names every user's decisions):
 *   per term, a log-log scatter of the calibrated prediction and - where ML
 *   corrected the term - the ML prediction against the actual, with the
 *   y = x line; the points as a table (screen readers, and checkable); and
 *   the fallback rate by reason over the listed decisions.
 *
 * Empty states: a controller without the registry (404), ML off, no `ml`
 * extra, no model yet. Selection lives in the URL (`?set=`).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { CartesianGrid, ReferenceLine, Scatter, ScatterChart, XAxis, YAxis } from "recharts";

import { describeError } from "../../api/errors";
import { useSession } from "../../api/hooks";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatTime } from "../../components/jobs/format";
import { useToast } from "../../components/toastContext";
import { cn } from "../../lib/cn";
import {
  PREDICTIONS_LIMITS,
  getMlModels,
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
  canPin,
  canUnpin,
  fallbackRows,
  gateText,
  inUseText,
  learnedText,
  liveText,
  mlState,
  retrainText,
  scatterOf,
  secondsText,
  setLabel,
  setTone,
  share,
  termMetricRows,
  termsOf,
  unitOf,
  type FallbackRow,
  type Scatter as ScatterData,
  type TermMetricsRow,
} from "./mlView";


/** While a retrain runs, the list is re-read this often (ms). */
const RETRAIN_POLL_MS = 3000;

const select = "h-8 max-w-full rounded-md border border-border-strong bg-surface px-2 text-sm text-fg";

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

/* ------------------------------------------------------------ in use */

const STATE_BADGE: Record<ReturnType<typeof mlState>, { tone: "ok" | "neutral" | "degraded" | "failed"; text: string }> = {
  in_use: { tone: "ok", text: "ML in use" },
  off: { tone: "neutral", text: "ML off" },
  no_model: { tone: "neutral", text: "no model yet" },
  not_loaded: { tone: "degraded", text: "set not loaded" },
  unavailable_extra: { tone: "degraded", text: "ml extra missing" },
};

function InUse({ models, admin, onTrain }: { models: MlModelsOut; admin: boolean; onTrain: () => void }) {
  const state = mlState(models);
  const badge = STATE_BADGE[state];
  const r = models.retrain;
  return (
    <Card
      title="Model in use"
      actions={
        <>
          <Badge tone={badge.tone}>{badge.text}</Badge>
          {admin && (
            <Button size="sm" variant="primary" onClick={onTrain} disabled={!r.available || r.running} loading={r.running}>
              Train now
            </Button>
          )}
        </>
      }
    >
      <div className="flex flex-col gap-1 text-sm" data-testid="ml-in-use">
        <p>{inUseText(models)}</p>
        <p className="font-mono text-xs text-fg-muted">{liveText(models.in_use)}</p>
        <p className="text-xs text-fg-muted">
          {retrainText(r)} {r.available && `Retrains: ${String(r.retrains)}, failures ${String(r.failures)}; automatic every ${String(Math.round(r.interval_s / 60))} min.`}
        </p>
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
    </Card>
  );
}

/* ------------------------------------------------------------ sets */

function setColumns(shown: string | null, onSelect: (v: string) => void, admin: boolean, ask: (a: Action) => void): Column<MlSetOut>[] {
  const cols: Column<MlSetOut>[] = [
    {
      id: "set",
      header: "Set",
      mono: true,
      cell: (s) => (
        <button
          type="button"
          className="font-mono text-accent underline-offset-2 hover:underline"
          aria-pressed={s.set_version === shown}
          aria-label={`Show set ${s.set_version}`}
          onClick={() => {
            onSelect(s.set_version);
          }}
        >
          {setLabel(s.set_version)}
        </button>
      ),
    },
    { id: "created", header: "Created", mono: true, cell: (s) => formatTime(s.created_at) },
    { id: "status", header: "Status", cell: (s) => <Badge tone={setTone(s.status)}>{s.status}</Badge> },
    { id: "learned", header: "Learned terms", mono: true, cell: (s) => learnedText(s.members) },
    { id: "reason", header: "Reason", cell: (s) => s.reason ?? "—" },
  ];
  if (admin) {
    cols.push({
      id: "actions",
      header: "Actions",
      cell: (s) =>
        canUnpin(s) ? (
          <Button size="sm" aria-label={`Unpin set ${s.set_version}`} onClick={() => { ask({ kind: "unpin", set: s }); }}>
            Unpin
          </Button>
        ) : canPin(s) ? (
          <Button size="sm" aria-label={`Pin set ${s.set_version}`} onClick={() => { ask({ kind: "pin", set: s }); }}>
            Pin
          </Button>
        ) : (
          "—"
        ),
    });
  }
  return cols;
}

const METRIC_COLUMNS: Column<TermMetricsRow>[] = [
  { id: "term", header: "Term", mono: true, cell: (r) => r.term },
  { id: "algorithm", header: "Algorithm", mono: true, cell: (r) => r.algorithm },
  { id: "rows", header: "CV rows", align: "right", mono: true, cell: (r) => r.rows },
  { id: "families", header: "Families", align: "right", mono: true, cell: (r) => r.families },
  { id: "selected", header: "Selected", mono: true, cell: (r) => r.selected },
  { id: "inner", header: "CV log error", align: "right", mono: true, cell: (r) => r.innerMale },
  { id: "width", header: "Interval width", align: "right", mono: true, cell: (r) => r.intervalWidth },
  { id: "held", header: "Held-out rows", align: "right", mono: true, cell: (r) => r.heldRows },
  { id: "male", header: "Held-out log error (model / calibration / reference)", align: "right", mono: true, cell: (r) => `${r.maleModel} / ${r.maleCalibration} / ${r.maleReference}` },
  { id: "coverage", header: "Coverage", align: "right", mono: true, cell: (r) => r.coverage },
  { id: "usable", header: "Usable", align: "right", mono: true, cell: (r) => r.usable },
  { id: "failed", header: "Rules failed", mono: true, cell: (r) => r.failed },
  { id: "reason", header: "Reason", cell: (r) => r.reason },
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
    <Card title={`Set ${setLabel(set.set_version)} (${set.status})`} flush data-testid="ml-set-detail">
      <div className="flex flex-col gap-1 px-4 py-3 text-xs text-fg-muted">
        <p className="font-mono break-all text-fg">{set.set_version}</p>
        <p>
          dataset <span className="font-mono">{set.dataset_digest.slice(0, 19)}</span> · features v{set.feature_version} · created{" "}
          {formatTime(set.created_at)}
          {set.reason != null && <> · {set.reason}</>}
        </p>
        <p className="text-sm text-fg" data-testid="ml-gate">
          {gateText(set.gate)}
        </p>
      </div>
      <Table
        caption={`Per-term numbers of set ${set.set_version}: cross-validation and held-out slice`}
        columns={METRIC_COLUMNS}
        rows={termMetricRows(set.members, set.gate)}
        rowKey={(r) => r.term}
        empty="This set has no members."
      />
    </Card>
  );
}

/* ------------------------------------------------------------ predictions */

const WIDTH = 560;
const HEIGHT = 360;
const TICK = { fill: "currentColor", fontSize: 11 };

function axis(v: number): string {
  return Number.isFinite(v) ? String(Number(v.toPrecision(2))) : "";
}

function ScatterPlot({ data }: { data: ScatterData }) {
  const unit = unitOf(data.term);
  const title = `${data.term}: predicted vs actual (${unit}, log axes)`;
  if (data.domain === null) {
    return <p className="text-sm text-fg-muted">No point of {data.term} can be plotted (every value is missing or zero).</p>;
  }
  const [lo, hi] = data.domain;
  return (
    <figure className="flex flex-col gap-2" data-testid="ml-scatter">
      <figcaption className="text-sm font-medium text-fg">{title}</figcaption>
      <ul className="flex flex-wrap gap-x-4 text-xs text-fg-muted" aria-label="Series">
        <li className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="size-2.5 rounded-full bg-status-unknown" />
          calibration ({data.calibration.length})
        </li>
        <li className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="size-2.5 rounded-full bg-accent" />
          ML, where used ({data.ml.length})
        </li>
        <li>dashed: y = x (a perfect prediction)</li>
      </ul>
      <div
        role="img"
        aria-label={`${title}: ${String(data.calibration.length)} calibration points and ${String(data.ml.length)} ML points; the numbers are in the table below.`}
        className="overflow-x-auto text-fg-muted"
      >
        <ScatterChart width={WIDTH} height={HEIGHT} margin={{ top: 12, right: 24, bottom: 32, left: 16 }}>
          <CartesianGrid stroke="currentColor" strokeOpacity={0.15} />
          <XAxis
            type="number"
            dataKey="x"
            name="predicted"
            scale="log"
            domain={[lo, hi]}
            allowDataOverflow
            tick={TICK}
            tickFormatter={axis}
            stroke="currentColor"
            label={{ value: `predicted (${unit})`, position: "insideBottom", offset: -18, fill: "currentColor", fontSize: 11 }}
          />
          <YAxis
            type="number"
            dataKey="y"
            name="actual"
            scale="log"
            domain={[lo, hi]}
            allowDataOverflow
            tick={TICK}
            tickFormatter={axis}
            stroke="currentColor"
            width={64}
            label={{ value: `actual (${unit})`, angle: -90, position: "insideLeft", fill: "currentColor", fontSize: 11 }}
          />
          <ReferenceLine segment={[{ x: lo, y: lo }, { x: hi, y: hi }]} stroke="currentColor" strokeDasharray="4 4" ifOverflow="hidden" />
          <Scatter name="calibration" data={data.calibration} className="text-status-unknown" fill="currentColor" fillOpacity={0.6} isAnimationActive={false} />
          <Scatter name="ML" data={data.ml} className="text-accent" fill="currentColor" isAnimationActive={false} />
        </ScatterChart>
      </div>
      {data.dropped > 0 && <p className="text-2xs text-fg-subtle">{data.dropped} point(s) not plotted: a missing or zero value has no place on log axes.</p>}
    </figure>
  );
}

function PointsTable({ points, term }: { points: readonly MlPredictionPointOut[]; term: string }) {
  const [open, setOpen] = useState(false);
  const rows = points.filter((p) => p.term === term);
  const unit = unitOf(term);
  return (
    <details
      className="text-xs"
      onToggle={(e) => {
        setOpen(e.currentTarget.open);
      }}
    >
      <summary className="cursor-pointer text-fg-muted">Points as a table ({rows.length})</summary>
      {open && (
        <div className="max-h-96 overflow-auto">
          <table className="mt-2 border-collapse" data-testid="ml-points">
            <caption className="sr-only">Predicted vs actual points of {term}</caption>
            <thead>
              <tr>
                {["decision", "node", "family", "analytical", "calibrated", "ML", "actual", "ML used", "set"].map((h) => (
                  <th key={h} scope="col" className="px-2 py-1 text-left font-medium text-fg-subtle">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={`${String(p.decision_id)}|${p.node_id}|${p.term}`} className="border-t border-border">
                  <td className="px-2 py-1 font-mono">#{p.decision_id}</td>
                  <td className="px-2 py-1 font-mono">{p.node_id}</td>
                  <td className="px-2 py-1 font-mono">{p.family ?? "—"}</td>
                  <td className="px-2 py-1 font-mono tabular-nums">{secondsText(p.analytical_s, unit)}</td>
                  <td className="px-2 py-1 font-mono tabular-nums">{secondsText(p.calibrated_s, unit)}</td>
                  <td className="px-2 py-1 font-mono tabular-nums">{secondsText(p.ml_s, unit)}</td>
                  <td className="px-2 py-1 font-mono tabular-nums">{secondsText(p.actual_s, unit)}</td>
                  <td className="px-2 py-1">{p.used ? "yes" : `no${p.reason != null ? ` (${p.reason})` : ""}`}</td>
                  <td className="px-2 py-1 font-mono">{p.ml_model_version == null ? "none" : setLabel(p.ml_model_version)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}

const FALLBACK_COLUMNS = (title: string): Column<FallbackRow>[] => [
  { id: "what", header: title, cell: (r) => r.label },
  { id: "n", header: "Points", align: "right", mono: true, cell: (r) => String(r.count) },
  { id: "share", header: "Share", align: "right", mono: true, cell: (r) => share(r.share) },
];

function Predictions({ serving }: { serving: string | null }) {
  const [limit, setLimit] = useState<PredictionsLimit>(PREDICTIONS_LIMITS[0]);
  const [term, setTerm] = useState<string | null>(null);
  const [onlyServing, setOnlyServing] = useState(false);
  const query = useQuery({ queryKey: mlKeys.predictions(limit), queryFn: ({ signal }) => getMlPredictions(limit, signal) });
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading predictions…
      </p>
    );
  }
  if (query.isError) {
    return <ErrorState title="Could not load predicted vs actual" error={query.error} onRetry={() => void query.refetch()} />;
  }
  const all = query.data.points;
  const points = onlyServing && serving !== null ? all.filter((p) => p.ml_model_version === serving) : all;
  const terms = termsOf(points);
  const shown = term !== null && terms.includes(term) ? term : (terms[0] ?? null);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="inline-flex items-center gap-2">
          Term
          <select
            className={select}
            value={shown ?? ""}
            onChange={(e) => {
              setTerm(e.target.value);
            }}
            disabled={terms.length === 0}
          >
            {terms.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="inline-flex items-center gap-2">
          Newest runs
          <select
            className={select}
            value={String(limit)}
            onChange={(e) => {
              setLimit(Number(e.target.value) as PredictionsLimit);
            }}
          >
            {PREDICTIONS_LIMITS.map((n) => (
              <option key={n} value={String(n)}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className={cn("inline-flex items-center gap-2", serving === null && "opacity-50")}>
          <input
            type="checkbox"
            checked={onlyServing}
            disabled={serving === null}
            onChange={(e) => {
              setOnlyServing(e.target.checked);
            }}
          />
          Only decisions planned with the serving set{serving !== null && ` (${setLabel(serving)})`}
        </label>
        <span className="text-xs text-fg-muted">
          {query.data.decisions_read} matched runs read · {points.length} points
        </span>
      </div>
      {shown === null ? (
        <EmptyState title="No predicted-vs-actual points yet">
          Points appear once dispatched decisions have finished runs with a measured time for a learnable term.
        </EmptyState>
      ) : (
        <>
          <ScatterPlot data={scatterOf(points, shown)} />
          <PointsTable points={points} term={shown} />
          <div className="grid gap-4 md:grid-cols-2">
            <Table caption={`How often ML acted for ${shown}`} columns={FALLBACK_COLUMNS(shown)} rows={fallbackRows(points, shown)} rowKey={(r) => r.key} />
            <Table caption="How often ML acted, all terms" columns={FALLBACK_COLUMNS("All terms")} rows={fallbackRows(points)} rowKey={(r) => r.key} />
          </div>
        </>
      )}
    </div>
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
    <div className="flex flex-col gap-4">
      <InUse
        models={models}
        admin={admin}
        onTrain={() => {
          setAction({ kind: "train" });
        }}
      />
      <Card title={`Model sets (${String(models.sets.length)})`} flush>
        <Table
          caption="Model sets, newest first"
          columns={setColumns(shown, onSelect, admin, setAction)}
          rows={models.sets}
          rowKey={(s) => s.set_version}
          empty="No model set has been trained yet."
        />
        {!admin && <p className="border-t border-border px-4 py-2 text-2xs text-fg-subtle">Pin, unpin and train are for admins (read-only view).</p>}
      </Card>
      {shown !== null && <SetDetail version={shown} />}
      <Card title="Predicted vs actual">
        {admin ? (
          <Predictions serving={models.serving_set} />
        ) : (
          <p className="text-sm text-fg-muted">
            Predicted vs actual is shown to admins only: its points name every user&apos;s decisions and nodes.
          </p>
        )}
      </Card>
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
  const query = useQuery({
    queryKey: mlKeys.models,
    queryFn: ({ signal }) => getMlModels(signal),
    refetchInterval: (q) => (q.state.data?.retrain.running === true ? RETRAIN_POLL_MS : false),
  });
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
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
