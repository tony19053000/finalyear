import { LogView } from "../CodeBlock";
import type { LogSnapshot, LogStatus } from "../../pages/jobs/logFollower";
import { formatBytes } from "./format";

const STATUS: Record<LogStatus, { label: string; light: string; breathe?: boolean }> = {
  connecting: { label: "Connecting", light: "bg-status-unknown", breathe: true },
  live: { label: "Live", light: "bg-status-ok", breathe: true },
  paused: { label: "Paused", light: "bg-status-unknown" },
  reconnecting: { label: "Reconnecting", light: "bg-status-degraded", breathe: true },
  ended: { label: "Ended", light: "bg-status-unknown" },
  unauthorized: { label: "Signed out", light: "bg-status-failed" },
  not_found: { label: "Unavailable", light: "bg-status-failed" },
  unavailable: { label: "Unavailable", light: "bg-status-failed" },
};

export interface JobLogPanelProps {
  snapshot: LogSnapshot;
  maxLines: number;
  onPause: () => void;
  onResume: () => void;
  /** GET /v1/jobs/{id}/logs - the whole persisted log as text. */
  downloadHref: string;
  downloadName: string;
}

/** The job's live log (0.8.5): a dark mono well in a frosted card; follow / pause, bounded. */
export function JobLogPanel({ snapshot, maxLines, onPause, onResume, downloadHref, downloadName }: JobLogPanelProps) {
  const status = STATUS[snapshot.status];
  const canPause = snapshot.status === "live" || snapshot.status === "connecting" || snapshot.status === "reconnecting";
  const end = snapshot.end;
  const gapStreams = end === null ? [] : Object.entries(end.gaps).filter(([, ranges]) => ranges.length > 0);
  return (
    <section aria-label="Job log" className="ov-card ov-frost nd-glass flex flex-col gap-5 px-9 pt-7 pb-8">
      <header className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div className="flex items-center gap-6">
          <h2 className="ov-card-title">Log</h2>
          <span className="flex items-center gap-2 font-mono text-[0.75rem] tracking-[0.14em] text-fg uppercase" data-testid="log-status">
            <span aria-hidden="true" className={`nd-light nd-light-sm ${status.light} ${status.breathe === true ? "nd-breathe" : ""}`} />
            {status.label}
          </span>
          <span className="font-mono text-[0.75rem] tracking-[0.08em] text-fg-muted uppercase">
            stdout {formatBytes(snapshot.offsets.stdout)} · stderr {formatBytes(snapshot.offsets.stderr)}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {canPause && (
            <button type="button" className="jb-quiet-btn" onClick={onPause}>
              Pause
            </button>
          )}
          {snapshot.status === "paused" && (
            <button type="button" className="jb-quiet-btn text-accent" onClick={onResume}>
              Resume
            </button>
          )}
          <a href={downloadHref} download={downloadName} className="jb-quiet-btn">
            Download full log
          </a>
        </div>
      </header>
      {snapshot.detail !== null && (
        <p role="status" className="text-xs text-fg-muted">
          {snapshot.detail}
        </p>
      )}
      {snapshot.dropped > 0 && (
        <p className="text-xs text-fg-muted">
          Showing the last {maxLines} lines; {snapshot.dropped} earlier lines are only in the full log.
        </p>
      )}
      <LogView label="Job output" lines={snapshot.lines} maxLines={maxLines} className="h-[28rem]" viewClassName="jb-log h-full min-h-40" />
      {end !== null && (
        <p className="font-mono text-[0.8125rem] tracking-[0.06em] text-fg" data-testid="log-end">
          Job {end.state ?? "ended"}
          {end.exitCode !== null ? ` · exit code ${end.exitCode}` : ""}
          {end.failureType !== null ? ` · ${end.failureType}` : ""}
          {gapStreams.length > 0
            ? ` · the live log missed some bytes of ${gapStreams.map(([s]) => s).join(" and ")} (no final log replaced them)`
            : ""}
        </p>
      )}
    </section>
  );
}
