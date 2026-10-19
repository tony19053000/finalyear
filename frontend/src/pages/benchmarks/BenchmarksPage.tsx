/**
 * Benchmarks (0.9.9, D221): the campaigns list. Admin only.
 *
 * Every number is the server's: run counts (`GET /v1/bench/campaigns`), and
 * per row done %, ETA, the infrastructure failure rate (owner pre-emptions
 * excluded) and when the runner took it (`GET .../{id}/status`). Live via
 * `campaign.progress` (applyCampaignEvent); polled when the stream is down
 * or a campaign is driven by hand (poll.ts).
 */
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { useDocumentTitle } from "../../app/useDocumentTitle";
import { useStreamLive } from "../../events/streamStatus";
import { Card } from "../../components/Card";
import { Meter } from "../../components/Meter";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatTime } from "../../components/jobs/format";
import { PageHeader } from "../PageHeader";
import { AdminOnly } from "./AdminOnly";
import { CampaignActions } from "./CampaignActions";
import { getCampaignStatus, listCampaigns, type CampaignOut, type CampaignStatus } from "./benchApi";
import { benchKeys } from "./benchKeys";
import { campaignTone, formatDonePercent, formatEta, formatFraction } from "./campaignView";
import { pollInterval, type PollNeed } from "./poll";

const OPEN = new Set(["active", "paused"]);

interface Row {
  campaign: CampaignOut;
  status: CampaignStatus | undefined;
  statusError: boolean;
}

function columns(): Column<Row>[] {
  return [
    {
      id: "name",
      header: "Campaign",
      cell: ({ campaign }) => (
        <div className="flex min-w-0 flex-col">
          <Link
            to={`/benchmarks/${encodeURIComponent(campaign.campaign_id)}`}
            className="font-medium text-accent underline-offset-2 hover:underline"
          >
            {campaign.name}
          </Link>
          <span className="font-mono text-2xs text-fg-muted">{campaign.campaign_id}</span>
        </div>
      ),
    },
    {
      id: "state",
      header: "State",
      cell: ({ campaign, status }) => (
        <span className="inline-flex items-center gap-1.5">
          <Badge tone={campaignTone(campaign.state)}>{campaign.state}</Badge>
          {status?.finished_reason != null && <span className="text-2xs text-fg-muted">{status.finished_reason}</span>}
        </span>
      ),
    },
    {
      id: "done",
      header: "Done",
      cell: ({ status }) => (
        <div className="flex w-32 flex-col gap-1">
          <span className="text-xs tabular-nums" data-testid="done-percent">
            {formatDonePercent(status?.done_percent)}
          </span>
          <Meter percent={status?.done_percent ?? null} />
        </div>
      ),
    },
    {
      id: "runs",
      header: "Runs",
      align: "right",
      mono: true,
      cell: ({ campaign }) => (
        <span title={`${String(campaign.counts.done)} done, ${String(campaign.counts.failed)} failed, ${String(campaign.counts.skipped)} skipped, ${String(campaign.counts.planned + campaign.counts.submitted)} open`}>
          {campaign.counts.done + campaign.counts.failed + campaign.counts.skipped} / {campaign.run_count}
        </span>
      ),
    },
    {
      id: "eta",
      header: "ETA",
      mono: true,
      cell: ({ status, statusError }) => (statusError ? "—" : formatEta(status?.eta_s)),
    },
    {
      id: "infra",
      header: (
        <span>
          Infra failures
          <span className="block normal-case tracking-normal">(owner pre-emptions excluded)</span>
        </span>
      ),
      align: "right",
      mono: true,
      cell: ({ status }) => formatFraction(status?.infra_failure_rate),
    },
    {
      id: "started",
      header: "Started",
      mono: true,
      cell: ({ status }) =>
        status === undefined ? "—" : status.started_at != null ? formatTime(status.started_at) : "not started",
    },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      cell: ({ campaign, status }) => (
        <CampaignActions
          campaignId={campaign.campaign_id}
          name={campaign.name}
          state={campaign.state}
          runnerOwned={status?.runner_owned}
        />
      ),
    },
  ];
}

/** What a campaign row needs polled for (poll.ts): open, and not known to be runner-owned. */
function rowNeed(campaign: CampaignOut, status: CampaignStatus | undefined): PollNeed {
  return { open: OPEN.has(campaign.state), unowned: campaign.state === "active" && status?.runner_owned !== true };
}

function CampaignsList() {
  const live = useStreamLive();
  const client = useQueryClient();
  const cachedStatus = (id: string) => client.getQueryData<CampaignStatus>(benchKeys.status(id));
  const list = useQuery({
    queryKey: benchKeys.list,
    queryFn: ({ signal }) => listCampaigns(signal),
    refetchInterval: (query) => {
      const needs = (query.state.data?.campaigns ?? []).map((c) => rowNeed(c, cachedStatus(c.campaign_id)));
      return pollInterval(live, { open: needs.some((n) => n.open), unowned: needs.some((n) => n.unowned) });
    },
  });
  const campaigns = list.data?.campaigns ?? [];
  const statuses = useQueries({
    queries: campaigns.map((c) => ({
      queryKey: benchKeys.status(c.campaign_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getCampaignStatus(c.campaign_id, signal),
      staleTime: 5_000,
      refetchInterval: () => pollInterval(live, rowNeed(c, cachedStatus(c.campaign_id))),
    })),
  });
  const rows: Row[] = campaigns.map((campaign, i) => ({
    campaign,
    status: statuses[i]?.data,
    statusError: statuses[i]?.isError === true,
  }));

  if (list.isPending) {
    return (
      <div role="status" className="py-8 text-center text-sm text-fg-muted">
        Loading campaigns…
      </div>
    );
  }
  if (list.isError) {
    return <ErrorState title="Could not load campaigns" error={list.error} onRetry={() => void list.refetch()} />;
  }
  if (campaigns.length === 0) {
    return (
      <EmptyState title="No benchmark campaigns yet">
        Create one from a spec file with <code className="font-mono">compute bench create spec.yaml</code> (see
        docs/BENCHMARKS.md); it appears here, where you can start, pause, resume or cancel it.
      </EmptyState>
    );
  }
  return (
    <Card title={`Campaigns (${String(campaigns.length)})`} flush>
      <Table
        caption="Benchmark campaigns, newest first"
        columns={columns()}
        rows={rows}
        rowKey={(r) => r.campaign.campaign_id}
      />
    </Card>
  );
}

export function BenchmarksPage() {
  useDocumentTitle("Benchmarks");
  return (
    <>
      <PageHeader
        title="Benchmarks"
        description="Benchmark campaigns: progress, controls, labels, charts, data quality and the dataset download."
      />
      <AdminOnly>
        <CampaignsList />
      </AdminOnly>
    </>
  );
}
