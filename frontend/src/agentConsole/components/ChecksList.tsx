import { useQuery } from "@tanstack/react-query";

import { Button } from "../../components/Button";
import { consoleApi, consoleKeys, explain, type CheckView } from "../api";
import { Dot, Problem } from "./fields";

const TONE: Record<CheckView["status"], "ok" | "warn" | "off"> = { ok: "ok", warn: "warn", missing: "off" };
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
    <div className="flex flex-col gap-4">
      <ul className="ac-checks flex flex-col" aria-label="Checks">
        {checks.data.checks.map((check) => (
          <li key={check.id} className="ac-check grid grid-cols-[minmax(0,1fr)_auto] gap-x-8 gap-y-1.5" data-check={check.id} data-status={check.status}>
            <span className="ac-row-label">{check.title}</span>
            <span className="ac-check-state text-sm">
              <Dot tone={TONE[check.status]}>{WORD[check.status]}</Dot>
            </span>
            <p className="ac-row-help col-span-2">{check.why}</p>
            {check.fix !== "" && <p className="ac-fix col-span-2">{check.fix}</p>}
            {check.action === "docker-access" && docker.data !== undefined && (
              <details className="ac-details col-span-2 text-sm text-fg-muted">
                <summary className="cursor-pointer text-fg">Why does this need my password?</summary>
                <p className="mt-1">{docker.data.explanation}</p>
                <p className="mt-1 font-mono text-xs break-all">{docker.data.command}</p>
              </details>
            )}
            {check.action === "restart" && onRestart !== undefined && (
              <div className="col-span-2">
                <Button size="sm" variant="ghost" className="ac-quiet" onClick={onRestart}>
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
          className="ac-quiet"
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
