import { FrostTable } from "../../pages/nodes/FrostTable";
import type { TransitionOut } from "../../pages/jobs/jobsApi";
import { stateLight } from "../../pages/jobs/jobView";
import { between, formatDuration, formatTime } from "./format";

const TH = "text-left font-mono font-medium tracking-[0.14em] whitespace-nowrap text-fg-subtle uppercase";

/**
 * The job's state history (job_transition rows, oldest first) with the time
 * spent in each state, in the node page's frosted table. The last state's
 * time runs until `now`.
 */
export function JobTimeline({ transitions, terminal, now }: { transitions: readonly TransitionOut[]; terminal: boolean; now: string }) {
  const ordered = [...transitions].sort((a, b) => a.seq - b.seq);
  if (ordered.length === 0) return <p className="text-sm text-fg-muted">No state changes recorded.</p>;
  return (
    <FrostTable className="jb-states" variant="plain" testId="timeline-card">
      <table className="w-full border-collapse">
        <caption className="sr-only">State timeline</caption>
        <thead>
          <tr>
            {["At", "State", "From", "Time in state"].map((h) => (
              <th key={h} scope="col" className={TH}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ordered.map((t, i) => {
            const next = ordered[i + 1];
            const until = next?.at ?? (terminal ? null : now);
            const spent = until === null ? null : between(t.at, until);
            return (
              <tr key={t.seq}>
                <td className="font-mono text-[0.8125rem] whitespace-nowrap text-fg-muted tabular-nums">{formatTime(t.at)}</td>
                <td>
                  <span className="inline-flex items-center gap-2.5">
                    <span aria-hidden="true" className={`nd-light ${stateLight(t.to_state)}`} />
                    {t.to_state}
                  </span>
                </td>
                <td className="text-fg-muted">{t.from_state ?? <span className="text-fg-subtle">—</span>}</td>
                <td className="font-mono text-[0.8125rem] whitespace-nowrap tabular-nums">
                  {spent === null ? (
                    <span className="text-fg-subtle">{terminal && next === undefined ? "final" : "—"}</span>
                  ) : (
                    <>
                      {formatDuration(spent)}
                      {next === undefined && <span className="ml-1.5 text-fg-muted"> so far</span>}
                    </>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </FrostTable>
  );
}
