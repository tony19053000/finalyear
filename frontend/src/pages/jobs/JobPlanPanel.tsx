/**
 * The job page's Plan tab: the planner's options for this job as clear rows
 * (who would run it, how, the estimated time) - the chosen one at full
 * strength, the others faded - with the estimated speed-up beside them. The
 * full explanation (every decision, term, source, rejection and the actuals:
 * what `compute explain` prints) opens below on request.
 */
import { useEffect, useId, useState, type ReactNode } from "react";

import { PlanTable } from "../../components/jobs/PlanTable";
import { formatDuration } from "../../components/jobs/format";
import { placingDecision } from "../overview/overviewModel";
import type { DecisionOut } from "./jobsApi";
import { MODE_TEXT, planView, speedFigure } from "./jobView";

interface OptionRow {
  key: string;
  names: string;
  mode: DecisionOut["candidates"][number]["mode"];
  seconds: number | null;
  chosen: boolean;
  feasible: boolean;
  note: string;
}

/** Every option the decision weighed: possible ones fastest first, then the rejected ones. */
function optionsOf(decision: DecisionOut, nameOf: (id: string) => string, predictedS: number | null): OptionRow[] {
  return decision.candidates
    .map((c, i) => {
      const t = c.estimate?.total_s ?? (c.mode === "split" && c.rank === 1 ? predictedS : null);
      const who = c.mode === "split" ? (c.node_set ?? []).map(nameOf).join(", ") || nameOf(c.node_id) : nameOf(c.node_id);
      const chosen = c.rank === 1 && (c.node_id === decision.chosen_node_id || c.mode === "split");
      const note = !c.feasible
        ? (c.reject_text ?? c.reject_code ?? "not possible")
        : c.mode === "split" && c.chunks != null
          ? `${String(c.chunks)} chunks`
          : "";
      return { key: `${c.mode}-${c.node_id}-${String(c.rank ?? i)}-${String(i)}`, names: who, mode: c.mode, seconds: t ?? null, chosen, feasible: c.feasible, note };
    })
    .sort((a, b) => (a.feasible === b.feasible ? (a.seconds ?? Infinity) - (b.seconds ?? Infinity) : a.feasible ? -1 : 1));
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="jb-fact-label">{label}</dt>
      <dd className="mt-2.5 truncate text-base text-fg">{children}</dd>
    </div>
  );
}

export interface JobPlanPanelProps {
  decisions: readonly DecisionOut[];
  pinned: boolean;
  placement: Parameters<typeof planView>[0]["placement"];
  names: ReadonlyMap<string, string>;
  /** A split job's predicted makespan (split summary), when its row has no estimate. */
  predictedS?: number | null;
}

export function JobPlanPanel({ decisions, pinned, placement, names, predictedS = null }: JobPlanPanelProps) {
  const [open, setOpen] = useState(false);
  // keep the table mounted while it folds away, then drop it
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (open) return undefined;
    const t = window.setTimeout(() => {
      setShown(false);
    }, 1100);
    return () => {
      window.clearTimeout(t);
    };
  }, [open]);
  const explainId = useId();
  const decision = placingDecision(decisions);
  if (decision === undefined) return <PlanTable decisions={decisions} pinned={pinned} />;

  const nameOf = (id: string) => names.get(id) ?? id;
  const options = optionsOf(decision, nameOf, predictedS);
  const plan = planView({ placement }, decisions, predictedS);
  const speed = speedFigure(plan.speed);

  return (
    <div className="flex flex-col gap-10">
      <section aria-label="Planner options" className="grid grid-cols-[18rem_minmax(0,1fr)] gap-x-16 pt-2 pb-2">
        <div className="flex flex-col">
          <h2 className="ov-card-title">Speed-up</h2>
          <p className="ov-figure my-auto pt-6 text-center text-[6.5rem] leading-none text-[#ffc93c] tabular-nums" data-testid="plan-speedup">
            {speed === null ? "—" : speed.figure}
            {speed !== null && (
              <span className="ml-1.5 text-3xl font-light tracking-normal text-[#ffc93c]/70">{speed.unit === "×" ? "×" : ` ${speed.unit}`}</span>
            )}
          </p>
          <p className="mt-4 text-center font-mono text-[0.6875rem] tracking-[0.12em] text-fg-muted uppercase">
            {speed === null ? "nothing to compare" : speed.label === "saved" ? "saved by staying local · est." : "faster than local · est."}
          </p>
        </div>
        <div className="min-w-0">
          <div className="jb-opt-h grid grid-cols-[minmax(0,1fr)_auto_auto] items-baseline gap-x-10 pb-3 xl:grid-cols-[minmax(0,1fr)_9rem_8rem]">
            <span>Option</span>
            <span className="text-center">Mode</span>
            <span className="text-right">Est. time</span>
          </div>
          <ul aria-label="Options the planner weighed" className="nd-hair-t divide-y divide-[#ffffff0f]">
            {options.map((o) => (
              <li
                key={o.key}
                aria-current={o.chosen ? "true" : undefined}
                className={`jb-option grid grid-cols-[minmax(0,1fr)_auto_auto] items-baseline gap-x-10 py-4 xl:grid-cols-[minmax(0,1fr)_9rem_8rem] ${o.chosen ? "" : "opacity-75"}`}
              >
                <span className="min-w-0">
                  <span className={`block truncate text-[0.9375rem] ${o.chosen ? "font-semibold text-fg" : "text-fg"}`} title={o.names}>
                    {o.names}
                  </span>
                  {o.note !== "" && <span className="mt-0.5 block truncate text-xs text-fg-muted">{o.note}</span>}
                </span>
                <span className="text-center font-mono text-[0.8125rem] tracking-[0.12em] text-fg uppercase">{MODE_TEXT[o.mode]}</span>
                <span className="text-right font-mono text-[0.9375rem] text-fg tabular-nums">
                  {o.seconds === null ? "—" : formatDuration(o.seconds * 1000)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <dl className="nd-hair-t grid grid-cols-5 gap-x-10 pt-8">
        <Fact label="Decided for">attempt {decision.attempt_no}</Fact>
        <Fact label="Policy">
          <span className="font-mono text-[0.9375rem]">{decision.policy}</span>
        </Fact>
        <Fact label="Objective">
          <span className="font-mono text-[0.9375rem]">{decision.objective ?? "—"}</span>
        </Fact>
        <Fact label="Outcome">{decision.outcome}</Fact>
        <Fact label="Decided in">{decision.decision_ms == null ? "—" : `${decision.decision_ms.toFixed(2)} ms`}</Fact>
      </dl>

      <div className={`ov-card ov-frost nd-glass jb-morph ${open ? "jb-morph-open" : ""}`}>
        <button
          type="button"
          className="jb-morph-btn"
          aria-label={open ? "Hide the full explanation" : "Show the full explanation"}
          title={open ? undefined : "How the planner decided"}
          aria-expanded={open}
          aria-controls={explainId}
          onClick={() => {
            if (!open) setShown(true);
            setOpen((v) => !v);
          }}
        >
          <span aria-hidden="true" className={open ? "text-base leading-none" : "font-serif text-[0.95rem] italic leading-none"}>
            {open ? "×" : "i"}
          </span>
        </button>
        <div className="jb-morph-body">
          <div>
            {shown && (
              <div id={explainId} className="jb-explain pt-7 pr-9 pb-8 pl-16">
                <PlanTable decisions={decisions} pinned={pinned} />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
