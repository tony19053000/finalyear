import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { jobTone } from "../../lib/status";
import { cn } from "../../lib/cn";
import { StatusDot } from "../Status";
import type { JobSummary } from "../../pages/jobs/jobsApi";
import { formatTime } from "./format";
import { useWindowedRows } from "./useWindowedRows";

/** Rows are one line each, so every row has the same height (windowing). */
const DEFAULT_ROW_HEIGHT = 37;
const COLUMNS = 7;

export interface JobsTableProps {
  jobs: readonly JobSummary[];
  /** Called when the window nears the last loaded row (fetch the next page). */
  onNearEnd?: () => void;
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
    <tr aria-hidden="true" data-testid="jobs-spacer">
      <td colSpan={COLUMNS} height={height} className="p-0" />
    </tr>
  );
}

function placementText(job: JobSummary): string {
  const { mode, node_id } = job.placement;
  if (mode === "auto") return job.placement.policy ? `auto · ${job.placement.policy}` : "auto";
  return node_id ? `${mode} · ${node_id}` : mode;
}

/**
 * The jobs list table (0.8.5): one line per job, newest first. Over 200
 * rows only the visible window (+ overscan) is in the DOM (D174). The job
 * name is a link (keyboard: Tab to it, Enter opens); a click anywhere on the
 * row opens the job too.
 */
export function JobsTable({ jobs, onNearEnd, className }: JobsTableProps) {
  const navigate = useNavigate();
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

  const shown = jobs.slice(rows.start, rows.end);
  return (
    <div
      ref={scroller}
      className={cn("max-h-[70vh] overflow-auto rounded-lg border border-border bg-surface", className)}
      data-windowed={rows.windowed ? "true" : "false"}
    >
      <table className="w-full border-collapse text-sm" aria-rowcount={jobs.length + 1}>
        <caption className="sr-only">Jobs, newest first</caption>
        <thead ref={header} className="sticky top-0 z-10 bg-surface">
          <tr className="border-b border-border" aria-rowindex={1}>
            {["Job", "State", "Placement", "Priority", "Created", "Updated", "Job id"].map((title) => (
              <th
                key={title}
                scope="col"
                className={cn(
                  "h-8 px-3 text-2xs font-medium tracking-wide whitespace-nowrap text-fg-subtle uppercase",
                  title === "Priority" ? "text-right" : "text-left",
                )}
              >
                {title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.padTop > 0 && <SpacerRow height={rows.padTop} />}
          {shown.map((job, i) => {
            const index = rows.start + i;
            return (
              <tr
                key={job.job_id}
                ref={i === 0 ? firstRow : undefined}
                aria-rowindex={index + 2}
                onClick={(event) => {
                  if ((event.target as HTMLElement).closest("a") !== null) return;
                  void navigate(`/jobs/${job.job_id}`);
                }}
                className="cursor-pointer border-b border-border hover:bg-surface-raised"
              >
                <td className="h-9 max-w-64 truncate px-3 align-middle whitespace-nowrap">
                  <Link
                    to={`/jobs/${job.job_id}`}
                    className="font-medium text-fg underline-offset-2 hover:underline"
                  >
                    {job.name}
                  </Link>
                </td>
                <td className="h-9 px-3 align-middle whitespace-nowrap">
                  <StatusDot tone={jobTone(job.state)} label={job.state} />
                </td>
                <td className="h-9 max-w-72 truncate px-3 align-middle font-mono text-xs whitespace-nowrap text-fg-muted">
                  {placementText(job)}
                </td>
                <td className="h-9 px-3 text-right align-middle font-mono text-xs tabular-nums">{job.priority}</td>
                <td className="h-9 px-3 align-middle font-mono text-xs whitespace-nowrap tabular-nums">
                  {formatTime(job.created_at)}
                </td>
                <td className="h-9 px-3 align-middle font-mono text-xs whitespace-nowrap tabular-nums">
                  {formatTime(job.updated_at)}
                </td>
                <td className="h-9 px-3 align-middle font-mono text-xs whitespace-nowrap text-fg-muted">{job.job_id}</td>
              </tr>
            );
          })}
          {rows.padBottom > 0 && <SpacerRow height={rows.padBottom} />}
          {jobs.length === 0 && (
            <tr>
              <td colSpan={COLUMNS} className="px-3 py-6 text-center text-fg-muted">
                No jobs match.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
