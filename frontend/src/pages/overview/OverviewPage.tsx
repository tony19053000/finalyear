/**
 * Overview (frame B layout). Top row: split progress, the honey speed-up
 * card, the CPU/RAM/GPU donut; right column: the 10-minute load graph over
 * the alerts; below: the nodes as a 3D honeycomb.
 *
 * Every figure comes from the controller (checklist 26): `GET /v1/nodes`
 * (live via node.status / node.metrics events) and each node's hardware,
 * `GET /v1/jobs` (recent; invalidated by job events), a job's
 * `GET /v1/jobs/{id}`, `/decisions` and `/split` (live via split.progress),
 * `GET /v1/alerts` (admins; live `alert` events). The load graph's history is
 * recorded in this browser from those same live numbers (fabricHistory.ts).
 */
import { useMemo, type ReactNode, useState } from "react";

import type { NodeSummary } from "../../api/endpoints";
import { useNodes, useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { CodeBlock } from "../../components/CodeBlock";
import { EmptyState, ErrorState } from "../../components/States";
import { PageHeader } from "../PageHeader";
import { AlertsCard } from "./AlertsCard";
import { useHistorySampler } from "./fabricHistory";
import { HoneycombCard } from "./HoneycombCard";
import { LoadHistoryCard } from "./LoadHistoryCard";
import {
  fabricPercents,
  focusJob,
  isLive,
  members,
  nodeLoad,
  placingDecision,
  resourceTotals,
  speedUp,
  type NodeLoad,
} from "./overviewModel";
import { ResourceDonutCard } from "./ResourceDonutCard";
import { SpeedUpCard } from "./SpeedUpCard";
import { JobProgressCard } from "./JobProgressCard";
import { useDecisions, useNodeCores, useRecentJobs, useSplitFocus } from "./useOverviewData";

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

function displayNames(nodes: readonly NodeSummary[]): Map<string, string> {
  return new Map(nodes.map((n) => [n.node_id, n.display_name || n.hostname]));
}

function Dashboard({ nodes, isAdmin }: { nodes: readonly NodeSummary[]; isAdmin: boolean }) {
  const cores = useNodeCores(nodes);
  const totals = resourceTotals(nodes, cores);
  const recent = useRecentJobs();
  const jobs = recent.data?.jobs ?? [];
  const names = useMemo(() => displayNames(nodes), [nodes]);

  const [alertsOpen, setAlertsOpen] = useState(false);
  const [loadOpen, setLoadOpen] = useState(false);
  const [resOpen, setResOpen] = useState(false);
  const [speedOpen, setSpeedOpen] = useState(false);
  const [jobOpen, setJobOpen] = useState(false);
  // the bell opens a top card like its arrow, showing what the card shows instead
  const [speedInfo, setSpeedInfo] = useState(false);
  const [jobInfo, setJobInfo] = useState(false);
  const splitFocus = useSplitFocus(recent.data?.jobs);
  const focus = focusJob(jobs);
  const decisions = useDecisions(focus?.job_id);
  const focusIsSplit = focus !== undefined && splitFocus.job?.job_id === focus.job_id;
  const predictedS = focusIsSplit ? splitFocus.split?.data?.predicted_s : undefined;
  const focusDecision = decisions.data === undefined ? undefined : placingDecision(decisions.data.decisions);
  const focusPlan = focusDecision === undefined ? null : speedUp(focusDecision, predictedS);

  useHistorySampler(() => {
    if (totals.reporting === 0) return null;
    const per: Record<string, NodeLoad> = {};
    for (const n of members(nodes)) if (isLive(n)) per[n.node_id] = nodeLoad(n);
    return { fabric: fabricPercents(totals), nodes: per };
  });

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,2.6fr)_minmax(0,1.75fr)]">
      <div className="flex min-w-0 flex-col gap-4">
        {/* Top row: Resources can widen to the left over Job progress and Speed-up. */}
        <div className={`ov-toprow ${resOpen ? "ov-toprow-res" : speedOpen ? "ov-toprow-speed" : jobOpen ? "ov-toprow-job" : ""}`}>
          <div className="ov-toprow-side ov-side-job" aria-hidden={resOpen || speedOpen}>
            <JobProgressCard
              job={focus}
              plan={focusPlan}
              progress={focusIsSplit ? splitFocus.split?.data : undefined}
              loading={recent.isPending}
              failed={focusIsSplit && (splitFocus.split?.isError ?? false)}
              names={names}
              expanded={jobOpen}
              explain={jobInfo}
              onToggle={() => {
                setResOpen(false);
                setSpeedOpen(false);
                setJobInfo(false);
                setJobOpen((o) => !o);
              }}
              onExplain={() => {
                setResOpen(false);
                setSpeedOpen(false);
                const show = !(jobOpen && jobInfo);
                setJobInfo(show);
                setJobOpen(show);
              }}
            />
          </div>
          <div className="ov-toprow-side ov-side-speed" aria-hidden={resOpen || jobOpen}>
            <SpeedUpCard
              expanded={speedOpen}
              explain={speedInfo}
              onToggle={() => {
                setResOpen(false);
                setJobOpen(false);
                setSpeedInfo(false);
                setSpeedOpen((o) => !o);
              }}
              onExplain={() => {
                setResOpen(false);
                setJobOpen(false);
                const show = !(speedOpen && speedInfo);
                setSpeedInfo(show);
                setSpeedOpen(show);
              }}
              job={focus}
              decisions={decisions.isError ? [] : decisions.data?.decisions}
              loading={recent.isPending}
              names={names}
              predictedS={predictedS ?? null}
            />
          </div>
          <div className="ov-toprow-side ov-side-res" aria-hidden={speedOpen}>
            <ResourceDonutCard
              totals={totals}
              nodes={nodes}
              cores={cores}
              expanded={resOpen}
              onToggle={() => {
                setSpeedOpen(false);
                setJobOpen(false);
                setResOpen((o) => !o);
              }}
            />
          </div>
        </div>
        <HoneycombCard nodes={nodes} cores={cores} />
      </div>
      {/* xl: the right column takes the left column's height (contain: size), so Alerts ends level with Nodes */}
      <div className="flex min-w-0 flex-col gap-4 xl:[contain:size]">
        {/* One of Load / Alerts can unfold to the full column; the other folds away. */}
        <div className={`ov-fold ${alertsOpen ? "ov-fold-closed" : ""} ${loadOpen ? "min-h-0 flex-1" : ""}`} aria-hidden={alertsOpen}>
          <div className="min-h-0">
            <LoadHistoryCard
              nodes={nodes}
              expanded={loadOpen}
              onToggle={() => {
                setAlertsOpen(false);
                setLoadOpen((o) => !o);
              }}
            />
          </div>
        </div>
        <div className={`ov-fold ${loadOpen ? "ov-fold-closed" : "min-h-0 flex-1"}`} aria-hidden={loadOpen}>
          <div className="flex min-h-0 flex-col">
        <AlertsCard
          isAdmin={isAdmin}
          nodes={nodes}
          jobs={jobs}
          names={names}
          expanded={alertsOpen}
          onToggle={() => {
            setLoadOpen(false);
            setAlertsOpen((o) => !o);
          }}
        />
          </div>
        </div>
        {recent.isError && (
          <ErrorState
            title="Could not load jobs"
            error={recent.error}
            onRetry={() => {
              void recent.refetch();
            }}
          />
        )}
      </div>
    </div>
  );
}

export function OverviewPage() {
  useDocumentTitle("Overview");
  const nodes = useNodes();
  const session = useSession();
  const isAdmin = session.data?.user.role === "admin";

  let content: ReactNode;
  if (nodes.isPending) {
    content = (
      <p role="status" className="text-sm text-fg-muted">
        Loading the fabric…
      </p>
    );
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
        {list.length === 0 && <NoNodesYet />}
        <Dashboard nodes={list} isAdmin={isAdmin} />
      </div>
    );
  }
  return (
    <>
      <PageHeader title="Overview" />
      {content}
    </>
  );
}
