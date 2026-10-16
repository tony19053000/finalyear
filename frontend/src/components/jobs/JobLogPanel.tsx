import { Badge } from "../Status";
import { Button } from "../Button";
import { LogView } from "../CodeBlock";
import type { StatusTone } from "../../lib/status";
import type { LogSnapshot, LogStatus } from "../../pages/jobs/logFollower";
import { formatBytes } from "./format";

const STATUS: Record<LogStatus, { label: string; tone: StatusTone }> = {
  connecting: { label: "Connecting", tone: "unknown" },
  live: { label: "Live", tone: "ok" },
  paused: { label: "Paused", tone: "unknown" },
  reconnecting: { label: "Reconnecting", tone: "degraded" },
  ended: { label: "Ended", tone: "unknown" },
  unauthorized: { label: "Signed out", tone: "failed" },
  not_found: { label: "Unavailable", tone: "failed" },
  unavailable: { label: "Unavailable", tone: "failed" },
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

/** The terminal-style live log of a job (0.8.5): follow / pause, bounded. */
export function JobLogPanel({ snapshot, maxLines, onPause, onResume, downloadHref, downloadName }: JobLogPanelProps) {
  const status = STATUS[snapshot.status];
  const canPause = snapshot.status === "live" || snapshot.status === "connecting" || snapshot.status === "reconnecting";
  const end = snapshot.end;
  const gapStreams = end === null ? [] : Object.entries(end.gaps).filter(([, ranges]) => ranges.length > 0);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-fg-muted">Log stream:</span>
          <Badge tone={status.tone}>{status.label}</Badge>
          <span className="font-mono text-2xs text-fg-subtle">
            stdout {formatBytes(snapshot.offsets.stdout)} · stderr {formatBytes(snapshot.offsets.stderr)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {canPause && (
            <Button size="sm" onClick={onPause}>
              Pause
            </Button>
          )}
          {snapshot.status === "paused" && (
            <Button size="sm" variant="primary" onClick={onResume}>
              Resume
            </Button>
          )}
          <a
            href={downloadHref}
            download={downloadName}
            className="inline-flex h-7 items-center rounded-md border border-border-strong bg-surface-raised px-2.5 text-xs font-medium text-fg hover:bg-surface-sunken"
          >
            Download full log
          </a>
        </div>
      </div>
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
      <LogView label="Job output" lines={snapshot.lines} maxLines={maxLines} className="h-96" />
      {end !== null && (
        <p className="text-xs text-fg-muted" data-testid="log-end">
          Job {end.state ?? "ended"}
          {end.exitCode !== null ? ` · exit code ${end.exitCode}` : ""}
          {end.failureType !== null ? ` · ${end.failureType}` : ""}
          {gapStreams.length > 0
            ? ` · the live log missed some bytes of ${gapStreams.map(([s]) => s).join(" and ")} (no final log replaced them)`
            : ""}
        </p>
      )}
    </div>
  );
}
