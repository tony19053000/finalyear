/**
 * Benchmarks (0.9.9, D221): the campaigns list. Admin only.
 *
 * Every number is the server's: run counts (`GET /v1/bench/campaigns`), and
 * per row done %, ETA, the infrastructure failure rate (owner pre-emptions
 * excluded) and when the runner took it (`GET .../{id}/status`). Live via
 * `campaign.progress` (applyCampaignEvent); polled when the stream is down
 * or a campaign is driven by hand (poll.ts). The speed-up column and the
 * headline figures are ratios of the labels' server medians
 * (`GET .../{id}/labels`, the same cache entry the campaign page reads).
 *
 * Layout: the Jobs list's language - the big title with text tabs by state
 * centred on its row (each with its count), one compact open row of headline
 * figures, then the frosted table filling the window (even columns, sticky
 * header clear at the top, filled only once rows scroll under it).
 */
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useDocumentTitle } from "../../app/useDocumentTitle";
import { useStreamLive } from "../../events/streamStatus";
import { EmptyState, ErrorState } from "../../components/States";
import { formatDayTime, formatTime } from "../../components/jobs/format";
import { cn } from "../../lib/cn";
import { formatMs } from "../../lib/format";
import { AdminOnly } from "./AdminOnly";
import { CampaignActions } from "./CampaignActions";
import { getCampaignStatus, getLabels, listCampaigns, type CampaignOut, type CampaignStatus, type LabelsOut } from "./benchApi";
import { benchKeys } from "./benchKeys";
import { Figure, Light } from "./benchUi";
import { TH, dash, useScrolled } from "./benchShared";
import {
  LIST_TABS,
  bestSpeedUp,
  campaignLight,
  formatDonePercent,
  formatEta,
  formatFactor,
  formatFraction,
  isListTab,
  listTabOf,
  plannerAgreement,
  type ListTab,
  type SpeedUpResult,
} from "./campaignView";
import { pollInterval, type PollNeed } from "./poll";

const OPEN = new Set(["active", "paused"]);
/** Labels are read for at most this many campaigns (newest first) for the speed-up column and figures. */
const LABELS_FOR = 12;
const HEADERS = ["Campaign", "State", "Progress", "Runs", "Speed-up", "Failures", "Started", "Actions"] as const;
/** The Failures column's explanation (its header's tooltip and screen-reader text). */
const FAILURES_HINT = "Infrastructure failure rate (owner pre-emptions excluded)";

/** A finished reason worth a sub-line: not one the state word already says (done / complete, cancelled / cancelled). */
function reasonLine(state: string, reason: CampaignStatus["finished_reason"] | undefined): string | null {
  if (reason == null) return null;
  if ((state === "done" && reason === "complete") || (state === "cancelled" && reason === "cancelled")) return null;
  return reason;
}

interface Row {
  campaign: CampaignOut;
  status: CampaignStatus | undefined;
  statusError: boolean;
  speed: SpeedUpResult | null;
}

/** What a campaign row needs polled for (poll.ts): open, and not known to be runner-owned. */
function rowNeed(campaign: CampaignOut, status: CampaignStatus | undefined): PollNeed {
  return { open: OPEN.has(campaign.state), unowned: campaign.state === "active" && status?.runner_owned !== true };
}

/** A slim progress track with the honey fill (an SVG width attribute: no inline style). */
function RowBar({ percent }: { percent: number }) {
  const p = Math.max(0, Math.min(100, percent));
  return (
    <svg aria-hidden="true" className="mt-1.5 block h-[3px] w-full overflow-hidden rounded-full" preserveAspectRatio="none" viewBox="0 0 100 3">
      <rect x="0" y="0" width="100" height="3" rx="1.5" className="jb-track" />
      {p > 0 && <rect x="0" y="0" width={p} height="3" rx="1.5" className="jb-fill" />}
    </svg>
  );
}

function CampaignRow({ row }: { row: Row }) {
  const navigate = useNavigate();
  const { campaign, status, statusError, speed } = row;
  const href = `/benchmarks/${encodeURIComponent(campaign.campaign_id)}`;
  const ended = campaign.counts.done + campaign.counts.failed + campaign.counts.skipped;
  const open = campaign.state === "active" || campaign.state === "paused";
  // active, but no run has left `planned` yet (the Queued tab's rule): no progress to show
  const queued = listTabOf(campaign) === "queued";
  const reason = reasonLine(campaign.state, status?.finished_reason);
  const eta = statusError ? "—" : formatEta(status?.eta_s);
  return (
    <tr
      className="cursor-pointer"
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("a, button, dialog") !== null) return;
        void navigate(href);
      }}
    >
      <td className="align-middle">
        <span className="flex min-w-0 flex-col">
          <Link
            to={href}
            className="truncate font-medium text-fg underline-offset-2 transition-colors duration-300 hover:text-accent hover:underline"
            title={campaign.name}
          >
            {campaign.name}
          </Link>
          <span className="truncate font-mono text-2xs text-fg-muted" title={campaign.campaign_id}>
            {campaign.campaign_id}
          </span>
        </span>
      </td>
      <td className="align-middle">
        <span className="flex flex-col">
          <span className="inline-flex items-center gap-2.5">
            <Light className={queued ? "bg-fg-subtle" : campaignLight(campaign.state)} breathe={campaign.state === "active" && !queued} />
            <span>{queued ? "queued" : campaign.state}</span>
          </span>
          {reason !== null && <span className="pl-5 font-mono text-2xs text-fg-muted">{reason}</span>}
        </span>
      </td>
      <td className="align-middle">
        {queued ? (
          <span className="text-fg-subtle" data-testid="done-percent">
            —
          </span>
        ) : (
          <span className="flex max-w-[10.5rem] flex-col">
            <span className="flex items-baseline justify-between gap-3">
              <span className="shrink-0 font-light tabular-nums" data-testid="done-percent">
                {formatDonePercent(status?.done_percent)}
              </span>
              {open && eta !== "—" && (
                <span className="truncate font-mono text-2xs text-fg-muted">
                  <span>{eta}</span> left
                </span>
              )}
            </span>
            {open && status?.done_percent != null && <RowBar percent={status.done_percent} />}
          </span>
        )}
      </td>
      <td className="align-middle font-mono text-[0.8125rem] tabular-nums">
        <span
          title={`${String(campaign.counts.done)} done, ${String(campaign.counts.failed)} failed, ${String(campaign.counts.skipped)} skipped, ${String(campaign.counts.planned + campaign.counts.submitted)} open`}
        >
          {ended} / {campaign.run_count}
        </span>
      </td>
      <td className="align-middle font-mono text-[0.8125rem] tabular-nums">
        {speed === null ? (
          dash
        ) : (
          <span
            className="whitespace-nowrap"
            title={`${speed.family}: ${formatMs(speed.baseline.total_ms?.median)} kept local → ${formatMs(speed.best.total_ms?.median)} at best (server medians)`}
          >
            {formatFactor(speed.factor)}
            <span className="ml-1 text-fg-muted">×</span>
          </span>
        )}
      </td>
      <td className="align-middle font-mono text-[0.8125rem] tabular-nums" title={FAILURES_HINT}>
        {formatFraction(status?.infra_failure_rate)}
      </td>
      <td className="align-middle whitespace-nowrap text-fg-muted">
        {status === undefined ? (
          dash
        ) : status.started_at != null ? (
          <time dateTime={status.started_at} title={formatTime(status.started_at)}>
            {formatDayTime(status.started_at)}
          </time>
        ) : (
          "not started"
        )}
      </td>
      <td className="align-middle text-right">
        <CampaignActions campaignId={campaign.campaign_id} name={campaign.name} state={campaign.state} runnerOwned={status?.runner_owned} />
      </td>
    </tr>
  );
}

function CampaignTable({ rows, empty }: { rows: readonly Row[]; empty: ReactNode }) {
  const scroller = useRef<HTMLDivElement>(null);
  const scrolled = useScrolled(scroller);
  return (
    <section aria-label="Campaigns" className="ov-card ov-frost ov-table-fit ov-table-big bm-list overflow-hidden pb-4">
      <div ref={scroller} className="jb-scroll bm-list-scroll min-h-40 overflow-auto" data-scrolled={scrolled ? "true" : "false"}>
        <table className="w-full border-collapse">
          <caption className="sr-only">Benchmark campaigns, newest first</caption>
          <thead>
            <tr>
              {HEADERS.map((h) => (
                <th key={h} scope="col" className={TH} title={h === "Failures" ? FAILURES_HINT : undefined}>
                  {h === "Actions" ? (
                    <span className="sr-only">Actions</span>
                  ) : h === "Failures" ? (
                    <>
                      {h}
                      <span className="sr-only"> ({FAILURES_HINT.toLowerCase()})</span>
                    </>
                  ) : (
                    h
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <CampaignRow key={r.campaign.campaign_id} row={r} />
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={HEADERS.length} className="text-center text-fg-muted">
                  {empty}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/**
 * The open row of headline figures: best speed-up (the one honey number),
 * runs, failed runs, planner. Spread evenly across the table's width: the
 * first starts at its first column's content edge, the last ends at its
 * actions' edge (the table's 3.25rem ends), equal gaps between.
 */
function Headline({ campaigns, labels }: { campaigns: readonly CampaignOut[]; labels: readonly { campaign: CampaignOut; labels: LabelsOut }[] }) {
  const done = campaigns.reduce((n, c) => n + c.counts.done, 0);
  const failed = campaigns.reduce((n, c) => n + c.counts.failed, 0);
  let best: { speed: SpeedUpResult; name: string } | null = null;
  let agrees = 0;
  let judged = 0;
  for (const l of labels) {
    const s = bestSpeedUp(l.labels);
    if (s !== null && (best === null || s.factor > best.speed.factor)) best = { speed: s, name: l.campaign.name };
    const p = plannerAgreement(l.labels);
    agrees += p.agrees;
    judged += p.judged;
  }
  return (
    <dl className="nd-stagger mb-6 flex justify-between gap-x-12 px-[3.25rem]" data-testid="bench-figures">
      <Figure
        label="Best speed-up"
        compact
        className="max-w-[24%]"
        honey
        value={best === null ? "—" : formatFactor(best.speed.factor)}
        unit="×"
        sub={
          best === null
            ? "no finished group with a local baseline yet"
            : `${best.name} · ${formatMs(best.speed.baseline.total_ms?.median)} → ${formatMs(best.speed.best.total_ms?.median)}`
        }
      />
      <Figure label="Runs done" compact className="max-w-[24%]" value={done.toLocaleString()} sub={`across ${String(campaigns.length)} campaign${campaigns.length === 1 ? "" : "s"}`} />
      <Figure label="Failed runs" compact className="max-w-[24%]" value={failed.toLocaleString()} sub={`of ${(done + failed).toLocaleString()} measured or failed`} />
      <Figure
        label="Planner picked best"
        compact
        className="max-w-[24%]"
        value={judged === 0 ? "—" : String(Math.round((agrees * 100) / judged))}
        unit="%"
        sub={judged === 0 ? "no group with a clear best yet" : `${String(agrees)} of ${String(judged)} groups with a clear best`}
      />
    </dl>
  );
}

function CampaignsList() {
  const live = useStreamLive();
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const stateParam = params.get("state");
  const tab: ListTab = isListTab(stateParam) ? stateParam : "all";
  const cachedStatus = (id: string) => client.getQueryData<CampaignStatus>(benchKeys.status(id));
  const list = useQuery({
    queryKey: benchKeys.list,
    queryFn: ({ signal }) => listCampaigns(signal),
    refetchInterval: (query) => {
      const needs = (query.state.data?.campaigns ?? []).map((c) => rowNeed(c, cachedStatus(c.campaign_id)));
      return pollInterval(live, { open: needs.some((n) => n.open), unowned: needs.some((n) => n.unowned) });
    },
  });
  const campaigns = useMemo(() => list.data?.campaigns ?? [], [list.data]);
  const statuses = useQueries({
    queries: campaigns.map((c) => ({
      queryKey: benchKeys.status(c.campaign_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getCampaignStatus(c.campaign_id, signal),
      staleTime: 5_000,
      refetchInterval: () => pollInterval(live, rowNeed(c, cachedStatus(c.campaign_id))),
    })),
  });
  // the labels of the newest campaigns that measured something: a finished campaign's never change
  const labelled = campaigns.filter((c) => c.counts.done > 0).slice(0, LABELS_FOR);
  const labelQueries = useQueries({
    queries: labelled.map((c) => ({
      queryKey: benchKeys.labels(c.campaign_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getLabels(c.campaign_id, signal),
      staleTime: OPEN.has(c.state) ? 60_000 : Infinity,
      retry: false,
    })),
  });
  const labels = labelled.flatMap((campaign, i) => {
    const data = labelQueries[i]?.data;
    return data === undefined ? [] : [{ campaign, labels: data }];
  });
  const speedOf = new Map(labels.map((l) => [l.campaign.campaign_id, bestSpeedUp(l.labels)]));
  const rows: Row[] = campaigns.map((campaign, i) => ({
    campaign,
    status: statuses[i]?.data,
    statusError: statuses[i]?.isError === true,
    speed: speedOf.get(campaign.campaign_id) ?? null,
  }));
  const counts = new Map<ListTab, number>(LIST_TABS.map((t) => [t.key, 0]));
  for (const c of campaigns) {
    const key = listTabOf(c);
    counts.set(key, (counts.get(key) ?? 0) + 1);
    counts.set("all", (counts.get("all") ?? 0) + 1);
  }
  const shown = tab === "all" ? rows : rows.filter((r) => listTabOf(r.campaign) === tab);

  let content: ReactNode;
  if (list.isPending) {
    content = (
      <div role="status" className="py-8 text-center text-sm text-fg-muted">
        Loading campaigns…
      </div>
    );
  } else if (list.isError) {
    content = <ErrorState title="Could not load campaigns" error={list.error} onRetry={() => void list.refetch()} />;
  } else if (campaigns.length === 0) {
    content = (
      <EmptyState title="No benchmark campaigns yet">
        Create one from a spec file with <code className="font-mono">compute bench create spec.yaml</code> (see
        docs/BENCHMARKS.md); it appears here, where you can start, pause, resume or cancel it.
      </EmptyState>
    );
  } else {
    content = (
      <>
        <Headline campaigns={campaigns} labels={labels} />
        <CampaignTable rows={shown} empty={`No ${LIST_TABS.find((t) => t.key === tab)?.label.toLowerCase() ?? ""} campaigns.`} />
      </>
    );
  }

  return (
    <>
      <div className="mt-3 mb-8 grid grid-cols-[1fr_auto_1fr] items-end gap-x-8">
        <h1 className="font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">Benchmarks</h1>
        {campaigns.length > 0 ? (
          <div role="group" aria-label="Filter by state" className="flex items-end justify-center gap-10 pb-1.5">
            {LIST_TABS.map((t) => {
              const on = t.key === tab;
              const count = String(counts.get(t.key) ?? 0);
              return (
                <button
                  key={t.key}
                  type="button"
                  aria-pressed={on}
                  aria-label={`${t.label} ${count}`}
                  onClick={() => {
                    setParams(t.key === "all" ? {} : { state: t.key }, { replace: true });
                  }}
                  className={cn("relative pb-2 text-[0.9375rem] transition-colors duration-300", on ? "font-semibold text-fg" : "text-fg-muted hover:text-fg")}
                >
                  {t.label}
                  <span className="ml-1.5 font-mono text-[0.6875rem] text-fg-subtle tabular-nums">{count}</span>
                  <span
                    aria-hidden="true"
                    className={cn("absolute bottom-0 left-0 h-0.5 rounded-full bg-fg transition-all duration-300", on ? "w-5 opacity-100" : "w-0 opacity-0")}
                  />
                </button>
              );
            })}
          </div>
        ) : (
          <span />
        )}
        <span />
      </div>
      {content}
    </>
  );
}

export function BenchmarksPage() {
  useDocumentTitle("Benchmarks");
  return (
    <AdminOnly title="Benchmarks">
      <CampaignsList />
    </AdminOnly>
  );
}
