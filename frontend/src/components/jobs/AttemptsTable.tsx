import { Badge } from "../Status";
import type { AttemptSummary } from "../../pages/jobs/jobsApi";
import type { StatusTone } from "../../lib/status";
import { between, formatBytes, formatDuration, formatTime } from "./format";

/** Attempt states (common/states.py AttemptState) -> tone. */
const ATTEMPT_TONES: Record<string, StatusTone> = {
  assigned: "unknown",
  staging: "busy",
  env_ready: "busy",
  running: "busy",
  uploading: "busy",
  done: "ok",
  failed: "failed",
  lost: "degraded",
  fenced: "degraded",
};

function attemptTone(state: string): StatusTone {
  return ATTEMPT_TONES[state] ?? "unknown";
}

/** Every attempt of the job, newest (highest fencing token) first. */
export function AttemptsTable({ attempts }: { attempts: readonly AttemptSummary[] }) {
  if (attempts.length === 0) {
    return <p className="text-sm text-fg-muted">No attempt yet - the job has not been placed on a node.</p>;
  }
  const ordered = [...attempts].sort((a, b) => b.fencing_token - a.fencing_token);
  return (
    <div className="flex flex-col gap-4">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">Attempts, newest first</caption>
          <thead>
            <tr className="border-b border-border">
              {["#", "Attempt", "Node", "State", "Failure", "Exit", "Started", "Ran for", "Logs"].map((h) => (
                <th
                  key={h}
                  scope="col"
                  className="h-8 px-3 text-left text-2xs font-medium tracking-wide whitespace-nowrap text-fg-subtle uppercase"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ordered.map((a) => (
              <tr key={a.attempt_id} className="border-b border-border last:border-b-0 align-top">
                <td className="px-3 py-2 font-mono text-xs tabular-nums">{a.fencing_token}</td>
                <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{a.attempt_id}</td>
                <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{a.node_id}</td>
                <td className="px-3 py-2">
                  <Badge tone={attemptTone(a.state)}>{a.state}</Badge>
                </td>
                <td className="px-3 py-2 text-xs">
                  {a.failure_type != null ? <span className="font-mono">{a.failure_type}</span> : "—"}
                  {a.detail != null && <div className="mt-0.5 max-w-80 break-words text-fg-muted">{a.detail}</div>}
                </td>
                <td className="px-3 py-2 font-mono text-xs tabular-nums">{a.exit_code ?? "—"}</td>
                <td className="px-3 py-2 font-mono text-xs whitespace-nowrap tabular-nums">{formatTime(a.started_at)}</td>
                <td className="px-3 py-2 font-mono text-xs whitespace-nowrap tabular-nums">
                  {formatDuration(between(a.started_at, a.ended_at))}
                </td>
                <td className="px-3 py-2 text-xs whitespace-nowrap">
                  {Object.keys(a.logs ?? {}).length === 0
                    ? "—"
                    : Object.entries(a.logs ?? {}).map(([stream, log]) => (
                        <div key={stream}>
                          {stream} {formatBytes(log.bytes)}
                          {log.truncated ? " (truncated)" : ""}
                          {log.digest == null ? " (live only)" : ""}
                        </div>
                      ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {ordered.some((a) => (a.transfers ?? []).length > 0) && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <caption className="mb-1 text-left text-xs font-semibold text-fg">Staging transfers (per tree and source)</caption>
            <thead>
              <tr className="border-b border-border">
                {["Attempt", "Tree", "Source", "Bytes", "Time", "Units", "Failures", "Resumed", "Dropped"].map((h) => (
                  <th
                    key={h}
                    scope="col"
                    className="h-8 px-3 text-left text-2xs font-medium tracking-wide whitespace-nowrap text-fg-subtle uppercase"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ordered.flatMap((a) =>
                (a.transfers ?? []).map((t, i) => (
                  <tr key={`${a.attempt_id}-${String(i)}`} className="border-b border-border last:border-b-0">
                    <td className="px-3 py-1.5 font-mono text-xs">{a.fencing_token}</td>
                    <td className="px-3 py-1.5 font-mono text-xs">{t.tree_id.replace(/^sha256:/, "").slice(0, 12)}…</td>
                    <td className="px-3 py-1.5 font-mono text-xs">{t.source}</td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular-nums">{formatBytes(t.bytes)}</td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular-nums">{formatDuration(t.duration_ms)}</td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular-nums">{t.units}</td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular-nums">{t.failures}</td>
                    <td className="px-3 py-1.5 font-mono text-xs tabular-nums">{t.resumed_chunks}</td>
                    <td className="px-3 py-1.5 font-mono text-xs">{t.dropped ?? "—"}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
