import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { StatusDot } from "../../components/Status";
import { useToast } from "../../components/toastContext";
import { consoleApi, consoleKeys, explain, type StatusView } from "../api";
import { ChecksList } from "../components/ChecksList";
import { Problem } from "../components/fields";
import { connectionSummary } from "../logic";

const STOP_HELP: Record<string, string> = {
  revoked: "The fabric admin removed this laptop. To come back, leave the fabric here and join again with a new code.",
  conflict: "Another MeshForge agent is using this laptop's identity. Stop the other one, then restart.",
  config: "Fix the setting named above (or ask whoever installed MeshForge), then restart.",
  config_reload: "Fix the configuration file, then restart.",
  identity: "See the Logs page for details.",
  version_unsupported:
    "The controller does not accept this MeshForge version. If the message says the agent is too old, install the newer MeshForge on this laptop; if it says the agent is newer, ask the fabric admin to upgrade the controller first. Then press Restart.",
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-0.5 py-1.5 sm:grid-cols-[10rem_1fr] sm:gap-3">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className="min-w-0 text-sm break-words text-fg">{children}</dd>
    </div>
  );
}

export function Home({ status }: { status: StatusView }) {
  const client = useQueryClient();
  const toast = useToast();
  const restart = useMutation({
    mutationFn: consoleApi.restart,
    onSuccess: (r) => {
      toast({ title: r.message || "Restarting", tone: "ok" });
      void client.invalidateQueries({ queryKey: consoleKeys.status });
    },
    onError: (err) => {
      toast({ title: "Restart failed", description: explain(err).message, tone: "failed" });
    },
  });
  const summary = connectionSummary(status);
  const reason = status.stop_reason;
  return (
    <div className="flex flex-col gap-4">
      {reason !== null && reason !== undefined && (
        <div className="flex flex-col gap-2">
          <Problem title="MeshForge is stopped on this laptop" message={reason.message} fix={STOP_HELP[reason.code] ?? ""} code={reason.code} />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="primary" loading={restart.isPending} onClick={() => { restart.mutate(); }}>
              Restart
            </Button>
            {reason.code === "revoked" && (
              <Link className="inline-flex h-7 items-center rounded-md border border-status-failed px-2.5 text-xs font-medium text-status-failed" to="/leave">
                Leave the fabric
              </Link>
            )}
          </div>
        </div>
      )}
      <Card title="This laptop" actions={<StatusDot tone={summary.tone} label={summary.label} />}>
        <dl className="divide-y divide-border">
          <Row label="Name">{status.node_name !== "" ? status.node_name : <span className="text-fg-muted">none (shown as {status.hostname})</span>}</Row>
          <Row label="Hostname">{status.hostname}</Row>
          <Row label="Fabric">{status.fabric_id ?? "-"}</Row>
          <Row label="Node id">
            <span className="font-mono text-xs">{status.node_id ?? "-"}</span>
          </Row>
          <Row label="Controller">{status.controller ?? "-"}</Row>
          {status.dashboard_url !== null && status.dashboard_url !== undefined && (
            <Row label="Dashboard">
              <a className="text-accent underline" href={status.dashboard_url} target="_blank" rel="noreferrer">
                {status.dashboard_url}
              </a>
            </Row>
          )}
          <Row label="Certificate valid until">{status.cert_not_after ?? "-"}</Row>
          <Row label="MeshForge version">{status.version}</Row>
        </dl>
        {status.identity_problem !== null && status.identity_problem !== undefined && (
          <Problem className="mt-3" title="This laptop's identity has a problem" message={status.identity_problem} />
        )}
      </Card>
      <Card title="Checks">
        <ChecksList onRestart={() => { restart.mutate(); }} />
      </Card>
    </div>
  );
}
