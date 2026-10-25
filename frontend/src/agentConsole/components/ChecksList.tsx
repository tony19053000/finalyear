import { useQuery } from "@tanstack/react-query";

import { Badge } from "../../components/Status";
import { Button } from "../../components/Button";
import type { StatusTone } from "../../lib/status";
import { consoleApi, consoleKeys, explain, type CheckView } from "../api";
import { Problem } from "./fields";

const TONE: Record<CheckView["status"], StatusTone> = { ok: "ok", warn: "degraded", missing: "unknown" };
const WORD: Record<CheckView["status"], string> = { ok: "OK", warn: "Needs attention", missing: "Not available" };

export interface ChecksListProps {
  onRestart?: () => void;
}

/** D388: what this laptop can offer, checked by MeshForge itself - reports, never gates. */
export function ChecksList({ onRestart }: ChecksListProps) {
  const checks = useQuery({ queryKey: consoleKeys.checks, queryFn: consoleApi.checks, staleTime: 30_000 });
  const docker = useQuery({ queryKey: ["console", "docker-access"], queryFn: consoleApi.dockerAccess, staleTime: Infinity });
  if (checks.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Checking this laptop…
      </p>
    );
  }
  if (checks.isError) {
    const { message, fix } = explain(checks.error);
    return <Problem title="The checks could not run" message={message} fix={fix} />;
  }
  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col divide-y divide-border" aria-label="Checks">
        {checks.data.checks.map((check) => (
          <li key={check.id} className="flex flex-col gap-1 py-3" data-check={check.id} data-status={check.status}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-fg">{check.title}</span>
              <Badge tone={TONE[check.status]}>{WORD[check.status]}</Badge>
            </div>
            <p className="text-sm text-fg-muted">{check.why}</p>
            {check.fix !== "" && <p className="text-sm text-fg">{check.fix}</p>}
            {check.action === "docker-access" && docker.data !== undefined && (
              <details className="text-sm text-fg-muted">
                <summary className="cursor-pointer text-fg">Why does this need my password?</summary>
                <p className="mt-1">{docker.data.explanation}</p>
                <p className="mt-1 font-mono text-xs break-all">{docker.data.command}</p>
              </details>
            )}
            {check.action === "restart" && onRestart !== undefined && (
              <div>
                <Button size="sm" onClick={onRestart}>
                  Restart MeshForge
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
      <div>
        <Button
          size="sm"
          variant="ghost"
          loading={checks.isFetching}
          onClick={() => {
            void checks.refetch();
          }}
        >
          Check again
        </Button>
      </div>
    </div>
  );
}
