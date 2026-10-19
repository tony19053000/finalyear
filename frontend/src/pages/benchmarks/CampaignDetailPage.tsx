/**
 * One benchmark campaign (0.9.9, D221): header with its controls, then tabs -
 * Overview, Runs, Labels, Charts, Quality, Download. The open tab is in the
 * URL (`?tab=`), so a link or a reload lands on the same view. A tab's data
 * loads when it is opened (inactive tabs are not mounted).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Tabs } from "../../components/Tabs";
import { useStreamLive } from "../../events/streamStatus";
import { PageHeader } from "../PageHeader";
import { AdminOnly } from "./AdminOnly";
import { CampaignActions } from "./CampaignActions";
import { ChartsTab } from "./ChartsTab";
import { DownloadTab } from "./DownloadTab";
import { LabelsTab } from "./LabelsTab";
import { OverviewTab } from "./OverviewTab";
import { QualityTab } from "./QualityTab";
import { RunsTab } from "./RunsTab";
import { getCampaign, getCampaignStatus, type CampaignDetail, type CampaignStatus } from "./benchApi";
import { benchKeys } from "./benchKeys";
import { campaignTone } from "./campaignView";
import { pollInterval } from "./poll";
import { useRefreshOnRunEnd } from "./useRefreshOnRunEnd";

const CAMPAIGN_TABS = ["overview", "runs", "labels", "charts", "quality", "download"] as const;
type CampaignTab = (typeof CAMPAIGN_TABS)[number];

function isTab(value: string | null): value is CampaignTab {
  return value !== null && (CAMPAIGN_TABS as readonly string[]).includes(value);
}

function need(campaign: CampaignDetail | undefined, status: CampaignStatus | undefined) {
  const state = status?.state ?? campaign?.state;
  return {
    open: state === "active" || state === "paused",
    unowned: state === "active" && status?.runner_owned !== true,
  };
}

const back = (
  <Link to="/benchmarks" className="mb-3 inline-block text-sm text-accent underline-offset-2 hover:underline">
    ← All campaigns
  </Link>
);

function Campaign({ campaignId }: { campaignId: string }) {
  const live = useStreamLive();
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab");
  const tab: CampaignTab = isTab(tabParam) ? tabParam : "overview";
  const client = useQueryClient();
  const detail = useQuery({
    queryKey: benchKeys.detail(campaignId),
    queryFn: ({ signal }) => getCampaign(campaignId, signal),
    // The run counts follow the same poll as the status.
    refetchInterval: (q) =>
      pollInterval(live, need(q.state.data, client.getQueryData<CampaignStatus>(benchKeys.status(campaignId)))),
  });
  const status = useQuery({
    queryKey: benchKeys.status(campaignId),
    queryFn: ({ signal }) => getCampaignStatus(campaignId, signal),
    enabled: detail.isSuccess,
    refetchInterval: (q) => pollInterval(live, need(detail.data, q.state.data)),
  });
  useRefreshOnRunEnd(campaignId, status.data?.counts ?? detail.data?.counts);
  useDocumentTitle(detail.data ? `${detail.data.name} · Benchmarks` : "Benchmark campaign");

  if (detail.isPending) {
    return (
      <>
        {back}
        <div role="status" className="py-8 text-center text-sm text-fg-muted">
          Loading campaign…
        </div>
      </>
    );
  }
  if (detail.isError) {
    if (detail.error instanceof ApiError && (detail.error.status === 404 || detail.error.status === 400)) {
      return (
        <>
          {back}
          <EmptyState title="No such campaign">
            <span className="font-mono">{campaignId}</span> does not exist on this controller.
          </EmptyState>
        </>
      );
    }
    return (
      <>
        {back}
        <ErrorState title="Could not load the campaign" error={detail.error} onRetry={() => void detail.refetch()} />
      </>
    );
  }
  const campaign = detail.data;
  return (
    <>
      {back}
      <PageHeader
        title={campaign.name}
        description={`${campaign.campaign_id} · ${String(campaign.run_count)} runs · ${String(campaign.summary.groups)} groups · ${String(campaign.summary.configs)} configurations`}
        actions={
          <>
            <Badge tone={campaignTone(campaign.state)}>{campaign.state}</Badge>
            <CampaignActions
              campaignId={campaign.campaign_id}
              name={campaign.name}
              state={campaign.state}
              runnerOwned={status.data?.runner_owned}
              size="md"
            />
          </>
        }
      />
      {status.isError && (
        <p role="alert" className="mb-3 text-sm text-status-failed">
          Could not load the campaign&apos;s progress numbers; the counts below are from the campaign itself.
        </p>
      )}
      <Tabs
        label="Campaign sections"
        value={tab}
        onValueChange={(value) => {
          setParams(value === "overview" ? {} : { tab: value }, { replace: true });
        }}
        items={[
          { value: "overview", label: "Overview", content: <OverviewTab campaign={campaign} status={status.data} /> },
          { value: "runs", label: "Runs", content: <RunsTab campaignId={campaign.campaign_id} runCount={campaign.run_count} /> },
          { value: "labels", label: "Labels", content: <LabelsTab campaignId={campaign.campaign_id} /> },
          { value: "charts", label: "Charts", content: <ChartsTab campaignId={campaign.campaign_id} /> },
          { value: "quality", label: "Quality", content: <QualityTab campaignId={campaign.campaign_id} /> },
          { value: "download", label: "Download", content: <DownloadTab campaignId={campaign.campaign_id} /> },
        ]}
      />
    </>
  );
}

export function CampaignDetailPage() {
  const { campaignId = "" } = useParams();
  return (
    <AdminOnly>
      <Campaign campaignId={campaignId} />
    </AdminOnly>
  );
}
