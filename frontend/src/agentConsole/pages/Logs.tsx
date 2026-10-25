import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Status";
import { Card } from "../../components/Card";
import type { StatusTone } from "../../lib/status";
import { consoleApi, consoleKeys, explain, type LogLineView } from "../api";
import { Problem } from "../components/fields";

const LIMIT = 500;
const TONE: Record<string, StatusTone> = { error: "failed", critical: "failed", warning: "degraded", info: "unknown", debug: "unknown" };

function fieldsText(fields: LogLineView["fields"]): string {
  return Object.entries(fields)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join("  ");
}

/** The agent's own recent log (redacted by the agent before it gets here). */
export function Logs() {
  const [onlyProblems, setOnlyProblems] = useState(false);
  const logs = useQuery({ queryKey: consoleKeys.logs, queryFn: () => consoleApi.logs(LIMIT), refetchInterval: 3000 });
  if (logs.isPending) return <p role="status" className="text-sm text-fg-muted">Loading…</p>;
  if (logs.isError) return <Problem title="Could not load the log" {...explain(logs.error)} />;
  const lines = logs.data.lines.filter((l) => !onlyProblems || l.level === "warning" || l.level === "error" || l.level === "critical");
  return (
    <Card
      title="Recent log"
      actions={
        <label className="flex items-center gap-1.5 text-sm text-fg-muted">
          <input type="checkbox" checked={onlyProblems} onChange={(e) => { setOnlyProblems(e.target.checked); }} />
          Only problems
        </label>
      }
      flush
    >
      <p className="border-b border-border px-4 py-2 text-xs text-fg-muted">
        The newest {String(LIMIT)} lines of MeshForge on this laptop (secrets are removed). The full log: journalctl -u meshforge-agent
      </p>
      {lines.length === 0 ? (
        <p className="p-4 text-sm text-fg-muted">Nothing to show.</p>
      ) : (
        <ol className="max-h-[70vh] overflow-auto font-mono text-xs" aria-label="Log lines">
          {[...lines].reverse().map((line) => (
            <li key={line.seq} className="flex flex-col gap-0.5 border-b border-border px-4 py-1.5 sm:flex-row sm:gap-3">
              <span className="shrink-0 text-fg-subtle">{line.timestamp.replace("T", " ").slice(0, 19)}</span>
              <span className="shrink-0">
                <Badge tone={TONE[line.level] ?? "neutral"}>{line.level}</Badge>
              </span>
              <span className="min-w-0 break-words text-fg">
                {line.event}
                {Object.keys(line.fields).length > 0 && <span className="text-fg-muted">  {fieldsText(line.fields)}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
