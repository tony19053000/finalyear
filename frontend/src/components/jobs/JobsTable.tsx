import { useQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState, type Ref } from "react";
import { Link, useNavigate } from "react-router-dom";

import { cn } from "../../lib/cn";
import { formatAgo } from "../../lib/format";
import { jobKeys } from "../../pages/jobs/jobKeys";
import { getDecisions, type JobSummary } from "../../pages/jobs/jobsApi";
import {
  MODE_TEXT,
  durationMs,
  isTerminal,
  planView,
  progressOf,
  speedFigure,
  stateLight,
} from "../../pages/jobs/jobView";
import { getSplit } from "../../pages/jobs/splitApi";
import { formatDuration, formatTime } from "./format";
import { useWindowedRows } from "./useWindowedRows";

/** First guess of a row's height (the real one is measured once rendered). */
const DEFAULT_ROW_HEIGHT = 76;
const HEADERS = ["Job", "Mode", "Nodes", "Progress", "Speed-up", "Submitted", "Duration"] as const;
const COLUMNS = HEADERS.length;
/** A running split row re-reads its chunk counts this often (events are the fast path). */
const SPLIT_POLL_MS = 10_000;

export interface JobsTableProps {
  jobs: readonly JobSummary[];
  /** Called when the window nears the last loaded row (fetch the next page). */
  onNearEnd?: () => void;
  /** node_id -> name shown for the Nodes column. */
  names?: ReadonlyMap<string, string>;
  /** The controller's "now" (ms): drives "6 min ago", durations and estimates. */
  now: number;
  className?: string;
}

/**
 * The space of the rows not rendered. Sized by the cell's `height`
 * attribute - an HTML presentational hint, not a style: no `style=`, no
 * CSSOM write, so CSP `style-src 'self'` (D181) and the source-level
 * "no inline style" invariant both hold.
 */
function SpacerRow({ height }: { height: number }) {
  return (
    <tr aria-hidden="true" data-testid="jobs-spacer" className="jb-spacer-row">
      <td colSpan={COLUMNS} height={height} className="jb-spacer" />
    </tr>
  );
}

const dash = <span className="text-fg-subtle">—</span>;

/** A slim progress track with the honey fill (an SVG width attribute: no inline style). */
function RowBar({ percent }: { percent: number }) {
  const p = Math.max(0, Math.min(100, percent));
  return (
    <svg aria-hidden="true" className="mt-2 block h-[3px] w-full max-w-28 overflow-hidden rounded-full" preserveAspectRatio="none" viewBox="0 0 100 3">
      <rect x="0" y="0" width="100" height="3" rx="1.5" className="jb-track" />
      {p > 0 && <rect x="0" y="0" width={p} height="3" rx="1.5" className="jb-fill" />}
    </svg>
  );
}

interface RowProps {
  job: JobSummary;
  index: number;
  names: ReadonlyMap<string, string> | undefined;
  now: number;
  rowRef?: Ref<HTMLTableRowElement>;
}

/**
 * One job. Its mode, nodes, estimated progress and speed-up come from the
 * planner's decision (`GET /v1/jobs/{id}/decisions`, shared with the job
 * page's cache; a finished job's decision never changes) and, for a running
 * split, its chunk counts (`GET /v1/jobs/{id}/split`).
 */
function JobRow({ job, index, names, now, rowRef }: RowProps) {
  const navigate = useNavigate();
  const terminal = isTerminal(job.state);
  const decisions = useQuery({
    queryKey: jobKeys.decisions(job.job_id),
    queryFn: ({ signal }) => getDecisions(job.job_id, signal),
    staleTime: terminal ? Infinity : 15_000,
    retry: false,
  });
  const firstPlan = planView(job, decisions.data?.decisions);
  const split = useQuery({
    queryKey: jobKeys.split(job.job_id),
    queryFn: ({ signal }) => getSplit(job.job_id, signal),
    enabled: firstPlan.mode === "split" && !terminal,
    refetchInterval: SPLIT_POLL_MS,
    retry: false,
  });
  const plan = split.data ? planView(job, decisions.data?.decisions, split.data.predicted_s) : firstPlan;
  const progress = progressOf(job, plan, split.data, now);
  const speed = speedFigure(plan.speed);
  const nameOf = (id: string) => names?.get(id) ?? id;
  const shownNodes = plan.nodeIds.slice(0, 2).map(nameOf);
  const moreNodes = plan.nodeIds.length - shownNodes.length;
  const ms = durationMs(job, now);
  const ago = Date.parse(job.created_at);

  return (
    <tr
      ref={rowRef}
      aria-rowindex={index + 2}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("a") !== null) return;
        void navigate(`/jobs/${job.job_id}`);
      }}
      className="cursor-pointer"
    >
      <td className="align-middle">
        <span className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-2">
            <Link
              to={`/jobs/${job.job_id}`}
              className="truncate font-medium text-fg underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline"
              title={job.name}
            >
              {job.name}
            </Link>
            <span
              data-state={job.state}
              title={job.state}
              className={cn("h-2 w-2 shrink-0 rounded-full", stateLight(job.state))}
            >
              <span className="sr-only">{job.state}</span>
            </span>
          </span>
          <span className="truncate font-mono text-2xs text-fg-muted" title={job.job_id}>
            {job.job_id}
          </span>
        </span>
      </td>
      <td className="align-middle">
        {plan.mode === null ? (
          <span className="jb-tag text-fg-subtle" title="The planner has not placed this job yet">
            auto
          </span>
        ) : (
          <span className="jb-tag text-fg-muted" data-testid="job-mode">
            {MODE_TEXT[plan.mode]}
          </span>
        )}
      </td>
      <td className="align-middle">
        {shownNodes.length === 0 ? (
          dash
        ) : (
          <span className="block truncate" title={plan.nodeIds.map(nameOf).join(", ")}>
            {shownNodes.join(", ")}
            {moreNodes > 0 && <span className="ml-1.5 font-mono text-2xs text-fg-muted">+{moreNodes}</span>}
          </span>
        )}
      </td>
      <td className="align-middle">
        {progress.percent === null ? (
          job.state === "failed" || job.state === "cancelled" ? (
            <span className="text-fg-subtle">stopped</span>
          ) : (
            dash
          )
        ) : (
          <span className="flex flex-col">
            <span className="flex items-baseline gap-2 tabular-nums">
              <span className="font-light">
                {Math.floor(progress.percent)}
                <span className="ml-0.5 text-fg-muted">%</span>
              </span>
              {progress.estimated && <span className="jb-tag text-[0.5625rem] text-fg-subtle">est.</span>}
            </span>
            {!terminal && <RowBar percent={progress.percent} />}
          </span>
        )}
      </td>
      <td className="align-middle">
        {speed === null ? (
          dash
        ) : (
          <span className="flex items-baseline gap-2 tabular-nums" title="The planner's estimate">
            <span className="font-light">
              {speed.figure}
              <span className="ml-0.5 text-fg-muted">{speed.unit === "×" ? "×" : ` ${speed.unit}`}</span>
            </span>
            <span className="jb-tag text-[0.5625rem] text-fg-subtle">{speed.label === "saved" ? "saved" : "est."}</span>
          </span>
        )}
      </td>
      <td className="align-middle whitespace-nowrap text-fg-muted">
        <time dateTime={job.created_at} title={formatTime(job.created_at)}>
          {Number.isNaN(ago) ? "—" : formatAgo((now - ago) / 1000)}
        </time>
      </td>
      <td className="align-middle font-mono text-[0.8125rem] whitespace-nowrap tabular-nums">
        {ms === null ? dash : formatDuration(ms)}
      </td>
    </tr>
  );
}

/**
 * The jobs list table (0.8.5), in the Nodes list's frosted language: one
 * tall row per job, newest first. Over 200 rows only the visible window
 * (+ overscan) is in the DOM (D174). The job name is a link (keyboard: Tab
 * to it, Enter opens); a click anywhere on the row opens the job too.
 */
export function JobsTable({ jobs, onNearEnd, names, now, className }: JobsTableProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const header = useRef<HTMLTableSectionElement>(null);
  const firstRow = useRef<HTMLTableRowElement>(null);
  const [rowHeight, setRowHeight] = useState(DEFAULT_ROW_HEIGHT);
  const [offset, setOffset] = useState(0);
  const rows = useWindowedRows(scroller, jobs.length, { rowHeight, offset });

  // Measure the real row and header height once rendered (fonts, zoom).
  useLayoutEffect(() => {
    const row = firstRow.current;
    if (row !== null && row.offsetHeight > 0 && row.offsetHeight !== rowHeight) setRowHeight(row.offsetHeight);
    const head = header.current;
    if (head !== null && head.offsetHeight !== offset) setOffset(head.offsetHeight);
  }, [rowHeight, offset, jobs]);

  const nearEnd = rows.lastVisible >= 0 && rows.lastVisible >= jobs.length - 20;
  useEffect(() => {
    if (nearEnd) onNearEnd?.();
  }, [nearEnd, jobs.length, onNearEnd]);

  // The sticky header stays clear at the top; it takes its glass only once
  // rows have scrolled under it.
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const el = scroller.current;
    if (el === null) return;
    const update = () => {
      setScrolled(el.scrollTop > 2);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    return () => {
      el.removeEventListener("scroll", update);
    };
  }, []);

  const shown = jobs.slice(rows.start, rows.end);
  return (
    <div
      ref={scroller}
      className={cn("jb-scroll jb-list-scroll min-h-40 overflow-auto", className)}
      data-windowed={rows.windowed ? "true" : "false"}
      data-scrolled={scrolled ? "true" : "false"}
    >
      <table className="w-full border-collapse" aria-rowcount={jobs.length + 1}>
        <caption className="sr-only">Jobs, newest first</caption>
        <thead ref={header}>
          <tr aria-rowindex={1}>
            {HEADERS.map((title) => (
              <th
                key={title}
                scope="col"
                className="text-left font-mono font-medium tracking-[0.14em] whitespace-nowrap text-fg-subtle uppercase"
              >
                {title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.padTop > 0 && <SpacerRow height={rows.padTop} />}
          {shown.map((job, i) => (
            <JobRow
              key={job.job_id}
              job={job}
              index={rows.start + i}
              names={names}
              now={now}
              rowRef={i === 0 ? firstRow : undefined}
            />
          ))}
          {rows.padBottom > 0 && <SpacerRow height={rows.padBottom} />}
          {jobs.length === 0 && (
            <tr>
              <td colSpan={COLUMNS} className="text-center text-fg-muted">
                No jobs match.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
