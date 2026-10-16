/**
 * Overview (0.8.3): fabric health, nodes by status, active jobs by state,
 * live utilisation and alerts. Every number comes from an endpoint (checklist
 * 26): `GET /v1/nodes` (live via node.status / node.metrics events),
 * `GET /healthz`, `GET /v1/jobs?state=...` (invalidated by job.state events),
 * `GET /v1/alerts` (+ live `alert` events). Nothing is estimated; an empty
 * fabric says so and says how to add a node.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { NodeSummary } from "../../api/endpoints";
import { ACTIVE_JOB_STATES, ACTIVE_JOBS_LIMIT, useActiveJobs, useHealth, type JobState } from "../../api/fabric";
import { useNodes, useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { Meter } from "../../components/Meter";
import { EmptyState, ErrorState } from "../../components/States";
import { StatusDot } from "../../components/Status";
import { formatBytes, formatPercent, percentOf } from "../../lib/format";
import { jobTone, nodeTone } from "../../lib/status";
import { countByState, fabricHealth, hostnames, utilization } from "../nodes/nodeView";
import { PageHeader } from "../PageHeader";
import { AlertsPanel } from "./AlertsPanel";

function Loading({ what }: { what: string }) {
  return (
    <p role="status" className="text-sm text-fg-muted">
      Loading {what}…
    </p>
  );
}

/** A labelled figure: the label names it for screen readers too. */
function Figure({ label, value, sub, testId }: { label: string; value: ReactNode; sub?: ReactNode; testId?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">{label}</dt>
      <dd className="text-lg font-semibold text-fg tabular-nums" data-testid={testId}>
        {value}
      </dd>
      {sub !== undefined && <dd className="text-xs text-fg-muted">{sub}</dd>}
    </div>
  );
}

function HealthCard({ nodes }: { nodes: readonly NodeSummary[] }) {
  const health = useHealth();
  const fabric = fabricHealth(nodes);
  return (
    <Card title="Fabric health" data-testid="health-card">
      <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Figure
          label="Fabric"
          testId="health-fabric"
          value={<StatusDot tone={fabric.tone} label={fabric.headline} />}
          sub={fabric.total === 0 ? undefined : `${String(fabric.online)} online of ${String(fabric.total)} enrolled (revoked nodes not counted)`}
        />
        <Figure
          label="Controller"
          testId="health-controller"
          value={
            health.isPending ? (
              "…"
            ) : health.isError ? (
              <StatusDot tone="failed" label="Not answering" />
            ) : (
              <StatusDot
                tone={health.data.status === "ok" ? "ok" : "degraded"}
                label={health.data.status === "ok" ? "OK" : `Degraded (${(health.data.degraded ?? []).join(", ")})`}
              />
            )
          }
          sub={health.isSuccess ? `${String(health.data.sessions)} agent session${health.data.sessions === 1 ? "" : "s"} connected` : undefined}
        />
      </dl>
    </Card>
  );
}

function NodesCard({ nodes }: { nodes: readonly NodeSummary[] }) {
  const counts = countByState(nodes);
  return (
    <Card
      title="Nodes"
      data-testid="nodes-card"
      actions={
        <Link to="/nodes" className="text-xs text-accent underline-offset-2 hover:underline">
          All nodes
        </Link>
      }
    >
      <dl className="flex flex-col gap-2">
        <div className="flex items-baseline justify-between">
          <dt className="text-sm text-fg-muted">Total</dt>
          <dd className="text-lg font-semibold tabular-nums" data-testid="nodes-total">
            {nodes.length}
          </dd>
        </div>
        {[...counts.entries()].map(([state, count]) => (
          <div key={state} className="flex items-baseline justify-between">
            <dt className="text-sm">
              <StatusDot tone={nodeTone(state)} label={state} />
            </dt>
            <dd className="text-sm font-medium tabular-nums" data-testid={`nodes-${state}`}>
              {count}
            </dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function ActiveJobsCard() {
  const jobs = useActiveJobs();
  let body: ReactNode;
  if (jobs.isPending) {
    body = <Loading what="jobs" />;
  } else if (jobs.isError) {
    body = (
      <ErrorState
        title="Could not load jobs"
        error={jobs.error}
        onRetry={() => {
          void jobs.refetch();
        }}
      />
    );
  } else {
    const counts = new Map<JobState, number>();
    for (const job of jobs.data.jobs) counts.set(job.state, (counts.get(job.state) ?? 0) + 1);
    const more = jobs.data.next_cursor != null;
    const total = jobs.data.jobs.length;
    body = (
      <div className="flex flex-col gap-2">
        <dl className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between">
            <dt className="text-sm text-fg-muted">Active</dt>
            <dd className="text-lg font-semibold tabular-nums" data-testid="jobs-active">
              {more ? `${String(total)}+` : total}
            </dd>
          </div>
          {ACTIVE_JOB_STATES.filter((state) => counts.has(state)).map((state) => (
            <div key={state} className="flex items-baseline justify-between">
              <dt className="text-sm">
                <StatusDot tone={jobTone(state)} label={state} />
              </dt>
              <dd className="text-sm font-medium tabular-nums" data-testid={`jobs-${state}`}>
                {counts.get(state)}
                {more ? "+" : ""}
              </dd>
            </div>
          ))}
        </dl>
        {total === 0 && <p className="text-sm text-fg-muted">No job is waiting or running.</p>}
        {more && (
          <p className="text-xs text-fg-muted">Counted over the newest {ACTIVE_JOBS_LIMIT} active jobs; there are more.</p>
        )}
      </div>
    );
  }
  return (
    <Card
      title="Active jobs"
      data-testid="jobs-card"
      actions={
        <Link to="/jobs" className="text-xs text-accent underline-offset-2 hover:underline">
          All jobs
        </Link>
      }
    >
      {body}
    </Card>
  );
}

function UsageRow({ label, percent, text, testId }: { label: string; percent: number | null; text: string; testId: string }) {
  return (
    // dt + dd directly in the row's div: the only nesting a <dl> allows (axe).
    <div className="grid grid-cols-[1fr_auto] items-baseline gap-x-3 gap-y-1">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className="text-sm font-medium tabular-nums" data-testid={testId}>
        {text}
      </dd>
      <dd className="col-span-2">
        <Meter percent={percent} />
      </dd>
    </div>
  );
}

function UtilizationCard({ nodes }: { nodes: readonly NodeSummary[] }) {
  const u = utilization(nodes);
  const ram = percentOf(u.ramUsed, u.ramTotal);
  const vram = percentOf(u.vramUsed, u.vramTotal);
  return (
    <Card title="Utilisation (live)" data-testid="utilization-card">
      {u.reporting === 0 ? (
        <p className="text-sm text-fg-muted">No node is reporting resources right now.</p>
      ) : (
        <dl className="flex flex-col gap-3">
          <UsageRow label="CPU (mean)" percent={u.cpuMean} text={formatPercent(u.cpuMean)} testId="util-cpu" />
          <UsageRow
            label="Memory"
            percent={ram}
            text={`${formatBytes(u.ramUsed)} / ${formatBytes(u.ramTotal)} (${formatPercent(ram)})`}
            testId="util-ram"
          />
          {u.gpuCount === 0 ? (
            <div className="flex items-baseline justify-between">
              <dt className="text-sm text-fg-muted">GPU</dt>
              <dd className="text-sm text-fg-muted" data-testid="util-gpu">
                No GPU reported
              </dd>
            </div>
          ) : (
            <>
              <UsageRow
                label={`GPU (mean of ${String(u.gpuCount)})`}
                percent={u.gpuUtilMean}
                text={formatPercent(u.gpuUtilMean)}
                testId="util-gpu"
              />
              <UsageRow
                label="GPU memory"
                percent={vram}
                text={`${formatBytes(u.vramUsed)} / ${formatBytes(u.vramTotal)} (${formatPercent(vram)})`}
                testId="util-vram"
              />
            </>
          )}
        </dl>
      )}
      {u.reporting > 0 && (
        <p className="mt-3 text-xs text-fg-muted" data-testid="util-reporting">
          From {u.reporting} of {nodes.length} node{nodes.length === 1 ? "" : "s"} reporting now.
        </p>
      )}
    </Card>
  );
}

function NoNodesYet() {
  return (
    <EmptyState title="No nodes yet - create an invite">
      <p>Add a laptop to the fabric: create a join code on the controller host, then run the join on that laptop.</p>
      <CodeBlock
        className="mt-3 text-left"
        label="Add a node"
        code={"# on the controller host\nmeshforge-controller fabric invite\n\n# on the new laptop\nmeshforge-agent join <code>"}
      />
    </EmptyState>
  );
}

export function OverviewPage() {
  useDocumentTitle("Overview");
  const nodes = useNodes();
  const session = useSession();
  const isAdmin = session.data?.user.role === "admin";

  let content: ReactNode;
  if (nodes.isPending) {
    content = <Loading what="the fabric" />;
  } else if (nodes.isError) {
    content = (
      <ErrorState
        title="Could not load the nodes"
        error={nodes.error}
        onRetry={() => {
          void nodes.refetch();
        }}
      />
    );
  } else {
    const list = nodes.data.nodes;
    content = (
      <div className="flex flex-col gap-5">
        <HealthCard nodes={list} />
        {list.length === 0 && <NoNodesYet />}
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <NodesCard nodes={list} />
          <ActiveJobsCard />
          <UtilizationCard nodes={list} />
        </div>
        <AlertsPanel isAdmin={isAdmin} names={hostnames(list)} />
      </div>
    );
  }
  return (
    <>
      <PageHeader title="Overview" description="Fabric health, nodes, and the work running on them." />
      {content}
    </>
  );
}
