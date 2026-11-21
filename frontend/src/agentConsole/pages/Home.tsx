import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Button } from "../../components/Button";
import { useToast } from "../../components/toastContext";
import { consoleApi, consoleKeys, explain, type StatusView } from "../api";
import { ChecksList } from "../components/ChecksList";
import { ArrowCorner, FigureCard, Panel, Problem } from "../components/fields";

const STOP_HELP: Record<string, string> = {
  revoked: "The fabric admin removed this laptop. To come back, leave the fabric here and join again with a new code.",
  conflict: "Another MeshForge agent is using this laptop's identity. Stop the other one, then restart.",
  config: "Fix the setting named above (or ask whoever installed MeshForge), then restart.",
  config_reload: "Fix the configuration file, then restart.",
  identity: "See the Logs page for details.",
  version_unsupported:
    "The controller does not accept this MeshForge version. If the message says the agent is too old, install the newer MeshForge on this laptop; if it says the agent is newer, ask the fabric admin to upgrade the controller first. Then press Restart.",
};

const UPGRADE_HELP: Record<string, string> = {
  rolled_back: "MeshForge keeps working on the previous version. Fix the cause the installer named, then install the new version again.",
  interrupted: "Finish the upgrade: on Linux install the new package again (sudo dpkg -i ./<new .deb>), on Windows run the installer again.",
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ac-kv grid grid-cols-1 gap-1 sm:grid-cols-[13rem_minmax(0,1fr)] sm:gap-6">
      <dt className="nd-label self-center">{label}</dt>
      <dd className="min-w-0 text-[0.9375rem] break-words text-fg">{children}</dd>
    </div>
  );
}

function daysLeft(iso: string | null | undefined): string {
  if (iso === null || iso === undefined) return "-";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return String(Math.max(0, Math.round((t - Date.now()) / 864e5)));
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
  const reason = status.stop_reason;
  return (
    <div className="flex flex-col gap-10">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div>
          <p className="nd-label">On this laptop</p>
          <h1 className="mt-3 font-display text-[3rem] leading-none font-bold tracking-[-0.04em] text-fg">
            {status.node_name !== "" ? status.node_name : status.hostname}
          </h1>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-[1.25fr_1fr_1fr_1fr]">
      <section className="ov-card ov-card-honey ac-hero flex flex-col justify-between" aria-label="Sharing">
        <p className="ac-hero-label">This laptop in the hive</p>
        <div>
          <p className="ac-hero-big">{status.stop_reason != null ? "Stopped" : status.connection === "ONLINE" ? "Sharing" : "Waiting"}</p>
          <p className="ac-hero-sub">
            {status.stop_reason != null
              ? "MeshForge is not running here."
              : status.connection === "ONLINE"
                ? "Connected and ready for the work the hive sends."
                : "Reaching for the controller."}
          </p>
        </div>
      </section>
      <FigureCard title="Certificate" sub={status.cert_not_after != null ? `valid until ${status.cert_not_after.slice(0, 10)}` : "not issued"}>
        {daysLeft(status.cert_not_after)}
        <span className="ml-1.5 text-2xl font-light text-fg-muted">days</span>
      </FigureCard>
      <FigureCard title="Controller" sub="the fabric's coordinator">
        <span className="ac-fig-text">{(status.controller ?? "-").split(":")[0]?.split(".")[0]}</span>
      </FigureCard>
      <FigureCard title="Version" sub={status.install_mode === "system" ? "installed for everyone" : "installed for you"}>
        {status.version}
      </FigureCard>
      </div>
      {reason !== null && reason !== undefined && (
        <div className="flex flex-col gap-2">
          <Problem title="MeshForge is stopped on this laptop" message={reason.message} fix={STOP_HELP[reason.code] ?? ""} code={reason.code} />
          <div className="flex flex-wrap gap-3">
            <Button size="sm" variant="primary" className="ac-go" loading={restart.isPending} onClick={() => { restart.mutate(); }}>
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
      {status.upgrade !== null && status.upgrade !== undefined && status.upgrade.kind !== "in_progress" && (
        <Problem
          title={status.upgrade.kind === "rolled_back" ? "The last upgrade was rolled back" : "An upgrade was interrupted"}
          message={status.upgrade.message}
          fix={status.upgrade.fix !== "" ? status.upgrade.fix : UPGRADE_HELP[status.upgrade.kind]}
          code={status.upgrade.kind}
        />
      )}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
      <Panel
        title="This laptop"
        corner={
          status.dashboard_url != null ? (
            <a className="ov-icon-btn" href={status.dashboard_url} target="_blank" rel="noreferrer" aria-label="Open the fabric's dashboard">
              <ArrowCorner />
            </a>
          ) : undefined
        }
      >
        <dl className="ac-kvs">
          <Row label="Name">{status.node_name !== "" ? status.node_name : <span className="text-fg-muted">none (shown as {status.hostname})</span>}</Row>
          <Row label="Hostname">{status.hostname}</Row>
          <Row label="Fabric">{status.fabric_id ?? "-"}</Row>
          <Row label="Node id">
            <span className="font-mono text-[0.8125rem]">{status.node_id ?? "-"}</span>
          </Row>
          <Row label="Controller">{status.controller ?? "-"}</Row>
          {status.dashboard_url !== null && status.dashboard_url !== undefined && (
            <Row label="Dashboard">
              <a className="ac-link" href={status.dashboard_url} target="_blank" rel="noreferrer">
                {status.dashboard_url}
              </a>
            </Row>
          )}
          <Row label="Certificate valid until">{status.cert_not_after ?? "-"}</Row>
          <Row label="MeshForge version">{status.version}</Row>
        </dl>
        {status.identity_problem !== null && status.identity_problem !== undefined && (
          <Problem className="mt-5" title="This laptop's identity has a problem" message={status.identity_problem} />
        )}
      </Panel>
      <Panel title="Checks">
        <ChecksList onRestart={() => { restart.mutate(); }} />
      </Panel>
      </div>
    </div>
  );
}
