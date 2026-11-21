/**
 * One job (0.8.5): header, facts, then text tabs - Progress, Chunks (a split
 * job), Plan, Attempts, Logs, Timeline, Outputs, Manifest - and cancel.
 *
 * Layout: the node page's language - the name with its state light, a mono
 * id + mode subline and a quiet outline action; an open facts strip between
 * hairlines; frame-B text tabs (a real ARIA tablist: arrow keys / Home / End;
 * the open tab is kept in `?tab=` so a shared link opens it).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { useNodes } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { EmptyState, ErrorState } from "../../components/States";
import { AttemptsTable } from "../../components/jobs/AttemptsTable";
import { CancelJobDialog } from "../../components/jobs/CancelJobDialog";
import { JobArtifacts } from "../../components/jobs/JobArtifacts";
import { JobLogPanel } from "../../components/jobs/JobLogPanel";
import { JobTimeline } from "../../components/jobs/JobTimeline";
import { SplitProgressPanel } from "../../components/jobs/SplitProgressPanel";
import { formatDuration, formatTime } from "../../components/jobs/format";
import type { SocketLike } from "../../events/eventStream";
import { cn } from "../../lib/cn";
import { formatAgo, formatDateTime } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";
import { jobKeys } from "./jobKeys";
import { JobPlanPanel } from "./JobPlanPanel";
import { JobProgressPanel } from "./JobProgressPanel";
import { TERMINAL_JOB_STATES, getDecisions, getJob, logDownloadUrl, type JobDetail } from "./jobsApi";
import { MODE_TEXT, durationMs, isWorking, nodeNames, planView, stateLight } from "./jobView";
import { isSplitJob } from "./splitApi";
import { LOG_MAX_LINES, useJobLogs } from "./useJobLogs";

function Fact({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="nd-label">{label}</dt>
      <dd className="mt-2.5 text-[0.9375rem] leading-6 break-words text-fg">{children}</dd>
    </div>
  );
}

function placementText(job: JobDetail): string {
  const { mode, node_id } = job.placement;
  if (mode === "auto") return "auto (the planner chooses)";
  return `${mode === "local" ? "local" : "offload"} → ${node_id ?? "?"}`;
}

const none = <span className="text-fg-subtle">—</span>;

/** The open facts strip (the node page's): mono labels over values, between hairlines, no box. */
function Facts({ job, names, now }: { job: JobDetail; names: ReadonlyMap<string, string>; now: number }) {
  const ms = durationMs(job, now);
  const created = Date.parse(job.created_at);
  return (
    <section aria-label="Summary">
      <dl className="nd-hair-y nd-stagger grid grid-cols-5 gap-x-10 gap-y-8 py-8" data-testid="job-facts">
        <Fact label="State">
          <span className="inline-flex items-center gap-2.5">
            <span aria-hidden="true" className={`nd-light ${stateLight(job.state)}`} />
            {job.state}
          </span>
        </Fact>
        <Fact label="Submitted">
          <time dateTime={job.created_at} title={formatDateTime(job.created_at)}>
            {Number.isNaN(created) ? job.created_at : formatAgo((now - created) / 1000)}
          </time>
        </Fact>
        <Fact label={TERMINAL_JOB_STATES.has(job.state) ? "Took" : "Running for"}>
          <span className="tabular-nums">{ms === null ? "—" : formatDuration(ms)}</span>
        </Fact>
        <Fact label="Placement">{placementText(job)}</Fact>
        <Fact label="Policy">
          <span className="font-mono text-[0.8125rem]">{job.placement.policy ?? "manifest / controller default"}</span>
        </Fact>
        <Fact label="Priority">
          <span className="tabular-nums">{job.priority}</span>
        </Fact>
        <Fact label="Updated">
          <span className="font-mono text-[0.8125rem]">{formatTime(job.updated_at)}</span>
        </Fact>
        <Fact label="Submitted from">
          {job.submitter_node_id == null ? (
            none
          ) : (
            <span title={job.submitter_node_id}>{names.get(job.submitter_node_id) ?? job.submitter_node_id}</span>
          )}
        </Fact>
        <Fact label={job.tree_id != null ? "Project tree" : "Bundle"} className="col-span-2">
          <span className="block truncate font-mono text-[0.8125rem]" title={job.tree_id ?? job.bundle_digest ?? undefined}>
            {job.tree_id ?? job.bundle_digest ?? "—"}
          </span>
        </Fact>
        {job.output_digest != null && (
          <Fact label="Output" className="col-span-2">
            <span className="block truncate font-mono text-[0.8125rem]" title={job.output_digest}>
              {job.output_digest}
            </span>
          </Fact>
        )}
        {job.failure_type != null && (
          <Fact label="Failure" className="col-span-3">
            <span className="font-mono text-[0.8125rem] text-status-failed">{job.failure_type}</span>
            {job.failure_detail != null && <span className="block text-sm text-fg-muted">{job.failure_detail}</span>}
          </Fact>
        )}
        {job.warnings.length > 0 && (
          <Fact label="Warnings" className="col-span-5">
            <ul aria-label="Job warnings" className="flex flex-col gap-2">
              {job.warnings.map((w) => (
                <li key={w.code} className="text-sm">
                  <span className="font-mono text-status-degraded">{w.code}</span> ×{w.count}
                  {(w.chunks ?? []).length > 0 && (
                    <span className="text-fg-muted">
                      {" "}
                      (chunks {(w.chunks ?? []).slice(0, 16).join(", ")}
                      {(w.chunks ?? []).length > 16 ? ", …" : ""})
                    </span>
                  )}
                  <span className="block text-fg-muted">{w.detail}</span>
                </li>
              ))}
            </ul>
          </Fact>
        )}
        {job.inputs.length > 0 && (
          <Fact label="Named inputs" className="col-span-5">
            <ul>
              {job.inputs.map((i) => (
                <li key={i.mount} className="font-mono text-[0.8125rem]">
                  {i.ref} → {i.mount}
                </li>
              ))}
            </ul>
          </Fact>
        )}
      </dl>
    </section>
  );
}

interface TabItem {
  value: string;
  label: string;
  count?: number;
}

/** Frame-B text tabs: plain text, the open one white and bold over a short underline. */
function DetailTabs({ items, value, onChange }: { items: readonly TabItem[]; value: string; onChange: (v: string) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = items.findIndex((t) => t.value === value);
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % items.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    const tab = items[next];
    if (tab === undefined) return;
    e.preventDefault();
    onChange(tab.value);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label="Job details" className="flex flex-wrap items-end gap-x-10 gap-y-3" onKeyDown={onKeyDown}>
      {items.map((t, k) => {
        const on = t.value === value;
        return (
          <button
            key={t.value}
            ref={(el) => {
              refs.current[k] = el;
            }}
            type="button"
            role="tab"
            id={`job-tab-${t.value}`}
            aria-selected={on}
            aria-controls={`job-panel-${t.value}`}
            aria-label={t.count === undefined ? undefined : `${t.label} ${String(t.count)}`}
            tabIndex={on ? 0 : -1}
            onClick={() => {
              onChange(t.value);
            }}
            className={`relative pb-2 text-[0.9375rem] transition-colors duration-300 ${on ? "font-semibold text-fg" : "text-fg-muted hover:text-fg"}`}
          >
            {t.label}
            {t.count !== undefined && <span className="ml-1.5 font-mono text-[0.6875rem] font-normal text-fg-subtle tabular-nums">{t.count}</span>}
            <span
              aria-hidden="true"
              className={`absolute bottom-0 left-0 h-0.5 rounded-full bg-fg transition-all duration-300 ${on ? "w-5 opacity-100" : "w-0 opacity-0"}`}
            />
          </button>
        );
      })}
    </div>
  );
}

function BackLink() {
  return (
    <div className="mt-3">
      <Link to="/jobs" className="text-sm text-fg-muted transition-colors duration-300 hover:text-accent">
        ← All jobs
      </Link>
    </div>
  );
}

export interface JobDetailPageProps {
  /** Tests inject a fake log socket. */
  logSocketFactory?: (url: string) => SocketLike;
}

/** One job (0.8.5): progress, plan explanation, attempts, logs, timeline, outputs, cancel. */
export function JobDetailPage({ logSocketFactory }: JobDetailPageProps) {
  const { jobId = "" } = useParams();
  const client = useQueryClient();
  const job = useQuery({ queryKey: jobKeys.detail(jobId), queryFn: ({ signal }) => getJob(jobId, signal) });
  const decisions = useQuery({
    queryKey: jobKeys.decisions(jobId),
    queryFn: ({ signal }) => getDecisions(jobId, signal),
    enabled: job.isSuccess,
  });
  const nodes = useNodes();
  const names = useMemo(() => nodeNames(nodes.data?.nodes ?? []), [nodes.data]);
  const now = useServerNow();
  const logs = useJobLogs(jobId, logSocketFactory);
  const [confirming, setConfirming] = useState(false);
  const [params, setParams] = useSearchParams();
  useDocumentTitle(job.data ? `${job.data.name} · Jobs` : "Job");

  // The log's end frame means the job ended: show its final state now.
  const ended = logs.snapshot.status === "ended";
  useEffect(() => {
    if (ended) void client.invalidateQueries({ queryKey: jobKeys.detail(jobId) });
  }, [ended, client, jobId]);

  if (job.isPending) {
    return (
      <>
        <BackLink />
        <div role="status" className="py-8 text-center text-sm text-fg-muted">
          Loading job…
        </div>
      </>
    );
  }
  if (job.data === undefined) {
    if (job.error instanceof ApiError && (job.error.status === 404 || job.error.status === 400)) {
      return (
        <>
          <BackLink />
          <EmptyState title="No such job" className="mt-6">
            <span className="font-mono">{jobId}</span> does not exist, or it belongs to another user.
          </EmptyState>
        </>
      );
    }
    return (
      <>
        <BackLink />
        <ErrorState
          className="mt-6"
          title="Could not load the job"
          error={job.error}
          onRetry={() => {
            void job.refetch();
          }}
        />
      </>
    );
  }

  const data = job.data;
  const terminal = TERMINAL_JOB_STATES.has(data.state);
  const split = isSplitJob(data.manifest);
  const plan = planView(data, decisions.data?.decisions);
  const mode = split ? "split" : plan.mode;

  const items: TabItem[] = [
    { value: "progress", label: "Progress" },
    // V0.10 G7: a split job's progress per chunk and node
    ...(split ? [{ value: "chunks", label: "Chunks" }] : []),
    { value: "plan", label: "Plan" },
    { value: "attempts", label: "Attempts", count: data.attempts.length },
    { value: "logs", label: "Logs" },
    { value: "timeline", label: "Timeline" },
    { value: "outputs", label: "Outputs" },
    { value: "manifest", label: "Manifest" },
  ];
  const tabParam = params.get("tab");
  const tab = items.some((t) => t.value === tabParam) ? (tabParam ?? "progress") : "progress";
  const setTab = (v: string) => {
    const next = new URLSearchParams(params);
    if (v === "progress") next.delete("tab");
    else next.set("tab", v);
    setParams(next, { replace: true });
  };

  let panel: ReactNode;
  if (tab === "chunks") panel = <SplitProgressPanel jobId={data.job_id} terminal={terminal} names={names} />;
  else if (tab === "plan")
    panel = decisions.data ? (
      <JobPlanPanel decisions={decisions.data.decisions} pinned={data.placement.mode !== "auto"} placement={data.placement} names={names} />
    ) : decisions.isError ? (
      <ErrorState
        title="Could not load the plan explanation"
        error={decisions.error}
        onRetry={() => {
          void decisions.refetch();
        }}
      />
    ) : (
      <p role="status" className="text-sm text-fg-muted">
        Loading decisions…
      </p>
    );
  else if (tab === "attempts") panel = <AttemptsTable attempts={data.attempts} names={names} />;
  else if (tab === "logs")
    panel = (
      <JobLogPanel
        snapshot={logs.snapshot}
        maxLines={LOG_MAX_LINES}
        onPause={logs.pause}
        onResume={logs.resume}
        downloadHref={logDownloadUrl(data.job_id)}
        downloadName={`${data.job_id}.log`}
      />
    );
  else if (tab === "timeline")
    panel = <JobTimeline transitions={data.transitions} terminal={terminal} now={new Date(now).toISOString()} />;
  else if (tab === "outputs") panel = <JobArtifacts jobId={data.job_id} attempts={data.attempts} terminal={terminal} names={names} />;
  else if (tab === "manifest")
    panel = (
      <section aria-label="Manifest" className="ov-card ov-frost nd-glass px-9 py-8">
        <pre aria-label="Job manifest (canonical)" tabIndex={0} className="jb-log max-h-[32rem] text-fg">
          <code>{JSON.stringify(data.manifest, null, 2)}</code>
        </pre>
      </section>
    );
  else panel = <JobProgressPanel job={data} decisions={decisions.data?.decisions} names={names} />;

  return (
    <>
      <BackLink />
      <header className="mt-5 mb-9 flex flex-wrap items-end justify-between gap-6">
        <div className="min-w-0">
          <div className="flex items-center gap-4">
            <h1 className="min-w-0 truncate font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">{data.name}</h1>
            <span
              aria-hidden="true"
              title={data.state}
              className={cn("nd-light mt-2", stateLight(data.state), isWorking(data.state) && "nd-breathe")}
              data-testid="title-light"
            />
          </div>
          <p className="mt-2.5 flex items-center gap-3 text-sm text-fg-muted">
            <span className="font-mono text-[0.8125rem]">{data.job_id}</span>
            {mode !== null && (
              <>
                <span aria-hidden="true" className="text-fg-subtle">
                  ·
                </span>
                <span className="jb-tag text-[#ffc93c]" data-testid="job-mode">
                  {MODE_TEXT[mode]}
                </span>
              </>
            )}
          </p>
        </div>
        {/* V1.0 G9 (lab L1 V0.8): a finished job has nothing to cancel, so the button is not shown at all. */}
        {!terminal && (
          <button
            type="button"
            className="nd-ghost-btn"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Cancel job
          </button>
        )}
      </header>
      {job.isRefetchError && (
        <p role="status" className="mb-3 text-xs text-status-degraded">
          Could not refresh this job; showing what was loaded at {formatTime(new Date(job.dataUpdatedAt).toISOString())}.
        </p>
      )}

      <Facts job={data} names={names} now={now} />

      <div className="mt-10 mb-8">
        <DetailTabs items={items} value={tab} onChange={setTab} />
      </div>
      <div role="tabpanel" id={`job-panel-${tab}`} aria-labelledby={`job-tab-${tab}`} key={tab} className="nd-in pb-6">
        {panel}
      </div>
      <CancelJobDialog
        job={data}
        open={confirming}
        onClose={() => {
          setConfirming(false);
        }}
      />
    </>
  );
}
