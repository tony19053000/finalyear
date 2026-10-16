import { jobTone } from "../../lib/status";
import { StatusDot } from "../Status";
import type { TransitionOut } from "../../pages/jobs/jobsApi";
import { between, formatDuration, formatTime } from "./format";

/**
 * The job's state history (job_transition rows, oldest first) with the time
 * spent in each state. The last state's time runs until `now`.
 */
export function JobTimeline({ transitions, terminal, now }: { transitions: readonly TransitionOut[]; terminal: boolean; now: string }) {
  const ordered = [...transitions].sort((a, b) => a.seq - b.seq);
  if (ordered.length === 0) return <p className="text-sm text-fg-muted">No state changes recorded.</p>;
  return (
    <ol aria-label="State timeline" className="flex flex-col">
      {ordered.map((t, i) => {
        const next = ordered[i + 1];
        const until = next?.at ?? (terminal ? null : now);
        const spent = until === null ? null : between(t.at, until);
        return (
          <li key={t.seq} className="grid grid-cols-[10rem_1fr_auto] items-center gap-3 border-l-2 border-border py-1.5 pl-3">
            <span className="font-mono text-xs text-fg-muted tabular-nums">{formatTime(t.at)}</span>
            <span className="text-sm">
              <StatusDot tone={jobTone(t.to_state)} label={t.to_state} />
              {t.from_state != null && <span className="ml-2 text-xs text-fg-subtle">from {t.from_state}</span>}
            </span>
            <span className="font-mono text-xs text-fg-muted tabular-nums">
              {spent === null ? (terminal && next === undefined ? "final" : "—") : `${formatDuration(spent)}${next === undefined ? " so far" : ""}`}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
