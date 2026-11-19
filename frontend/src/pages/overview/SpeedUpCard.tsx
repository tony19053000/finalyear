/**
 * The one honey card: the planner's estimated speed-up for the newest active
 * (else newest) job, from `GET /v1/jobs/{id}/decisions`:
 *
 * - Split / Sent away: local estimate ÷ chosen estimate ("3.2x est. speed-up");
 * - Local: best non-local estimate − local estimate ("N s saved · stayed local").
 *
 * Estimates, never measurements - the card says "est.".
 */
import { useEffect, useId, useRef, useState } from "react";

import type { DecisionOut, JobSummary } from "../jobs/jobsApi";
import { ArrowUpRight, InfoBell, InfoPoints, OverviewCard } from "./OverviewCard";
import { durationParts, placingDecision, speedUp, type SpeedUp } from "./overviewModel";

export interface SpeedUpCardProps {
  job: JobSummary | undefined;
  decisions: readonly DecisionOut[] | undefined;
  loading: boolean;
  names: ReadonlyMap<string, string>;
  /** A split job's predicted makespan (split summary), when its row has no estimate. */
  predictedS?: number | null;
  /** Widened over the top row: every option the planner weighed + the recent jobs strip. */
  expanded?: boolean;
  onToggle?: () => void;
  /** Opened by the bell (what the card shows) rather than the arrow. */
  explain?: boolean;
  onExplain?: () => void;
  recent?: readonly { job: JobSummary; plan: SpeedUp }[];
}

/**
 * Counts smoothly from the shown value to a new one (~1.4 s, ease-out) when
 * the job - and so the speed-up - changes. The first value and reduced
 * motion jump.
 */
function useCountTo(target: number | null): number | null {
  // `anim` holds the in-between value only while counting; otherwise the target shows.
  const [anim, setAnim] = useState<number | null>(null);
  const from = useRef(target);
  useEffect(() => {
    const start = from.current;
    from.current = target;
    const reduce = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // First value appears as is; only a change of job counts.
    if (target === null || start === null || reduce || start === target) return undefined;
    const t0 = performance.now();
    let raf = 0;
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / 1400);
      const v = start + (target - start) * (1 - (1 - k) ** 3);
      from.current = v;
      setAnim(k < 1 ? v : null);
      if (k < 1) raf = requestAnimationFrame(step);
      else from.current = target;
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
    };
  }, [target]);
  return target === null ? null : (anim ?? target);
}

/** Short time for a pill's circle: "24m", "7.5m", "40s", "1.2h". */
function shortTime(s: number): string {
  const [n, u] = durationParts(s);
  return `${n.replace(/\.0$/, "")}${u === "min" ? "m" : u}`;
}

const PILL_W = 40;
const PILL_H = 116;
const DOT = 38;

/**
 * Frame B's two pills as frosted glass tracks: left = the slower option (local, or "sent away"
 * for a job that stayed local), right = what hive chose. Each circle
 * sits at a height set by its time (the slower one at the top) and slides when
 * the job changes.
 */
function TimePills({ slowS, fastS, slowLabel, fastLabel }: { slowS: number; fastS: number; slowLabel: string; fastLabel: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const max = Math.max(slowS, fastS);
  const travel = PILL_H - DOT;
  // y of each circle's top: the longer time at the top, the shorter lower down.
  const yOf = (t: number) => travel - (t / max) * travel;
  const W = PILL_W * 2 + 10;
  return (
    <svg
      viewBox={`0 0 ${String(W)} ${String(PILL_H)}`}
      className="h-[7.25rem] w-auto"
      role="img"
      aria-label={`${slowLabel} ${shortTime(slowS)}, ${fastLabel} ${shortTime(fastS)}`}
      data-testid="speedup-pills"
    >
      <defs>
        {/* frosted glass: a pale fill fading down, a soft inner shadow at the top edge */}
        <linearGradient id={`glass${id}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.42" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0.14" />
        </linearGradient>
        <filter id={`inset${id}`} x="-20%" y="-20%" width="140%" height="140%">
          <feOffset dy="2" />
          <feGaussianBlur stdDeviation="2.5" result="b" />
          <feComposite in="SourceAlpha" in2="b" operator="out" result="ring" />
          <feFlood floodColor="#7a4a00" floodOpacity="0.28" />
          <feComposite in2="ring" operator="in" result="shadow" />
          <feMerge>
            <feMergeNode in="SourceGraphic" />
            <feMergeNode in="shadow" />
          </feMerge>
        </filter>
        {/* the shine on each circle: a light from the top-left */}
        <radialGradient id={`shineDark${id}`} cx="0.35" cy="0.28" r="0.75">
          <stop offset="0" stopColor="#4a4438" />
          <stop offset="0.55" stopColor="#15130f" />
          <stop offset="1" stopColor="#0b0a08" />
        </radialGradient>
        <radialGradient id={`shineLight${id}`} cx="0.35" cy="0.28" r="0.75">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset="0.6" stopColor="#fff4c2" />
          <stop offset="1" stopColor="#ffe48a" />
        </radialGradient>
      </defs>
      <g filter={`url(#inset${id})`}>
        <rect x="0" y="0" width={PILL_W} height={PILL_H} rx={PILL_W / 2} fill={`url(#glass${id})`} stroke="#ffffff" strokeOpacity="0.45" strokeWidth="1" />
        <rect x={PILL_W + 10} y="0" width={PILL_W} height={PILL_H} rx={PILL_W / 2} fill={`url(#glass${id})`} stroke="#ffffff" strokeOpacity="0.45" strokeWidth="1" />
      </g>
      <svg x={(PILL_W - DOT) / 2} y={yOf(slowS)} width={DOT} height={DOT} overflow="visible" className="ov-pill-dot">
        <circle cx={DOT / 2} cy={DOT / 2} r={DOT / 2} fill={`url(#shineDark${id})`} />
        <text x={DOT / 2} y={DOT / 2} textAnchor="middle" dominantBaseline="central" className="ov-pill-text" fill="#f2ede4">
          {shortTime(slowS)}
        </text>
      </svg>
      <svg x={PILL_W + 10 + (PILL_W - DOT) / 2} y={yOf(fastS)} width={DOT} height={DOT} overflow="visible" className="ov-pill-dot">
        <circle cx={DOT / 2} cy={DOT / 2} r={DOT / 2} fill={`url(#shineLight${id})`} className="ov-pill-light" />
        <text x={DOT / 2} y={DOT / 2} textAnchor="middle" dominantBaseline="central" className="ov-pill-text" fill="#0b0a08">
          {shortTime(fastS)}
        </text>
      </svg>
      <title>{`${slowLabel}: ${shortTime(slowS)} · ${fastLabel}: ${shortTime(fastS)}`}</title>
    </svg>
  );
}

const MODE_SHORT: Record<string, string> = { local: "Local", offload: "Sent away", split: "Split" };

/** What the card shows, opened by its bell. */
const SPEED_UP_POINTS = [
  "Expected speed-up vs. your laptop alone",
  "3.2x = 3.2 times faster",
  "Kept local: seconds saved",
  "Bars: your laptop vs. chosen plan",
  "Planner estimate, not measured",
  "Arrow: every option weighed",
];

export function SpeedUpCard({ job, decisions, loading, predictedS, names, expanded = false, onToggle, explain = false, onExplain }: SpeedUpCardProps) {
  const decision = decisions === undefined ? undefined : placingDecision(decisions);
  const su = decision === undefined ? null : speedUp(decision, predictedS);

  let figure: string;
  let unit = "";
  let caption: string;
  if (job === undefined) {
    figure = "—";
    caption = loading ? "Loading jobs…" : "No jobs yet. Submit one and hive shows how much faster its plan is.";
  } else if (decisions === undefined) {
    figure = "—";
    caption = "Reading the planner's decision…";
  } else if (su === null) {
    figure = "—";
    caption = "No planner estimate for this job (placed by hand, or not planned yet).";
  } else if (su.mode === "local") {
    if (su.savedS === null) {
      figure = "—";
    } else {
      const [n, u] = durationParts(su.savedS);
      figure = n.replace(/\.0$/, "");
      unit = u;
    }
    caption = su.savedS === null ? "stayed local · no other option to compare" : "est. · stayed local";
  } else {
    figure = su.factor === null ? "—" : su.factor >= 10 ? su.factor.toFixed(0) : su.factor.toFixed(1);
    unit = su.factor === null ? "" : "x";
    caption = su.factor === null ? "no local estimate to compare" : "est. speed-up";
  }

  const numeric = figure === "—" ? null : Number(figure);
  const decimals = figure.includes(".") ? (figure.split(".")[1]?.length ?? 0) : 0;
  const counted = useCountTo(numeric !== null && Number.isFinite(numeric) ? numeric : null);
  const shownFigure = counted === null ? figure : counted.toFixed(decimals);

  // The two times behind the number (planner estimates).
  let pills: { slowS: number; fastS: number; slowLabel: string; fastLabel: string } | null = null;
  if (su !== null && su.chosenS !== null) {
    if (su.mode === "local" && su.savedS !== null && su.savedS > 0)
      pills = { slowS: su.chosenS + su.savedS, fastS: su.chosenS, slowLabel: "sent away", fastLabel: "local" };
    else if (su.mode !== "local" && su.factor !== null)
      pills = { slowS: su.chosenS * su.factor, fastS: su.chosenS, slowLabel: "local", fastLabel: "hive" };
  }

  // Every option the planner weighed for this job, fastest first.
  const chosenId = decision?.chosen_node_id;
  const options = (decision?.candidates ?? [])
    .map((c) => {
      const t = c.estimate?.total_s ?? (c.mode === "split" && c.rank === 1 ? (predictedS ?? null) : null);
      const nameOf = (id: string) => names.get(id) ?? id;
      const who = c.mode === "split" ? (c.node_set ?? []).map(nameOf).join(", ") : nameOf(c.node_id);
      const chosen = c.rank === 1 && (c.node_id === chosenId || c.mode === "split");
      const note = !c.feasible ? (c.reject_text ?? c.reject_code ?? "not possible") : c.mode === "split" && c.chunks != null ? `${String(c.chunks)} chunks` : "";
      return { key: `${c.mode}-${c.node_id}-${String(c.rank ?? "")}`, mode: c.mode, names: who, t, chosen, feasible: c.feasible, note };
    })
    .sort((a, b) => (a.feasible === b.feasible ? (a.t ?? Infinity) - (b.t ?? Infinity) : a.feasible ? -1 : 1));


  return (
    <OverviewCard
      title="Speed-up"
      honey
      data-testid="speedup-card"
      className="h-[12.5rem]"
      actions={
        <>
        <InfoBell open={expanded && explain} title="Speed-up" onClick={onExplain} dark={true} />
        <button
          type="button"
          className={`ov-icon-btn ${expanded ? "ov-icon-btn-dark" : ""}`}
          aria-label={expanded ? "Close the planner's options" : "Show every option the planner weighed"}
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? <span aria-hidden="true" className="text-base leading-none">×</span> : <ArrowUpRight />}
        </button>
        </>
      }
    >
      <div className="flex min-h-0 flex-1 gap-8">
        <div className={`relative flex min-w-0 flex-col ${expanded && explain ? "hidden" : expanded ? "w-[6.5rem] shrink-0" : "flex-1"}`}>
      {pills !== null && figure !== "—" && (
            <div className="pointer-events-none absolute right-0 -bottom-1">
              <TimePills {...pills} />
            </div>
          )}
          <div className={`mt-auto flex flex-col gap-1 ${pills !== null ? "pr-[6.5rem]" : ""} ${expanded ? "hidden" : ""}`}>
            <div className="flex items-baseline">
              <p className="ov-figure ov-figure-bold text-[4rem] tabular-nums" data-testid="speedup-figure">
                {shownFigure}
                {unit !== "" && <span className="ml-1 text-3xl font-normal tracking-normal">{unit}</span>}
              </p>
            </div>
            {figure === "—" ? (
              <p className="text-sm font-medium" data-testid="speedup-caption">
                {caption}
              </p>
            ) : (
              job !== undefined && (
                <p className="truncate font-mono text-xs" title={job.name} data-testid="speedup-job">
                  <span className="font-medium uppercase">{su?.mode === "local" ? "saved · est." : "est."}</span>
                  <span className="opacity-70"> · {job.job_id}</span>
                </p>
              )
            )}
          </div>
    
        </div>
        {expanded && explain && <InfoPoints title="Speed-up" points={SPEED_UP_POINTS} testId="speedup-info" />}
        {expanded && !explain && job !== undefined && (
          <div className="ov-speed-more flex min-w-0 flex-1 flex-col pr-24" data-testid="speedup-options">
            <p className="truncate text-sm font-semibold">
              {job.name}
              <span className="ml-2 font-mono text-[0.6875rem] font-normal opacity-60">{job.job_id}</span>
            </p>
            <ul className="ov-scroll ov-scroll-none mt-1 flex min-h-0 flex-1 flex-col divide-y divide-[#0b0a08]/10 overflow-y-auto">
              {options
                .filter((o) => o.t !== null)
                .map((o) => (
                  <li key={o.key} className={`flex items-baseline gap-3 py-1 ${o.chosen ? "" : "opacity-60"}`}>
                    <span className="min-w-0 flex-1 truncate text-[0.8125rem] font-medium" title={o.names}>
                      {o.names}
                    </span>
                    <span className="shrink-0 font-mono text-[0.625rem] tracking-[0.1em] uppercase opacity-70">{MODE_SHORT[o.mode] ?? o.mode}</span>
                    <span className="w-14 shrink-0 text-right font-mono text-sm font-semibold tabular-nums">{shortTime(o.t ?? 0)}</span>
                  </li>
                ))}
            </ul>
          </div>
        )}
      </div>
    </OverviewCard>
  );
}
