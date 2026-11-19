import { FrostTable } from "../../pages/nodes/FrostTable";
import type { AttemptSummary } from "../../pages/jobs/jobsApi";
import { cn } from "../../lib/cn";
import { between, formatBytes, formatDuration, formatTime } from "./format";
import { reconnectGraceText } from "./reconnectGrace";

/** Attempt states (common/states.py AttemptState) -> the flat light's colour. */
const ATTEMPT_LIGHT: Record<string, string> = {
  assigned: "bg-status-unknown",
  staging: "bg-status-busy",
  env_ready: "bg-status-busy",
  running: "bg-status-busy",
  uploading: "bg-status-busy",
  done: "bg-status-ok",
  failed: "bg-status-failed",
  lost: "bg-status-degraded",
  fenced: "bg-status-degraded",
};

/** V0.10: what a non-``run`` attempt does for its split job. */
const ROLE_HELP: Record<string, string> = {
  lane: "one node's share of a split job (it ran chunks)",
  combine: "combined the chunks' results into the job's output",
};

const TH = "text-left font-mono font-medium tracking-[0.14em] whitespace-nowrap text-fg-subtle uppercase";
const none = <span className="text-fg-subtle">—</span>;

function Head({ titles }: { titles: readonly string[] }) {
  return (
    <thead>
      <tr>
        {titles.map((h) => (
          <th key={h} scope="col" className={TH}>
            {h}
          </th>
        ))}
      </tr>
    </thead>
  );
}

/** Every attempt of the job, newest (highest fencing token) first, in the node page's frosted table. */
export function AttemptsTable({ attempts, names }: { attempts: readonly AttemptSummary[]; names?: ReadonlyMap<string, string> }) {
  if (attempts.length === 0) {
    return <p className="text-sm text-fg-muted">No attempt yet - the job has not been placed on a node.</p>;
  }
  const ordered = [...attempts].sort((a, b) => b.fencing_token - a.fencing_token);
  const nameOf = (id: string) => names?.get(id);
  return (
    <div className="flex flex-col gap-12">
      <FrostTable className="jb-att" testId="attempts-card">
        <table className="w-full border-collapse">
          <caption className="sr-only">Attempts, newest first</caption>
          <Head titles={["Attempt", "Node", "State", "Result", "Started", "Ran for", "Logs"]} />
          <tbody>
            {ordered.map((a) => {
              const grace = reconnectGraceText(a.reconnect_grace_until);
              const name = nameOf(a.node_id);
              const logs = Object.entries(a.logs ?? {});
              return (
                <tr key={a.attempt_id} className="align-top">
                  <td>
                    <span className="flex flex-col">
                      <span className="font-mono text-[0.8125rem] text-fg">
                        <span className="mr-2 text-fg-subtle tabular-nums">#{a.fencing_token}</span>
                        <span className="break-all">{a.attempt_id}</span>
                      </span>
                      {a.role !== "run" && (
                        <span className="jb-tag mt-1 text-[0.625rem] text-fg-muted" title={ROLE_HELP[a.role]}>
                          {a.role}
                        </span>
                      )}
                    </span>
                  </td>
                  <td>
                    <span className="flex flex-col">
                      <span className="truncate">{name ?? a.node_id}</span>
                      {name !== undefined && <span className="truncate font-mono text-2xs text-fg-muted">{a.node_id}</span>}
                    </span>
                  </td>
                  <td>
                    <span className="inline-flex items-center gap-2.5">
                      <span aria-hidden="true" className={cn("nd-light", ATTEMPT_LIGHT[a.state] ?? "bg-status-unknown")} />
                      {a.state}
                    </span>
                    {grace !== "" && (
                      <span className="mt-1 block text-2xs text-fg-muted" role="status">
                        {grace}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="font-mono text-[0.8125rem] tabular-nums">
                      {a.exit_code == null ? none : `exit ${String(a.exit_code)}`}
                    </span>
                    {a.failure_type != null && <span className="block font-mono text-2xs text-status-failed">{a.failure_type}</span>}
                    {a.detail != null && (
                      <span className="mt-0.5 block text-2xs break-words text-fg-muted" title={a.detail}>
                        {a.detail}
                      </span>
                    )}
                  </td>
                  <td className="font-mono text-[0.8125rem] whitespace-nowrap tabular-nums">{formatTime(a.started_at)}</td>
                  <td className="font-mono text-[0.8125rem] whitespace-nowrap tabular-nums">
                    {formatDuration(between(a.started_at, a.ended_at))}
                  </td>
                  <td className="text-[0.8125rem]">
                    {logs.length === 0
                      ? none
                      : logs.map(([stream, log]) => (
                          <span key={stream} className="block whitespace-nowrap">
                            <span className="jb-tag mr-2 text-[0.625rem] text-fg-subtle">{stream}</span>
                            <span className="font-mono tabular-nums">{formatBytes(log.bytes)}</span>
                            {log.truncated ? <span className="text-fg-muted"> (truncated)</span> : ""}
                            {log.digest == null ? <span className="text-fg-muted"> (live only)</span> : ""}
                          </span>
                        ))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </FrostTable>
      {ordered.some((a) => (a.transfers ?? []).length > 0) && (
        <FrostTable title="Staging transfers" className="jb-xfer" testId="transfers-card">
          <table className="w-full border-collapse">
            <caption className="sr-only">Staging transfers (per tree and source)</caption>
            <Head titles={["Attempt", "Tree", "Source", "Bytes", "Time", "Units", "Failures", "Resumed", "Dropped"]} />
            <tbody>
              {ordered.flatMap((a) =>
                (a.transfers ?? []).map((t, i) => (
                  <tr key={`${a.attempt_id}-${String(i)}`} className="font-mono text-[0.8125rem] tabular-nums">
                    <td>#{a.fencing_token}</td>
                    <td className="truncate" title={t.tree_id}>
                      {t.tree_id.replace(/^sha256:/, "").slice(0, 12)}…
                    </td>
                    <td>{t.source}</td>
                    <td>{formatBytes(t.bytes)}</td>
                    <td>{formatDuration(t.duration_ms)}</td>
                    <td>{t.units}</td>
                    <td>{t.failures}</td>
                    <td>{t.resumed_chunks}</td>
                    <td>{t.dropped ?? "—"}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </FrostTable>
      )}
    </div>
  );
}
