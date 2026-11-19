/**
 * Job progress (frame B "Total Sales" card) for the same job the Speed-up card
 * describes (newest active, else newest), with its mode under the title:
 *
 * - Split: real % from chunk counts (`GET /v1/jobs/{id}/split`, live events);
 * - Local / Sent away: estimated % = time since submit ÷ the planner's
 *   estimate, capped at 95 % until the job really ends ("est.").
 */
import { useEffect, useId, useState } from "react";

import type { JobSummary } from "../jobs/jobsApi";
import type { SplitProgress } from "../jobs/splitApi";
import { ArrowUpRight, InfoBell, InfoPoints, OverviewCard } from "./OverviewCard";
import { estimatedPercent, isActive, MODE_LABEL, splitPercent, type SpeedUp } from "./overviewModel";

export interface JobProgressCardProps {
  job: JobSummary | undefined;
  /** The planner's placement for the job (mode + chosen estimate). */
  plan: SpeedUp | null;
  /** The split summary, when the job is a split. */
  progress: SplitProgress | undefined;
  loading: boolean;
  failed: boolean;
  /** node_id -> display name. */
  names?: ReadonlyMap<string, string>;
  /** Widened to the right (over Speed-up): who does what, and the time. */
  expanded?: boolean;
  onToggle?: () => void;
  /** Opened by the bell (what the card shows) rather than the arrow. */
  explain?: boolean;
  onExplain?: () => void;
}

function clockOf(s: number): string {
  const v = Math.max(0, Math.round(s));
  const h = Math.floor(v / 3600);
  const m = Math.floor((v % 3600) / 60);
  const sec = v % 60;
  return h > 0 ? `${String(h)}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${String(m)}:${String(sec).padStart(2, "0")}`;
}

const STEPS = ["Waiting", "Sending", "Running", "Done"] as const;

function stepOf(state: string, mode: string | null): number {
  if (state === "succeeded" || state === "failed" || state === "cancelled") return 3;
  if (state === "running") return 2;
  if (state === "dispatched" || state === "leased" || state === "staging") return mode === "offload" ? 1 : 2;
  return 0;
}

const BAR_H = 18;

/**
 * Thick pill: honey fill with a rounded end, a silver knob with "⋮" at the
 * fill edge, the rest hatched grey inside a dark pill. SVG with width
 * attributes (no inline style: CSP style-src 'self').
 */
function HatchedBar({ percent }: { percent: number }) {
  const p = Math.max(0, Math.min(100, percent));
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const r = BAR_H / 2;
  return (
    <div role="progressbar" aria-label="Job progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(p)}>
      <svg aria-hidden="true" className="ov-hatch block h-[18px] w-full rounded-full">
        <defs>
          <linearGradient id={`fill${id}`} x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" className="ov-stop-deep" />
            <stop offset="1" className="ov-stop-honey" />
          </linearGradient>
        </defs>
        {p > 0 && (
          <svg x="0" y="0" width={`${String(p)}%`} height={BAR_H} overflow="visible">
            <rect className="ov-bar-fill" x="0" y="0" width="100%" height={BAR_H} rx={r} fill={`url(#fill${id})`} />
          </svg>
        )}
      </svg>
    </div>
  );
}

function useNow(on: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return undefined;
    const t = window.setInterval(() => {
      setNow(Date.now());
    }, 1000);
    return () => {
      window.clearInterval(t);
    };
  }, [on]);
  return now;
}

/** What the card shows, opened by its bell. */
const JOB_PROGRESS_POINTS = [
  "Progress of the newest job",
  "Tag: Split, Sent away or Local",
  "Split: real pieces done",
  "Others: estimate (est.)",
  "Arrow: laptops and time left",
];

export function JobProgressCard({ job, plan, progress, loading, failed, names, expanded = false, onToggle, explain = false, onExplain }: JobProgressCardProps) {
  const running = job !== undefined && isActive(job);
  const now = useNow(running);
  const actions = (
    <>
      <InfoBell open={expanded && explain} title="Job progress" onClick={onExplain} dark={false} />
      <button
        type="button"
        className="ov-icon-btn ov-icon-btn-honey"
        aria-label={expanded ? "Close the job details" : "Show who runs the job and the time"}
        aria-expanded={expanded}
        onClick={onToggle}
      >
        {expanded ? <span aria-hidden="true" className="text-base leading-none">×</span> : <ArrowUpRight />}
      </button>
    </>
  );

  const mode = plan?.mode ?? (progress !== undefined ? "split" : null);
  let pct: number | null = null;
  let est = false;
  let note: string | null = null;
  if (job !== undefined) {
    if (mode === "split" && progress !== undefined) {
      pct = splitPercent(progress);
    } else if (job.state === "succeeded") {
      pct = 100;
    } else if (!running) {
      note = job.state;
    } else if (mode === "split") {
      note = failed ? "Could not load the split progress." : "Loading the split progress…";
    } else {
      pct = estimatedPercent(job.created_at, plan?.chosenS ?? null, now);
      est = true;
      if (pct === null) note = "running · no planner estimate";
    }
  }

  let body;
  if (job === undefined) {
    body = loading ? (
      <p role="status" className="mt-auto text-sm text-fg-muted">
        Looking for jobs…
      </p>
    ) : (
      <div className="mt-auto flex flex-col gap-4" data-testid="split-empty">
        <p className="ov-figure text-6xl text-fg-subtle">—</p>
        <div className="ov-hatch h-[18px] w-full rounded-full" aria-hidden="true" />
      </div>
    );
  } else {
    body = (
      <div className="mt-auto flex flex-col gap-4">
        <div className="flex items-end justify-between gap-3">
          <p className="ov-figure flex items-baseline text-[3.75rem] font-light text-fg" data-testid="split-percent">
            {pct === null ? "—" : Math.floor(pct)}
            {pct !== null && <span className="ml-1 text-xl font-light text-fg-muted">%</span>}
          </p>
          <span className="mb-2 flex flex-col items-end gap-1 text-right">
            {mode !== null && (
              <span
                className="flex items-center gap-1.5 font-mono text-[0.625rem] font-medium tracking-[0.14em] whitespace-nowrap text-[#ffc93c] uppercase"
                data-testid="progress-mode"
              >
                <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-[#ffc93c]" />
                {MODE_LABEL[mode]}
              </span>
            )}
            {(est || note !== null) && (
              <span className="font-mono text-[0.625rem] tracking-[0.08em] text-fg-muted uppercase">{note ?? "estimated"}</span>
            )}
          </span>
        </div>
        <HatchedBar percent={pct ?? 0} />
      </div>
    );
  }

  const nameOf = (id: string) => names?.get(id) ?? id;
  // time: split summary first, else since submit + the planner's estimate
  const elapsedS = progress?.elapsed_s ?? (job === undefined ? null : (now - Date.parse(job.created_at)) / 1000);
  const leftS =
    progress?.eta_s ?? (plan?.chosenS != null && elapsedS != null && running ? Math.max(0, plan.chosenS - elapsedS) : null);

  let details = null;
  if (expanded && explain) {
    details = <InfoPoints title="Job progress" points={JOB_PROGRESS_POINTS} testId="progress-info" />;
  } else if (expanded && job !== undefined) {
    details = (
      <div className="ov-speed-more flex min-w-0 flex-1 flex-col" data-testid="progress-details">
        <div className="flex items-baseline gap-3">
          <p className="min-w-0 flex-1 truncate text-sm font-semibold text-fg">
            {job.name}
            <span className="ml-2 font-mono text-[0.6875rem] font-normal text-fg-muted">{job.job_id}</span>
          </p>
          {mode === "split" && progress !== undefined && (
            <>
              <span className="w-24 shrink-0 text-right font-mono text-[0.5625rem] tracking-[0.14em] text-fg-subtle uppercase">working on</span>
              <span className="w-24 shrink-0 text-right font-mono text-[0.5625rem] tracking-[0.14em] text-fg-subtle uppercase">done</span>
            </>
          )}
        </div>
        {mode === "split" && progress !== undefined ? (
          <ul className="ov-scroll ov-scroll-none mt-1 flex min-h-0 flex-1 flex-col divide-y divide-[#2c2924] overflow-y-auto">
            {progress.lanes.map((l) => (
              <li key={l.attempt_id} className="flex items-baseline gap-3 py-0.5">
                <span className="min-w-0 flex-1 truncate text-[0.8125rem] font-medium text-fg">{nameOf(l.node_id)}</span>
                <span className="w-24 shrink-0 text-right font-mono text-[0.625rem] tracking-[0.1em] text-fg-muted uppercase">
                  {l.current.length > 0 ? `chunk #${String(l.current[0]?.chunk_index ?? "")}` : l.state}
                </span>
                <span className="w-24 shrink-0 text-right font-mono text-sm text-fg tabular-nums">
                  {l.chunks_done}
                  <span className="text-fg-muted"> chunks</span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col justify-center gap-5">
            <p className="text-sm text-fg">
              <span className="text-fg-muted">{mode === "local" ? "Running on " : "Sent to "}</span>
              <span className="font-semibold">{plan?.targetNodeId != null ? nameOf(plan.targetNodeId) : "—"}</span>
            </p>
            {/* steps spread across the width, joined by a thin line */}
            <ol className="flex items-center font-mono text-[0.6875rem] tracking-[0.12em] uppercase">
              {STEPS.filter((st) => mode === "offload" || st !== "Sending").map((st, i, list) => {
                const at = stepOf(job.state, mode);
                const idx = STEPS.indexOf(st);
                const cur = idx === at;
                const reached = idx <= at;
                return (
                  <li key={st} className={`flex items-center ${i < list.length - 1 ? "flex-1" : ""}`}>
                    <span className="flex shrink-0 items-center gap-2">
                      <span
                        aria-hidden="true"
                        className={`h-2 w-2 rounded-full ${cur ? "bg-[#ffc93c]" : reached ? "bg-fg" : "border border-fg-subtle"}`}
                      />
                      <span className={cur ? "text-[#ffc93c]" : reached ? "text-fg" : "text-fg-subtle"}>{st}</span>
                    </span>
                    {i < list.length - 1 && <span aria-hidden="true" className={`mx-3 h-px flex-1 ${idx < at ? "bg-fg-muted" : "bg-[#3a3327]"}`} />}
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </div>
    );
  }

  return (
    <OverviewCard
      title="Job progress"
      actions={actions}
      data-testid="split-card"
      className="h-[12.5rem]"
    >
      <div className="flex min-h-0 flex-1 gap-8">
        <div className={`flex min-w-0 flex-col ${expanded && explain ? "hidden" : expanded ? "w-[11rem] shrink-0" : "flex-1"}`}>
          {body}
          {expanded && !explain && job !== undefined && (
            <p className="mt-2 font-mono text-[0.625rem] whitespace-nowrap text-fg-muted tabular-nums" data-testid="progress-time">
              {elapsedS === null ? "" : clockOf(elapsedS)}
              {leftS !== null && running ? ` · ${clockOf(leftS)} left` : running ? "" : ` · ${job.state}`}
            </p>
          )}
        </div>
        {details}
      </div>
    </OverviewCard>
  );
}
