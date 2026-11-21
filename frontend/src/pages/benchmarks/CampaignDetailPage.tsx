/**
 * One benchmark campaign (0.9.9, D221): header with its controls, the open
 * facts strip, then text tabs - Overview, Runs, Labels, Charts, Quality,
 * Download. The open tab is in the URL (`?tab=`), so a link or a reload
 * lands on the same view. A tab's data loads when it is opened (inactive
 * tabs are not mounted).
 *
 * Layout: the job page's language - the name with its state light, a mono
 * id subline and the quiet round actions; the facts between hairlines; frame-B
 * text tabs (a real ARIA tablist).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { EmptyState, ErrorState } from "../../components/States";
import { formatTime } from "../../components/jobs/format";
import { useStreamLive } from "../../events/streamStatus";
import { formatAgo } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";
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
import { Fact, Light, TextTabs, type TabItem } from "./benchUi";
import { dash, useNodeNames } from "./benchShared";
import { campaignLight } from "./campaignView";
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
  <div className="mt-3">
    <Link to="/benchmarks" className="text-sm text-fg-muted transition-colors duration-300 hover:text-accent">
      ← All campaigns
    </Link>
  </div>
);

/** The open facts strip: who / when / what the campaign is, between hairlines. */
function Facts({ campaign, status }: { campaign: CampaignDetail; status: CampaignStatus | undefined }) {
  const nameOf = useNodeNames();
  const now = useServerNow();
  const created = Date.parse(campaign.created_at);
  const started: ReactNode =
    status === undefined ? (
      dash
    ) : status.started_at != null ? (
      <time dateTime={status.started_at} title={formatTime(status.started_at)}>
        {formatAgo((now - Date.parse(status.started_at)) / 1000)}
      </time>
    ) : (
      "not started (manual submit)"
    );
  return (
    <section aria-label="Summary">
      <dl className="nd-hair-y nd-stagger grid grid-cols-5 gap-x-10 gap-y-8 py-8" data-testid="campaign-facts">
        <Fact label="State">
          <span className="inline-flex items-center gap-2.5">
            <Light className={campaignLight(campaign.state)} />
            {campaign.state}
            {status?.finished_reason != null && <span className="font-mono text-[0.8125rem] text-fg-muted">· {status.finished_reason}</span>}
          </span>
        </Fact>
        <Fact label="Started">{started}</Fact>
        <Fact label="Created">
          <time dateTime={campaign.created_at} title={formatTime(campaign.created_at)}>
            {Number.isNaN(created) ? campaign.created_at : formatAgo((now - created) / 1000)}
          </time>
          {campaign.created_by != null && <span className="block font-mono text-2xs text-fg-muted">{campaign.created_by}</span>}
        </Fact>
        <Fact label="Session">{campaign.summary.session_label ?? dash}</Fact>
        <Fact label="Groups / configs">
          <span className="tabular-nums">{`${String(campaign.summary.groups)} / ${String(campaign.summary.configs)}`}</span>
        </Fact>
        <Fact label="Nodes" className="col-span-3">
          <span className="block truncate" title={campaign.summary.nodes.join(", ")}>
            {campaign.summary.nodes.length === 0 ? dash : campaign.summary.nodes.map(nameOf).join(", ")}
          </span>
        </Fact>
        <Fact label="Seed">
          <span className="font-mono text-[0.8125rem]">{campaign.seed}</span>
        </Fact>
        <Fact label="Spec digest">
          <span className="block truncate font-mono text-[0.8125rem]" title={campaign.spec_digest}>
            {campaign.spec_digest}
          </span>
        </Fact>
      </dl>
    </section>
  );
}

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
          <EmptyState title="No such campaign" className="mt-6">
            <span className="font-mono">{campaignId}</span> does not exist on this controller.
          </EmptyState>
        </>
      );
    }
    return (
      <>
        {back}
        <ErrorState className="mt-6" title="Could not load the campaign" error={detail.error} onRetry={() => void detail.refetch()} />
      </>
    );
  }
  const campaign = detail.data;
  const items: TabItem[] = [
    { value: "overview", label: "Overview" },
    { value: "runs", label: "Runs", count: campaign.run_count },
    { value: "labels", label: "Labels" },
    { value: "charts", label: "Charts" },
    { value: "quality", label: "Quality" },
    { value: "download", label: "Download" },
  ];
  let panel: ReactNode;
  if (tab === "runs") panel = <RunsTab campaignId={campaign.campaign_id} runCount={campaign.run_count} />;
  else if (tab === "labels") panel = <LabelsTab campaignId={campaign.campaign_id} />;
  else if (tab === "charts") panel = <ChartsTab campaignId={campaign.campaign_id} />;
  else if (tab === "quality") panel = <QualityTab campaignId={campaign.campaign_id} />;
  else if (tab === "download") panel = <DownloadTab campaignId={campaign.campaign_id} />;
  else panel = <OverviewTab campaign={campaign} status={status.data} />;

  const working = (status.data?.state ?? campaign.state) === "active" && (status.data?.counts ?? campaign.counts).submitted > 0;
  return (
    <>
      {back}
      <header className="mt-5 mb-9 flex flex-wrap items-end justify-between gap-6">
        <div className="min-w-0">
          <div className="flex items-center gap-4">
            <h1 className="min-w-0 truncate font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">{campaign.name}</h1>
            <span aria-hidden="true" title={campaign.state} className={`nd-light mt-2 ${campaignLight(campaign.state)} ${working ? "nd-breathe" : ""}`} />
          </div>
          <p className="mt-2.5 flex flex-wrap items-center gap-3 text-sm text-fg-muted">
            <span className="font-mono text-[0.8125rem]">{campaign.campaign_id}</span>
            <span aria-hidden="true" className="text-fg-subtle">
              ·
            </span>
            <span className="jb-tag text-fg-muted">
              {`${String(campaign.run_count)} runs · ${String(campaign.summary.groups)} groups · ${String(campaign.summary.configs)} configurations`}
            </span>
          </p>
        </div>
        <CampaignActions
          campaignId={campaign.campaign_id}
          name={campaign.name}
          state={campaign.state}
          runnerOwned={status.data?.runner_owned}
          size="md"
        />
      </header>
      {status.isError && (
        <p role="alert" className="mb-3 text-sm text-status-failed">
          Could not load the campaign&apos;s progress numbers; the counts below are from the campaign itself.
        </p>
      )}

      <Facts campaign={campaign} status={status.data} />

      <div className="mt-10 mb-8">
        <TextTabs
          label="Campaign sections"
          idPrefix="bench"
          items={items}
          value={tab}
          onChange={(value) => {
            setParams(value === "overview" ? {} : { tab: value }, { replace: true });
          }}
        />
      </div>
      <div role="tabpanel" id={`bench-panel-${tab}`} aria-labelledby={`bench-tab-${tab}`} key={tab} className="nd-in pb-6">
        {panel}
      </div>
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
