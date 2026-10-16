import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, describeError } from "../../../api/errors";
import { useNodes } from "../../../api/hooks";
import { queryKeys } from "../../../api/queryKeys";
import { useDocumentTitle } from "../../../app/useDocumentTitle";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { CodeBlock } from "../../../components/CodeBlock";
import { useToast } from "../../../components/toastContext";
import { formatBytes } from "../../../components/jobs/format";
import { PageHeader } from "../../PageHeader";
import { jobKeys } from "../jobKeys";
import { POLICIES, submitJob, type JobSubmissionBody, type PolicyName } from "../jobsApi";
import { folderSource } from "./folderSource";
import { createHasher, type FileHasher } from "./hasher";
import { prepareProject, type HashProgress, type Prepared } from "./prepare";
import { sanitizeName } from "./projectManifest";
import type { ProjectSource } from "./projectWalk";
import { SPEC } from "./spec";
import { uploadTree, type UploadProgress, type UploadReport } from "./upload";
import { readZip } from "./zip";

type Phase =
  | { kind: "idle" }
  | { kind: "preparing"; progress: HashProgress | null }
  | { kind: "ready"; prepared: Prepared }
  | { kind: "uploading"; prepared: Prepared; progress: UploadProgress | null }
  | { kind: "submitting"; prepared: Prepared; report: UploadReport }
  | { kind: "failed"; prepared: Prepared | null; error: unknown; stage: "prepare" | "upload" | "submit" };

const FIELD = "h-8 rounded-md border border-border-strong bg-surface-sunken px-2 text-sm text-fg";

function percent(done: number, total: number): number {
  return total <= 0 ? 100 : Math.min(100, Math.floor((done / total) * 100));
}

function Progress({ label, done, total, detail }: { label: string; done: number; total: number; detail: string }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-sm">
        <span>{label}</span>
        <span className="font-mono text-xs text-fg-muted tabular-nums">{detail}</span>
      </div>
      <progress className="mf-progress h-2 w-full" max={Math.max(total, 1)} value={Math.min(done, Math.max(total, 1))} aria-label={label}>
        {percent(done, total)}%
      </progress>
    </div>
  );
}

/** Server/YAML values as text (never "[object Object]"). */
function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  return JSON.stringify(value);
}

interface Rejection {
  node_id?: unknown;
  reject_code?: unknown;
  reject_text?: unknown;
}

function Rejections({ error }: { error: unknown }) {
  if (!(error instanceof ApiError)) return null;
  const list = error.detail.rejections;
  if (!Array.isArray(list)) return null;
  if (list.length === 0) return <p className="text-sm">No node has joined the fabric yet.</p>;
  return (
    <ul className="list-inside list-disc text-sm">
      {(list as Rejection[]).map((item, i) => (
        <li key={i}>
          <span className="font-mono">{asText(item.node_id ?? "?")}</span>: {asText(item.reject_code)} -{" "}
          {asText(item.reject_text)}
        </li>
      ))}
    </ul>
  );
}

function newKey(): string {
  return `web-${crypto.randomUUID()}`;
}

/** The priority field's problem, or null (empty = the manifest's/default). Same range as the API. */
function priorityProblem(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  if (!/^[+-]?\d+$/.test(trimmed) || !Number.isInteger(value) || value < -10 || value > 10) {
    return "Priority must be a whole number from -10 to 10.";
  }
  return null;
}

/**
 * One idempotency key per distinct request (T-14): an identical retry after a
 * failure reuses it (the controller answers with the job it may already have
 * made), but a request whose body changed gets a new key - the controller
 * would answer 409 to a reused key with a different body.
 */
interface KeyedRequest {
  key: string;
  body: string;
}

/**
 * Submit from the browser (D177, 0.8.5): pick a project folder or a .zip;
 * the same ignore/size rules as `compute run` (projectSpec.json); SHA-256 in
 * a Web Worker; the existing resumable CAS upload; the same POST /v1/jobs
 * the CLI sends. Cancel at any point; after a reload, pick the same folder
 * again and only what the controller still lacks is sent.
 */
export function SubmitJobPage() {
  useDocumentTitle("Submit job");
  const navigate = useNavigate();
  const client = useQueryClient();
  const toast = useToast();
  const nodes = useNodes();
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [name, setName] = useState("");
  const [priority, setPriority] = useState("");
  const [policy, setPolicy] = useState("");
  const [node, setNode] = useState("");
  const abort = useRef<AbortController | null>(null);
  const hasher = useRef<FileHasher | null>(null);
  const idempotencyKey = useRef<KeyedRequest | null>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const ids = {
    folder: useId(),
    zip: useId(),
    name: useId(),
    priority: useId(),
    priorityError: useId(),
    policy: useId(),
    node: useId(),
  };

  useEffect(() => {
    // Not in React's DOM types; the browser's folder picker attribute.
    folderInput.current?.setAttribute("webkitdirectory", "");
    return () => {
      abort.current?.abort();
      hasher.current?.close();
    };
  }, []);

  const sessionCheck = (error: unknown) => {
    if (error instanceof ApiError && error.status === 401) void client.invalidateQueries({ queryKey: queryKeys.session });
  };

  const prepare = async (load: () => Promise<ProjectSource>) => {
    abort.current?.abort();
    hasher.current?.close();
    const controller = new AbortController();
    abort.current = controller;
    idempotencyKey.current = null;
    setPhase({ kind: "preparing", progress: null });
    try {
      const source = await load();
      hasher.current = createHasher();
      const prepared = await prepareProject(source, hasher.current, {
        signal: controller.signal,
        onProgress: (progress) => {
          if (!controller.signal.aborted) setPhase({ kind: "preparing", progress });
        },
      });
      if (!controller.signal.aborted) setPhase({ kind: "ready", prepared });
    } catch (error) {
      if (controller.signal.aborted) return;
      setPhase({ kind: "failed", prepared: null, error, stage: "prepare" });
    } finally {
      hasher.current?.close();
      hasher.current = null;
    }
  };

  const buildBody = (prepared: Prepared): JobSubmissionBody => {
    const manifest = { ...prepared.manifest.manifest };
    if (name.trim() !== "") manifest.name = sanitizeName(name.trim());
    if (priority.trim() !== "") manifest.priority = Number(priority.trim());
    const placement: JobSubmissionBody["placement"] = {};
    if (node !== "") placement.node_id = node;
    if (policy !== "") placement.policy = policy as PolicyName;
    return { manifest, tree_id: prepared.tree.treeId, placement };
  };

  const send = async (prepared: Prepared) => {
    if (priorityProblem(priority) !== null) return; // refused before any upload (T-15)
    const controller = new AbortController();
    abort.current = controller;
    // Fixed now: what was picked when the user clicked is what is submitted.
    const body = buildBody(prepared);
    const fingerprint = JSON.stringify(body);
    if (idempotencyKey.current?.body !== fingerprint) idempotencyKey.current = { key: newKey(), body: fingerprint };
    const key = idempotencyKey.current.key;
    setPhase({ kind: "uploading", prepared, progress: null });
    let report: UploadReport;
    try {
      report = await uploadTree(prepared.tree, prepared.blobs, {
        signal: controller.signal,
        onProgress: (progress) => {
          if (!controller.signal.aborted) setPhase({ kind: "uploading", prepared, progress });
        },
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      sessionCheck(error);
      setPhase({ kind: "failed", prepared, error, stage: "upload" });
      return;
    }
    setPhase({ kind: "submitting", prepared, report });
    try {
      const created = await submitJob(body, key, controller.signal);
      void client.invalidateQueries({ queryKey: jobKeys.lists });
      toast({ title: "Job submitted", description: `${created.job_id} is ${created.state}.`, tone: "ok" });
      void navigate(`/jobs/${created.job_id}`);
    } catch (error) {
      if (controller.signal.aborted) return;
      sessionCheck(error);
      setPhase({ kind: "failed", prepared, error, stage: "submit" });
    }
  };

  const cancel = () => {
    abort.current?.abort();
    hasher.current?.close();
    hasher.current = null;
    setPhase((old) =>
      old.kind === "uploading" || old.kind === "submitting" ? { kind: "ready", prepared: old.prepared } : { kind: "idle" },
    );
  };

  const busy = phase.kind === "preparing" || phase.kind === "uploading" || phase.kind === "submitting";
  const badPriority = priorityProblem(priority);
  const prepared = phase.kind === "idle" || phase.kind === "preparing" ? null : phase.prepared;

  return (
    <>
      <Link to="/jobs" className="mb-3 inline-block text-sm text-accent underline-offset-2 hover:underline">
        ← All jobs
      </Link>
      <PageHeader
        title="Submit a job"
        description="A project folder (or a .zip of one) with a compute.yaml - or a main.py - runs on the fabric."
      />
      <div className="flex max-w-4xl flex-col gap-5">
        <Card title="1. Project">
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-6">
              <div className="flex flex-col gap-1">
                <label htmlFor={ids.folder} className="text-sm font-medium">
                  Project folder
                </label>
                <input
                  ref={folderInput}
                  id={ids.folder}
                  type="file"
                  multiple
                  disabled={busy}
                  className="text-sm file:mr-3 file:h-8 file:rounded-md file:border file:border-border-strong file:bg-surface-raised file:px-3 file:text-fg"
                  onChange={(event) => {
                    const files = Array.from(event.currentTarget.files ?? []);
                    event.currentTarget.value = "";
                    if (files.length > 0) void prepare(() => Promise.resolve(folderSource(files)));
                  }}
                />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor={ids.zip} className="text-sm font-medium">
                  …or a .zip of it
                </label>
                <input
                  id={ids.zip}
                  type="file"
                  accept=".zip,application/zip"
                  disabled={busy}
                  className="text-sm file:mr-3 file:h-8 file:rounded-md file:border file:border-border-strong file:bg-surface-raised file:px-3 file:text-fg"
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    event.currentTarget.value = "";
                    if (file !== undefined) void prepare(() => readZip(file));
                  }}
                />
              </div>
            </div>
            <p className="text-xs text-fg-muted">
              Same rules as <code className="font-mono">compute run</code>: <code className="font-mono">.gitignore</code> and{" "}
              <code className="font-mono">.computeignore</code> apply, plus {SPEC.builtin_ignores.join(", ")}. Files are
              hashed in this browser; only what the controller does not already hold is uploaded. A folder picker
              cannot see empty folders or exec bits and follows symlinks - use a zip or the CLI if those matter.
            </p>
            {phase.kind === "preparing" && (
              <div className="flex flex-col gap-2">
                {phase.progress === null ? (
                  <p role="status" className="text-sm text-fg-muted">
                    Reading the project…
                  </p>
                ) : (
                  <Progress
                    label="Hashing files"
                    done={phase.progress.hashedBytes}
                    total={phase.progress.totalBytes}
                    detail={`${phase.progress.hashedFiles}/${phase.progress.totalFiles} files · ${formatBytes(phase.progress.hashedBytes)} of ${formatBytes(phase.progress.totalBytes)}`}
                  />
                )}
                <div>
                  <Button onClick={cancel}>Cancel</Button>
                </div>
              </div>
            )}
            {prepared !== null && (
              <dl className="grid grid-cols-[10rem_1fr] gap-x-4 gap-y-1 text-sm" aria-label="Project summary">
                <dt className="text-fg-muted">Project</dt>
                <dd className="font-mono">{prepared.source.name}</dd>
                <dt className="text-fg-muted">Manifest</dt>
                <dd>{prepared.manifest.fromFile ? SPEC.manifest_file : "none - name and entrypoint inferred"}</dd>
                <dt className="text-fg-muted">Job name</dt>
                <dd className="font-mono">{asText(prepared.manifest.manifest.name)}</dd>
                <dt className="text-fg-muted">Entrypoint</dt>
                <dd className="font-mono">{JSON.stringify(prepared.manifest.manifest.entrypoint)}</dd>
                <dt className="text-fg-muted">Files</dt>
                <dd>
                  {prepared.tree.entries.filter((e) => e.kind === "file").length} files, {formatBytes(prepared.walk.totalBytes)}
                  {prepared.walk.ignored > 0 ? ` (${prepared.walk.ignored} ignored)` : ""}
                </dd>
                <dt className="text-fg-muted">Tree id</dt>
                <dd className="font-mono text-2xs break-all">{prepared.tree.treeId}</dd>
              </dl>
            )}
          </div>
        </Card>

        <Card title="2. Options">
          <div className="flex flex-wrap gap-4">
            <div className="flex flex-col gap-1">
              <label htmlFor={ids.name} className="text-2xs font-medium text-fg-muted uppercase">
                Name (overrides the manifest)
              </label>
              <input id={ids.name} className={FIELD} value={name} disabled={busy} onChange={(e) => { setName(e.target.value); }} />
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor={ids.priority} className="text-2xs font-medium text-fg-muted uppercase">
                Priority (-10 to 10)
              </label>
              <input
                id={ids.priority}
                type="text"
                inputMode="numeric"
                className={`${FIELD} w-24`}
                value={priority}
                disabled={busy}
                aria-invalid={badPriority !== null}
                aria-describedby={badPriority !== null ? ids.priorityError : undefined}
                onChange={(e) => {
                  setPriority(e.target.value);
                }}
              />
              {badPriority !== null && (
                <p id={ids.priorityError} className="text-xs text-status-failed">
                  {badPriority}
                </p>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor={ids.policy} className="text-2xs font-medium text-fg-muted uppercase">
                Placement policy
              </label>
              <select id={ids.policy} className={FIELD} value={policy} disabled={busy} onChange={(e) => { setPolicy(e.target.value); }}>
                <option value="">Default (manifest, else the controller's)</option>
                {POLICIES.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor={ids.node} className="text-2xs font-medium text-fg-muted uppercase">
                Node
              </label>
              <select id={ids.node} className={FIELD} value={node} disabled={busy} onChange={(e) => { setNode(e.target.value); }}>
                <option value="">Let the planner choose</option>
                {nodes.data?.nodes.map((n) => (
                  <option key={n.node_id} value={n.node_id}>
                    {n.hostname ? `${n.hostname} (${n.node_id}, ${n.state})` : `${n.node_id} (${n.state})`}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </Card>

        <Card title="3. Upload and run">
          <div className="flex flex-col gap-3">
            {phase.kind === "uploading" && (
              <>
                {phase.progress === null ? (
                  <p role="status" className="text-sm text-fg-muted">
                    Asking the controller what it already has…
                  </p>
                ) : (
                  <Progress
                    label="Uploading"
                    done={phase.progress.sentBytes}
                    total={phase.progress.totalBytes}
                    detail={`${phase.progress.sentBlobs}/${phase.progress.totalBlobs} files · ${formatBytes(phase.progress.sentBytes)} of ${formatBytes(phase.progress.totalBytes)}`}
                  />
                )}
              </>
            )}
            {phase.kind === "submitting" && (
              <p role="status" className="text-sm text-fg-muted">
                Uploaded {phase.report.uploadedBlobs} files ({formatBytes(phase.report.uploadedBytes)}), skipped{" "}
                {phase.report.skippedBlobs} already on the controller
                {phase.report.resumedParts > 0 ? `, resumed ${phase.report.resumedParts} parts` : ""}. Submitting…
              </p>
            )}
            {phase.kind === "failed" && (
              <div role="alert" className="flex flex-col gap-2 rounded-md border border-status-failed bg-status-failed-soft px-3 py-2">
                <p className="text-sm font-semibold text-status-failed">
                  {phase.stage === "prepare"
                    ? "Cannot submit this project"
                    : phase.stage === "upload"
                      ? "The upload stopped"
                      : "The controller refused the job"}
                </p>
                <p className="text-sm">{describeError(phase.error).message}</p>
                <Rejections error={phase.error} />
                {phase.stage === "prepare" && describeError(phase.error).message.includes("compute run") && (
                  <CodeBlock label="Submit with the CLI" code="compute run ./project" />
                )}
              </div>
            )}
            <div className="flex gap-2">
              <Button
                variant="primary"
                disabled={prepared === null || busy || badPriority !== null}
                onClick={() => {
                  if (prepared !== null) void send(prepared);
                }}
              >
                {phase.kind === "failed" && phase.stage !== "prepare" ? "Retry upload and submit" : "Upload and submit"}
              </Button>
              {(phase.kind === "uploading" || phase.kind === "submitting") && <Button onClick={cancel}>Cancel upload</Button>}
            </div>
            <p className="text-xs text-fg-muted">
              Interrupted? Pick the same folder again: parts the controller already has are not sent twice.
            </p>
          </div>
        </Card>
      </div>
    </>
  );
}
