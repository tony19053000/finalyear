/**
 * The job page's Plan tab: the planner's options for this job as clear rows
 * (who would run it, how, the estimated time) - the chosen one at full
 * strength, the others faded - with the estimated speed-up beside them. The
 * full explanation (every decision, term, source, rejection and the actuals:
 * what `compute explain` prints) opens below on request.
 */
import { useId, useState, type ReactNode } from "react";

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
      <dt className="nd-label">{label}</dt>
      <dd className="mt-2 truncate text-[0.9375rem] text-fg">{children}</dd>
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
  const explainId = useId();
  const decision = placingDecision(decisions);
  if (decision === undefined) return <PlanTable decisions={decisions} pinned={pinned} />;

  const nameOf = (id: string) => names.get(id) ?? id;
  const options = optionsOf(decision, nameOf, predictedS);
  const plan = planView({ placement }, decisions, predictedS);
  const speed = speedFigure(plan.speed);

  return (
    <div className="flex flex-col gap-10">
      <section aria-label="Planner options" className="ov-card ov-frost grid grid-cols-[13rem_minmax(0,1fr)] gap-x-14 px-10 pt-9 pb-8">
        <div className="flex flex-col">
          <h2 className="ov-card-title">Speed-up</h2>
          <p className="ov-figure mt-auto pt-10 text-[4.25rem] text-fg tabular-nums" data-testid="plan-speedup">
            {speed === null ? "—" : speed.figure}
            {speed !== null && (
              <span className="ml-1 text-2xl font-light tracking-normal text-fg-muted">{speed.unit === "×" ? "×" : ` ${speed.unit}`}</span>
            )}
          </p>
          <p className="mt-3 font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">
            {speed === null ? "nothing to compare" : speed.label === "saved" ? "saved by staying local · est." : "faster than local · est."}
          </p>
        </div>
        <div className="min-w-0">
          <div className="grid grid-cols-[minmax(0,1fr)_7rem_6rem] items-baseline gap-x-8 pb-3">
            <span className="nd-label">Option</span>
            <span className="nd-label">Mode</span>
            <span className="nd-label text-right">Est. time</span>
          </div>
          <ul aria-label="Options the planner weighed" className="nd-hair-t divide-y divide-[#ffffff0f]">
            {options.map((o) => (
              <li
                key={o.key}
                aria-current={o.chosen ? "true" : undefined}
                className={`jb-option grid grid-cols-[minmax(0,1fr)_7rem_6rem] items-baseline gap-x-8 py-3.5 ${o.chosen ? "" : "opacity-45"}`}
              >
                <span className="min-w-0">
                  <span className={`block truncate text-[0.9375rem] ${o.chosen ? "font-semibold text-fg" : "text-fg"}`} title={o.names}>
                    {o.names}
                  </span>
                  {o.note !== "" && <span className="mt-0.5 block truncate text-xs text-fg-muted">{o.note}</span>}
                </span>
                <span className="font-mono text-[0.6875rem] tracking-[0.14em] text-fg-muted uppercase">{MODE_TEXT[o.mode]}</span>
                <span className="text-right font-mono text-sm text-fg tabular-nums">
                  {o.seconds === null ? "—" : formatDuration(o.seconds * 1000)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <dl className="grid grid-cols-5 gap-x-10 px-2">
        <Fact label="Decided for">attempt {decision.attempt_no}</Fact>
        <Fact label="Policy">
          <span className="font-mono text-[0.8125rem]">{decision.policy}</span>
        </Fact>
        <Fact label="Objective">
          <span className="font-mono text-[0.8125rem]">{decision.objective ?? "—"}</span>
        </Fact>
        <Fact label="Outcome">{decision.outcome}</Fact>
        <Fact label="Decided in">{decision.decision_ms == null ? "—" : `${decision.decision_ms.toFixed(2)} ms`}</Fact>
      </dl>

      <div className="flex flex-col gap-6">
        <div>
          <button
            type="button"
            className="jb-quiet-btn"
            aria-expanded={open}
            aria-controls={explainId}
            onClick={() => {
              setOpen((v) => !v);
            }}
          >
            {open ? "Hide the full explanation" : "Show the full explanation"}
          </button>
        </div>
        {open && (
          <div id={explainId} className="nd-in ov-card ov-frost jb-explain px-9 py-8">
            <PlanTable decisions={decisions} pinned={pinned} />
          </div>
        )}
      </div>
    </div>
  );
}
