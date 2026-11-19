/**
 * The job page's Progress tab, in the Overview's job-progress language: the
 * big light percent over the honey bar, then either the split's per-node
 * chunk rows (WORKING ON / DONE) or the local / sent-away steps line
 * (Waiting → Sending → Running → Done).
 *
 * - Split: real % from chunk counts (`GET /v1/jobs/{id}/split`, shared with
 *   the Chunks tab and patched by split.progress events);
 * - Local / Sent away: estimated % = time since the work started ÷ the
 *   planner's estimate, capped below 100 until the job really ends ("est.").
 */
import { useQuery } from "@tanstack/react-query";
import { useId, type ReactNode } from "react";

import { liveElapsed } from "../../components/jobs/splitView";
import { formatDuration } from "../../components/jobs/format";
import { useServerNow } from "../../lib/serverClock";
import { jobKeys } from "./jobKeys";
import type { DecisionOut, JobDetail } from "./jobsApi";
import { MODE_TEXT, isTerminal, planView, progressOf, startedAt, type PlanView } from "./jobView";
import { getSplit, isSplitJob, type SplitProgress } from "./splitApi";

const SPLIT_POLL_MS = 10_000;
const BAR_H = 18;

/** Frame B's thick bar: a deep→honey fill with a rounded end over the dark track (SVG width attribute: no inline style). */
function HoneyBar({ percent }: { percent: number | null }) {
  const p = percent === null ? 0 : Math.max(0, Math.min(100, percent));
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <div
      role="progressbar"
      aria-label="Job progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent === null ? undefined : Math.round(p)}
    >
      <svg aria-hidden="true" className="ov-hatch block h-[18px] w-full rounded-full">
        <defs>
          <linearGradient id={`jbfill${id}`} x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" className="ov-stop-deep" />
            <stop offset="1" className="ov-stop-honey" />
          </linearGradient>
        </defs>
        {p > 0 && (
          <svg x="0" y="0" width={`${String(p)}%`} height={BAR_H} overflow="visible">
            <rect className="ov-bar-fill" x="0" y="0" width="100%" height={BAR_H} rx={BAR_H / 2} fill={`url(#jbfill${id})`} />
          </svg>
        )}
      </svg>
    </div>
  );
}

function Fact({ label, children, field }: { label: string; children: ReactNode; field?: string }) {
  return (
    <div className="min-w-0">
      <dt className="nd-label">{label}</dt>
      <dd className="mt-2 text-[0.9375rem] text-fg tabular-nums" data-progress={field}>
        {children}
      </dd>
    </div>
  );
}

const STEPS = ["Waiting", "Sending", "Running", "Done"] as const;

/** The step a job state stands at (0 Waiting … 3 Done). */
function stepOf(state: string, mode: PlanView["mode"]): number {
  if (isTerminal(state)) return 3;
  if (state === "running" || state === "collecting") return 2;
  if (state === "preparing") return mode === "offload" ? 1 : 2;
  return 0;
}

/** Waiting → (Sending) → Running → Done, spread across the width and joined by thin lines. */
function Steps({ state, mode }: { state: string; mode: PlanView["mode"] }) {
  const at = stepOf(state, mode);
  const steps = STEPS.filter((st) => mode === "offload" || st !== "Sending");
  const last = state === "failed" ? "Failed" : state === "cancelled" ? "Cancelled" : "Done";
  return (
    <ol aria-label="Steps" className="flex items-center font-mono text-[0.6875rem] tracking-[0.14em] uppercase">
      {steps.map((st, i) => {
        const idx = STEPS.indexOf(st);
        const cur = idx === at;
        const reached = idx <= at;
        const stopped = idx === 3 && (state === "failed" || state === "cancelled");
        const tone = stopped ? (state === "failed" ? "text-status-failed" : "text-fg-muted") : cur ? "text-[#ffc93c]" : reached ? "text-fg" : "text-fg-subtle";
        const dot = stopped
          ? state === "failed"
            ? "bg-status-failed"
            : "bg-fg-muted"
          : cur
            ? "bg-[#ffc93c]"
            : reached
              ? "bg-fg"
              : "border border-fg-subtle";
        return (
          <li key={st} aria-current={cur ? "step" : undefined} className={`flex items-center ${i < steps.length - 1 ? "flex-1" : ""}`}>
            <span className="flex shrink-0 items-center gap-2.5">
              <span aria-hidden="true" className={`h-2 w-2 rounded-full ${dot}`} />
              <span className={tone}>{idx === 3 ? last : st}</span>
            </span>
            {i < steps.length - 1 && (
              <span aria-hidden="true" className={`jb-step-line mx-5 h-px flex-1 ${idx < at ? "bg-fg-muted" : "bg-[#3a3327]"}`} />
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** The split's nodes: what each works on now and how many chunks it finished. */
function LaneRows({ progress, nameOf }: { progress: SplitProgress; nameOf: (id: string) => string }) {
  if (progress.lanes.length === 0) return <p className="text-sm text-fg-muted">No node has taken a chunk yet.</p>;
  const total = progress.items_total;
  return (
    <div>
      <div className="grid grid-cols-[minmax(0,1fr)_8rem_8rem_8rem] items-baseline gap-x-8 pb-3">
        <span className="nd-label">Node</span>
        <span className="nd-label text-right">Working on</span>
        <span className="nd-label text-right">Done</span>
        <span className="nd-label text-right">Share</span>
      </div>
      <ul aria-label="Nodes of the split" className="nd-hair-t divide-y divide-[#ffffff0f]">
        {progress.lanes.map((l) => (
          <li key={l.attempt_id} className="grid grid-cols-[minmax(0,1fr)_8rem_8rem_8rem] items-baseline gap-x-8 py-3.5" data-lane-row={l.node_id}>
            <span className="truncate text-[0.9375rem] font-medium text-fg" title={l.node_id}>
              {nameOf(l.node_id)}
            </span>
            <span className="text-right font-mono text-[0.6875rem] tracking-[0.12em] text-fg-muted uppercase">
              {l.current.length > 0 ? l.current.map((c) => `chunk #${String(c.chunk_index)}`).join(" · ") : l.state}
            </span>
            <span className="text-right font-mono text-sm text-fg tabular-nums">
              {l.chunks_done}
              <span className="text-fg-muted"> chunks</span>
            </span>
            <span className="text-right font-mono text-sm text-fg-muted tabular-nums">
              {total > 0 ? `${String(Math.round((l.items_done * 100) / total))}%` : "—"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface JobProgressPanelProps {
  job: JobDetail;
  decisions: readonly DecisionOut[] | undefined;
  names: ReadonlyMap<string, string>;
}

export function JobProgressPanel({ job, decisions, names }: JobProgressPanelProps) {
  const terminal = isTerminal(job.state);
  const isSplit = isSplitJob(job.manifest);
  const split = useQuery({
    queryKey: jobKeys.split(job.job_id),
    queryFn: ({ signal }) => getSplit(job.job_id, signal),
    enabled: isSplit,
    refetchInterval: terminal ? false : SPLIT_POLL_MS,
  });
  const now = useServerNow();
  const nameOf = (id: string) => names.get(id) ?? id;

  const plan0 = planView(job, decisions, split.data?.predicted_s);
  const plan: PlanView = isSplit ? { ...plan0, mode: "split" } : plan0;
  const progress = progressOf(job, plan, split.data, now);

  // time: the split summary first, else from the job's own timestamps + the planner's estimate
  const startMs = Date.parse(startedAt(job));
  const endMs = terminal ? Date.parse(job.updated_at) : now;
  const elapsedS =
    split.data !== undefined
      ? liveElapsed(split.data, now)
      : Number.isNaN(startMs) || job.state === "submitted" || job.state === "planning" || job.state === "queued"
        ? null
        : Math.max(0, (endMs - startMs) / 1000);
  const estimateS = plan.speed?.chosenS ?? split.data?.predicted_s ?? null;
  const leftS = terminal
    ? null
    : split.data !== undefined
      ? (split.data.eta_s ?? null)
      : estimateS !== null && elapsedS !== null
        ? Math.max(0, estimateS - elapsedS)
        : null;

  let note: string | null = null;
  if (progress.percent === null) {
    if (job.state === "failed" || job.state === "cancelled") note = job.state;
    else if (isSplit && split.isPending) note = "loading the split";
    else if (isSplit && split.isError) note = "split progress unavailable";
    else if (!terminal && !["preparing", "running", "collecting"].includes(job.state)) note = "waiting to start";
    else note = "no planner estimate";
  }

  const runsOn =
    plan.nodeIds.length === 0 ? null : plan.mode === "split" ? plan.nodeIds.map(nameOf).join(", ") : nameOf(plan.nodeIds[0] ?? "");

  return (
    <section aria-label="Job progress" className="ov-card ov-frost flex flex-col gap-10 px-10 pt-9 pb-10" data-testid="job-progress">
      <div className="flex flex-col gap-5">
        <div className="flex items-end justify-between gap-6">
          <p className="ov-figure flex items-baseline text-[5rem] font-light text-fg" data-testid="progress-percent">
            {progress.percent === null ? "—" : Math.floor(progress.percent)}
            {progress.percent !== null && <span className="ml-1.5 text-2xl font-light text-fg-muted">%</span>}
          </p>
          <span className="mb-3 flex flex-col items-end gap-1.5 text-right">
            {plan.mode !== null && (
              <span className="flex items-center gap-2 font-mono text-[0.6875rem] font-medium tracking-[0.14em] whitespace-nowrap text-[#ffc93c] uppercase">
                <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-[#ffc93c]" />
                {MODE_TEXT[plan.mode]}
              </span>
            )}
            {(progress.estimated || note !== null) && (
              <span className="font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">{note ?? "estimated"}</span>
            )}
          </span>
        </div>
        <HoneyBar percent={progress.percent} />
      </div>

      <dl className="grid grid-cols-4 gap-x-10 gap-y-6">
        <Fact label="Elapsed" field="elapsed">
          {elapsedS === null ? "—" : formatDuration(elapsedS * 1000)}
        </Fact>
        <Fact label={progress.estimated || leftS !== null ? "Left · est." : "Left"} field="left">
          {leftS === null ? "—" : leftS < 1 ? <span className="text-fg-muted">past the estimate</span> : formatDuration(leftS * 1000)}
        </Fact>
        <Fact label="Planner estimate" field="estimate">
          {estimateS === null ? "—" : formatDuration(estimateS * 1000)}
        </Fact>
        {split.data !== undefined ? (
          <Fact label="Chunks done" field="chunks">
            {split.data.chunks.done}
            <span className="text-fg-muted"> / {split.data.chunks.total}</span>
          </Fact>
        ) : (
          <Fact label={plan.mode === "offload" ? "Sent to" : "Runs on"} field="node">
            {runsOn ?? "—"}
          </Fact>
        )}
      </dl>

      <div className="nd-hair-t pt-9">
        {isSplit ? (
          split.data !== undefined ? (
            <LaneRows progress={split.data} nameOf={nameOf} />
          ) : (
            <p className="text-sm text-fg-muted">{split.isError ? "Could not load the split progress." : "Loading the split progress…"}</p>
          )
        ) : (
          <div className="flex flex-col gap-7">
            <p className="text-[0.9375rem] text-fg">
              <span className="text-fg-muted">{plan.mode === "offload" ? "Sent to " : plan.mode === "local" ? (terminal ? "Ran on " : "Running on ") : "Not placed yet"}</span>
              {runsOn !== null && <span className="font-semibold">{runsOn}</span>}
            </p>
            <Steps state={job.state} mode={plan.mode} />
          </div>
        )}
      </div>
    </section>
  );
}
