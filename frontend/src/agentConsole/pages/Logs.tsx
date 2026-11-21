import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { consoleApi, consoleKeys, explain, type LogLineView } from "../api";
import { Dot, Panel, Problem } from "../components/fields";

const LIMIT = 500;
const TONE: Record<string, "failed" | "warn" | "off"> = { error: "failed", critical: "failed", warning: "warn", info: "off", debug: "off" };

function fieldsText(fields: LogLineView["fields"]): string {
  return Object.entries(fields)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join("  ");
}

/** The agent's own recent log (redacted by the agent before it gets here). */
export function Logs() {
  const [onlyProblems, setOnlyProblems] = useState(false);
  const logs = useQuery({ queryKey: consoleKeys.logs, queryFn: () => consoleApi.logs(LIMIT), refetchInterval: 3000 });
  if (logs.isPending) return <p role="status" className="ac-loading">Loading…</p>;
  if (logs.isError) return <Problem title="Could not load the log" {...explain(logs.error)} />;
  const lines = logs.data.lines.filter((l) => !onlyProblems || l.level === "warning" || l.level === "error" || l.level === "critical");
  return (
    <div className="flex flex-col gap-8">
    <div>
      <p className="nd-label">Logs</p>
      <p className="mt-3 font-display text-[3rem] leading-none font-bold tracking-[-0.04em] text-fg">What Hive did</p>
    </div>
    <Panel
      title="Recent log"
      className="ac-log"
      actions={
        <label className="flex items-center gap-3 text-sm text-fg-muted">
          Only problems
          <input type="checkbox" className="ac-switch" checked={onlyProblems} onChange={(e) => { setOnlyProblems(e.target.checked); }} />
        </label>
      }
    >
      <p className="ac-note pb-4">
        The newest {String(LIMIT)} lines of Hive on this laptop (secrets are removed). The full log: journalctl -u meshforge-agent
      </p>
      {lines.length === 0 ? (
        <p className="py-6 text-sm text-fg-muted">Nothing to show.</p>
      ) : (
        <ol className="jb-scroll ac-lines max-h-[64vh] overflow-auto font-mono text-[0.8125rem]" aria-label="Log lines">
          {[...lines].reverse().map((line) => (
            <li key={line.seq} className="ac-line grid grid-cols-1 gap-1 sm:grid-cols-[10.5rem_6.5rem_minmax(0,1fr)] sm:gap-6">
              <span className="shrink-0 text-fg-muted tabular-nums">{line.timestamp.replace("T", " ").slice(0, 19)}</span>
              <span className="shrink-0">
                <Dot tone={TONE[line.level] ?? "off"}>{line.level}</Dot>
              </span>
              <span className="min-w-0 break-words text-fg">
                {line.event}
                {Object.keys(line.fields).length > 0 && <span className="text-fg-muted">  {fieldsText(line.fields)}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
    </Panel>
    </div>
  );
}
