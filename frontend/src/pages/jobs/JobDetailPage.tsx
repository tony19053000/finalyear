import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { EmptyState, ErrorState } from "../../components/States";
import { StatusDot } from "../../components/Status";
import { Tabs } from "../../components/Tabs";
import { AttemptsTable } from "../../components/jobs/AttemptsTable";
import { CancelJobDialog } from "../../components/jobs/CancelJobDialog";
import { JobArtifacts } from "../../components/jobs/JobArtifacts";
import { JobLogPanel } from "../../components/jobs/JobLogPanel";
import { JobTimeline } from "../../components/jobs/JobTimeline";
import { PlanTable } from "../../components/jobs/PlanTable";
import { formatTime } from "../../components/jobs/format";
import type { SocketLike } from "../../events/eventStream";
import { jobTone } from "../../lib/status";
import { PageHeader } from "../PageHeader";
import { jobKeys } from "./jobKeys";
import { TERMINAL_JOB_STATES, getDecisions, getJob, logDownloadUrl, type JobDetail } from "./jobsApi";
import { LOG_MAX_LINES, useJobLogs } from "./useJobLogs";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">{label}</dt>
      <dd className="min-w-0 text-sm break-words">{children}</dd>
    </div>
  );
}

function placement(job: JobDetail): string {
  const { mode, node_id } = job.placement;
  if (mode === "auto") return "auto (the planner chooses)";
  return `${mode === "local" ? "local" : "offload"} → ${node_id ?? "?"}`;
}

function Summary({ job }: { job: JobDetail }) {
  return (
    <Card title="Summary">
      <dl className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-x-6 gap-y-3">
        <Field label="State">
          <StatusDot tone={jobTone(job.state)} label={job.state} />
        </Field>
        <Field label="Placement">{placement(job)}</Field>
        <Field label="Policy">{job.placement.policy ?? "manifest / controller default"}</Field>
        <Field label="Priority">{job.priority}</Field>
        <Field label="Created">
          <span className="font-mono text-xs">{formatTime(job.created_at)}</span>
        </Field>
        <Field label="Updated">
          <span className="font-mono text-xs">{formatTime(job.updated_at)}</span>
        </Field>
        <Field label="Submitted from node">
          <span className="font-mono text-xs">{job.submitter_node_id ?? "—"}</span>
        </Field>
        <Field label={job.tree_id != null ? "Project tree" : "Bundle"}>
          <span className="font-mono text-2xs break-all">{job.tree_id ?? job.bundle_digest ?? "—"}</span>
        </Field>
        {job.failure_type != null && (
          <Field label="Failure">
            <span className="font-mono text-xs text-status-failed">{job.failure_type}</span>
            {job.failure_detail != null && <span className="block text-fg-muted">{job.failure_detail}</span>}
          </Field>
        )}
        {job.inputs.length > 0 && (
          <Field label="Named inputs">
            <ul>
              {job.inputs.map((i) => (
                <li key={i.mount} className="font-mono text-xs">
                  {i.ref} → {i.mount}
                </li>
              ))}
            </ul>
          </Field>
        )}
      </dl>
    </Card>
  );
}

export interface JobDetailPageProps {
  /** Tests inject a fake log socket. */
  logSocketFactory?: (url: string) => SocketLike;
}

/** One job (0.8.5): summary, logs, plan explanation, attempts, timeline, outputs, cancel. */
export function JobDetailPage({ logSocketFactory }: JobDetailPageProps) {
  const { jobId = "" } = useParams();
  const client = useQueryClient();
  const job = useQuery({ queryKey: jobKeys.detail(jobId), queryFn: ({ signal }) => getJob(jobId, signal) });
  const decisions = useQuery({
    queryKey: jobKeys.decisions(jobId),
    queryFn: ({ signal }) => getDecisions(jobId, signal),
    enabled: job.isSuccess,
  });
  const logs = useJobLogs(jobId, logSocketFactory);
  const [confirming, setConfirming] = useState(false);
  useDocumentTitle(job.data ? `${job.data.name} · Jobs` : "Job");

  // The log's end frame means the job ended: show its final state now.
  const ended = logs.snapshot.status === "ended";
  useEffect(() => {
    if (ended) void client.invalidateQueries({ queryKey: jobKeys.detail(jobId) });
  }, [ended, client, jobId]);

  const back = (
    <Link to="/jobs" className="mb-3 inline-block text-sm text-accent underline-offset-2 hover:underline">
      ← All jobs
    </Link>
  );

  if (job.isPending) {
    return (
      <>
        {back}
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
          {back}
          <EmptyState title="No such job">
            <span className="font-mono">{jobId}</span> does not exist, or it belongs to another user.
          </EmptyState>
        </>
      );
    }
    return (
      <>
        {back}
        <ErrorState
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
  return (
    <>
      {back}
      <PageHeader
        title={data.name}
        description={data.job_id}
        actions={
          <Button
            variant="danger"
            disabled={terminal}
            title={terminal ? `The job is ${data.state}; there is nothing to cancel.` : undefined}
            onClick={() => {
              setConfirming(true);
            }}
          >
            Cancel job
          </Button>
        }
      />
      {job.isRefetchError && (
        <p role="status" className="mb-3 text-xs text-status-degraded">
          Could not refresh this job; showing what was loaded at {formatTime(new Date(job.dataUpdatedAt).toISOString())}.
        </p>
      )}
      <div className="flex flex-col gap-5">
        <Summary job={data} />
        <Tabs
          label="Job details"
          defaultValue="logs"
          items={[
            {
              value: "logs",
              label: "Logs",
              content: (
                <JobLogPanel
                  snapshot={logs.snapshot}
                  maxLines={LOG_MAX_LINES}
                  onPause={logs.pause}
                  onResume={logs.resume}
                  downloadHref={logDownloadUrl(data.job_id)}
                  downloadName={`${data.job_id}.log`}
                />
              ),
            },
            {
              value: "plan",
              label: "Plan",
              content: decisions.data ? (
                <PlanTable decisions={decisions.data.decisions} pinned={data.placement.mode !== "auto"} />
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
              ),
            },
            {
              value: "attempts",
              label: `Attempts (${String(data.attempts.length)})`,
              content: <AttemptsTable attempts={data.attempts} />,
            },
            {
              value: "timeline",
              label: "Timeline",
              content: <JobTimeline transitions={data.transitions} terminal={terminal} now={new Date().toISOString()} />,
            },
            {
              value: "outputs",
              label: "Outputs",
              content: <JobArtifacts jobId={data.job_id} attempts={data.attempts} terminal={terminal} />,
            },
            {
              value: "manifest",
              label: "Manifest",
              content: <CodeBlock label="Job manifest (canonical)" code={JSON.stringify(data.manifest, null, 2)} />,
            },
          ]}
        />
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
