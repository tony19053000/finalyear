/**
 * The Planner page's open row of headline figures (the Benchmarks list's
 * language): how close the planner's estimate is to the measured time (the
 * one honey figure), how long a decision takes, the calibration and the ML
 * model in use. Spread evenly edge to edge; every number is an API answer
 * (the active calibration's held-out error, the newest evaluation's decision
 * latency, the model registry), read from the same cache as the tabs. A
 * route that is missing or fails leaves its figure at "—" (the tab says why).
 */
import { learnedText, mlState, setLabel } from "./mlView";
import { useCalibration, useEvalReport, useEvalReports, useMlModels } from "./plannerQueries";
import { PlFigure } from "./plannerUi";
import { errorText, withinPct } from "./plannerView";

const EACH = "max-w-[24%]";

function EstimateFigure() {
  const query = useCalibration(null);
  const cal = query.data?.calibration ?? null;
  const within = withinPct(cal?.holdout_error_new);
  return (
    <PlFigure
      label="Estimate within"
      honey
      className={EACH}
      testId="planner-figure-estimate"
      value={within === null ? "—" : within.toFixed(0)}
      unit="%"
      sub={
        cal === null || within === null
          ? query.isPending
            ? "reading the calibration"
            : "no calibrated estimate yet"
          : `of measured · held-out runs, v${String(cal.version)}`
      }
      hint={
        cal === null || within === null
          ? undefined
          : `The planner's estimate against the measured time on the held-out fifth of v${String(cal.version)}'s matched runs: mean |ln(predicted / actual)| = ${errorText(cal.holdout_error_new)}, a factor of ×${(1 + within / 100).toFixed(3)}.`
      }
    />
  );
}

function DecisionFigure() {
  const list = useEvalReports();
  const newest = list.data?.reports.find((r) => r.status === "done") ?? null;
  const report = useEvalReport(newest?.id ?? null);
  const latency = report.data?.report?.latency ?? null;
  const p50 = latency?.p50_ms ?? null;
  return (
    <PlFigure
      label="Decision time"
      className={EACH}
      testId="planner-figure-decision"
      value={p50 === null ? "—" : p50.toFixed(1)}
      unit="ms"
      sub={
        latency === null || p50 === null
          ? list.isError
            ? "no evaluator on this controller"
            : "no timed decisions yet"
          : `p50 · p99 ${latency.p99_ms == null ? "—" : latency.p99_ms.toFixed(1)} ms · report #${String(report.data?.id ?? "")}`
      }
      hint={
        latency === null
          ? undefined
          : `Wall-clock time of one planner decision over the ${String(latency.decisions)} dispatched decisions report #${String(report.data?.id ?? "")} timed; criterion 3 judges the p99 against ${String(latency.budget_ms)} ms.`
      }
    />
  );
}

function CalibrationFigure() {
  const query = useCalibration(null);
  const view = query.data;
  const inUse = view?.in_use.in_use_version ?? null;
  const pinned = view?.in_use.pinned_version ?? null;
  return (
    <PlFigure
      label="Calibration"
      className={EACH}
      testId="planner-figure-calibration"
      value={view === undefined ? "—" : inUse === null ? "none" : `v${String(inUse)}`}
      sub={
        view === undefined
          ? "—"
          : inUse === null
            ? "uncalibrated estimates"
            : `${pinned !== null ? "pinned" : "in use"} · mode ${view.in_use.mode} · ${String(view.recent.length)} versions`
      }
      hint="The calibration version the planner scales its estimates with now (refitted from finished runs; a new fit goes live only if it beats the active one on held-out runs)."
    />
  );
}

function ModelFigure() {
  const query = useMlModels();
  const models = query.data;
  const state = models === undefined ? null : mlState(models);
  const set = models?.in_use.in_use_set ?? null;
  const members = models?.sets.find((s) => s.set_version === set)?.members ?? [];
  const learned = members.filter((m) => m.algorithm !== "none").length;
  const value = models === undefined ? "—" : state === "off" ? "off" : set === null ? "none" : setLabel(set).slice(0, 8);
  const sub =
    models === undefined
      ? query.isError
        ? "no model registry"
        : "—"
      : state === "off"
        ? "calibration only"
        : set === null
          ? models.retrain.running
            ? "first set training…"
            : "calibration only, no set yet"
          : `${models.in_use.kind ?? "active"} · ${String(learned)} of ${String(members.length || 8)} terms learned${models.retrain.running ? " · training…" : ""}`;
  return (
    <PlFigure
      label="Active model"
      className={EACH}
      testId="planner-figure-model"
      value={value}
      sub={sub}
      hint={set === null ? undefined : `Model set ${set}: ${learnedText(members)}`}
    />
  );
}

export function PlannerHeadline() {
  return (
    <dl className="nd-stagger mb-9 flex justify-between gap-x-12 px-9" data-testid="planner-figures">
      <EstimateFigure />
      <DecisionFigure />
      <CalibrationFigure />
      <ModelFigure />
    </dl>
  );
}
