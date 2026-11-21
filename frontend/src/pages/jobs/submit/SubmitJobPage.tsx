import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, describeError } from "../../../api/errors";
import { useNodes } from "../../../api/hooks";
import { queryKeys } from "../../../api/queryKeys";
import { ChevronIcon } from "../../../app/icons";
import { useDocumentTitle } from "../../../app/useDocumentTitle";
import { CodeBlock } from "../../../components/CodeBlock";
import { useToast } from "../../../components/toastContext";
import { formatBytes } from "../../../components/jobs/format";
import { ListboxField, type ListboxOption } from "../../../components/jobs/ListboxField";
import { cn } from "../../../lib/cn";
import { PageHeader } from "../../PageHeader";
import { jobKeys } from "../jobKeys";
import { POLICIES, submitJob, type JobSubmissionBody, type PolicyName } from "../jobsApi";
import { nodeNames } from "../jobView";
import { droppedFolderSource } from "./dropSource";
import { folderSource } from "./folderSource";
import { createHasher, type FileHasher } from "./hasher";
import { prepareProject, type HashProgress, type Prepared } from "./prepare";
import { sanitizeName } from "./projectManifest";
import { ProjectError, type ProjectSource } from "./projectWalk";
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

function percent(done: number, total: number): number {
  return total <= 0 ? 100 : Math.min(100, Math.floor((done / total) * 100));
}

/** A thin honey line with the label and a mono figure above it. */
function Progress({ label, done, total, detail }: { label: string; done: number; total: number; detail: string }) {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-4">
        <span className="nd-label">{label}</span>
        <span className="font-mono text-xs text-fg-muted tabular-nums">
          {detail}
          <span className="ml-3 text-fg">{percent(done, total)}%</span>
        </span>
      </div>
      <progress className="mf-progress w-full" max={Math.max(total, 1)} value={Math.min(done, Math.max(total, 1))} aria-label={label}>
        {percent(done, total)}%
      </progress>
    </div>
  );
}

/** A small mono uppercase label over its field (the Jobs filter fields). */
function Field({ id, label, children }: { id: string; label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="nd-label">
        {label}
      </label>
      {children}
    </div>
  );
}

/** One light big figure with its mono label under it. */
function Figure({ value, label }: { value: string; label: string }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="ov-figure text-[2.5rem] text-fg">{value}</span> <span className="nd-label">{label}</span>
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
  if (list.length === 0) return <p className="text-sm text-fg-muted">No node has joined the fabric yet.</p>;
  return (
    <ul className="flex flex-col gap-1 text-sm text-fg-muted">
      {(list as Rejection[]).map((item, i) => (
        <li key={i}>
          <span className="font-mono text-fg">{asText(item.node_id ?? "?")}</span>: {asText(item.reject_code)} -{" "}
          {asText(item.reject_text)}
        </li>
      ))}
    </ul>
  );
}

/** The failure of one stage: no box, a quiet red title and the reason. */
function Failure({ phase }: { phase: Extract<Phase, { kind: "failed" }> }) {
  const message = describeError(phase.error).message;
  return (
    <div role="alert" className="sj-in flex flex-col gap-2">
      <p className="text-sm font-medium text-status-failed">
        {phase.stage === "prepare"
          ? "Cannot submit this project"
          : phase.stage === "upload"
            ? "The upload stopped"
            : "The controller refused the job"}
      </p>
      <p className="text-sm text-fg-muted">{message}</p>
      <Rejections error={phase.error} />
      {phase.stage === "prepare" && message.includes("compute run") && (
        <CodeBlock label="Submit with the CLI" code="compute run ./project" />
      )}
    </div>
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
  const [dragging, setDragging] = useState(false);
  const [showRules, setShowRules] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const hasher = useRef<FileHasher | null>(null);
  const idempotencyKey = useRef<KeyedRequest | null>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const zipInput = useRef<HTMLInputElement>(null);
  const ids = {
    folder: useId(),
    zip: useId(),
    name: useId(),
    priority: useId(),
    priorityError: useId(),
    policy: useId(),
    node: useId(),
    rules: useId(),
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

  const names = useMemo(() => nodeNames(nodes.data?.nodes ?? []), [nodes.data]);
  const policyOptions = useMemo<ListboxOption[]>(
    () => [{ value: "", label: "Default", detail: "the manifest's, else the controller's" }, ...POLICIES.map((p) => ({ value: p, label: p }))],
    [],
  );
  const nodeOptions = useMemo<ListboxOption[]>(() => {
    const known = (nodes.data?.nodes ?? []).map((n) => {
      const shown = names.get(n.node_id);
      return { value: n.node_id, label: shown !== undefined && shown !== "" ? shown : n.node_id, detail: `${n.node_id} · ${n.state}` };
    });
    return [{ value: "", label: "Let the planner choose" }, ...known];
  }, [nodes.data, names]);

  // The stepper starts from the typed value, else the manifest's, else 0.
  const manifestPriority = prepared?.manifest.manifest.priority;
  const priorityBase = /^[+-]?\d+$/.test(priority.trim())
    ? Number(priority.trim())
    : typeof manifestPriority === "number"
      ? manifestPriority
      : 0;
  const stepPriority = (delta: number) => {
    setPriority(String(Math.max(-10, Math.min(10, priorityBase + delta))));
  };

  // A folder or one .zip dropped anywhere on the project card.
  const onDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setDragging(false);
    if (busy) return;
    const items = Array.from(event.dataTransfer.items).filter((item) => item.kind === "file");
    const item = items.length === 1 ? items[0] : undefined;
    const entry = item?.webkitGetAsEntry() ?? null;
    const file = item?.getAsFile() ?? null;
    if (entry?.isDirectory === true) {
      const dir = entry as FileSystemDirectoryEntry;
      void prepare(() => droppedFolderSource(dir));
    } else if (file !== null && /\.zip$/i.test(file.name)) {
      void prepare(() => readZip(file));
    } else {
      void prepare(() => Promise.reject(new ProjectError("drop one project folder or one .zip")));
    }
  };

  const fileCount = prepared === null ? 0 : prepared.tree.entries.filter((e) => e.kind === "file").length;

  return (
    <>
      <Link to="/jobs" className="sj-back">
        ← All jobs
      </Link>
      <PageHeader title="Submit a job" />

      <div className="grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] items-stretch gap-8">
        <section
          aria-label="Project"
          className="ov-card ov-frost nd-glass sj-card"
          onDragOver={(event) => {
            event.preventDefault();
            if (!busy) setDragging(true);
          }}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
          }}
          onDrop={onDrop}
        >
          <div className="flex h-7 items-center justify-between gap-4">
            <h2 className="ov-card-title">Project</h2>
            {prepared !== null && !busy && (
              <button type="button" className="sj-x" aria-label="Clear the project" onClick={cancel}>
                ×
              </button>
            )}
          </div>

          <label htmlFor={ids.folder} className="sr-only">
            Project folder
          </label>
          <input
            ref={folderInput}
            id={ids.folder}
            type="file"
            multiple
            tabIndex={-1}
            disabled={busy}
            className="sr-only"
            onChange={(event) => {
              const files = Array.from(event.currentTarget.files ?? []);
              event.currentTarget.value = "";
              if (files.length > 0) void prepare(() => Promise.resolve(folderSource(files)));
            }}
          />
          <label htmlFor={ids.zip} className="sr-only">
            …or a .zip of it
          </label>
          <input
            ref={zipInput}
            id={ids.zip}
            type="file"
            accept=".zip,application/zip"
            tabIndex={-1}
            disabled={busy}
            className="sr-only"
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              event.currentTarget.value = "";
              if (file !== undefined) void prepare(() => readZip(file));
            }}
          />

          <div className="mt-6 flex min-h-0 flex-1 flex-col">
            {phase.kind === "preparing" ? (
              <div className="sj-drop sj-in">
                <div className="flex w-full max-w-md flex-col gap-6">
                  {phase.progress === null ? (
                    <p role="status" className="text-center text-sm text-fg-muted">
                      Reading the project…
                    </p>
                  ) : (
                    <Progress
                      label="Hashing files"
                      done={phase.progress.hashedBytes}
                      total={phase.progress.totalBytes}
                      detail={`${String(phase.progress.hashedFiles)}/${String(phase.progress.totalFiles)} files · ${formatBytes(phase.progress.hashedBytes)} of ${formatBytes(phase.progress.totalBytes)}`}
                    />
                  )}
                  <div className="flex justify-center">
                    <button type="button" className="sj-link" onClick={cancel}>
                      Cancel
                    </button>
                  </div>
                </div>
              </div>
            ) : prepared !== null ? (
              <dl aria-label="Project summary" className="sj-in flex flex-1 flex-col px-1">
                <div className="flex flex-col gap-2">
                  <dt className="nd-label">Project</dt>
                  <dd className="truncate text-[1.75rem] leading-tight font-medium tracking-[-0.02em] text-fg">
                    {prepared.source.name}
                  </dd>
                </div>
                <div className="mt-10">
                  <dt className="sr-only">Files</dt>
                  <dd className="grid grid-cols-3 gap-8">
                    <Figure value={String(fileCount)} label="files" />
                    <Figure value={formatBytes(prepared.walk.totalBytes)} label="total" />
                    <Figure value={String(prepared.walk.ignored)} label="ignored" />
                  </dd>
                </div>
                <div className="mt-auto grid grid-cols-[7.5rem_minmax(0,1fr)] items-baseline gap-x-6 gap-y-3 pt-10 text-sm">
                  <dt className="nd-label">Manifest</dt>
                  <dd className="text-fg-muted">
                    {prepared.manifest.fromFile ? SPEC.manifest_file : "none - name and entrypoint inferred"}
                  </dd>
                  <dt className="nd-label">Job name</dt>
                  <dd className="truncate font-mono text-fg">{asText(prepared.manifest.manifest.name)}</dd>
                  <dt className="nd-label">Entrypoint</dt>
                  <dd className="truncate font-mono text-fg">{JSON.stringify(prepared.manifest.manifest.entrypoint)}</dd>
                  <dt className="nd-label">Tree id</dt>
                  <dd className="font-mono text-2xs break-all text-fg-subtle">{prepared.tree.treeId}</dd>
                </div>
              </dl>
            ) : (
              <div
                className={cn("sj-drop cursor-pointer", dragging && "sj-drop-over")}
                onClick={(event) => {
                  // anywhere in the drop area (not the two links) opens the folder picker
                  if (busy || (event.target as HTMLElement).closest("button")) return;
                  folderInput.current?.click();
                }}
              >
                <svg
                  aria-hidden="true"
                  viewBox="0 0 32 32"
                  width="34"
                  height="34"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.25"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="sj-drop-icon"
                >
                  <path d="M16 3.5 26.5 9.5v12L16 27.5 5.5 21.5v-12z" />
                  <path d="M16 20v-8.5M12.25 15 16 11.25 19.75 15" />
                </svg>
                <p className="mt-5 text-[1.0625rem] text-fg">Drop a folder or .zip</p>
                <div className="mt-3 flex items-center gap-3">
                  <button
                    type="button"
                    className="sj-link"
                    disabled={busy}
                    onClick={() => {
                      folderInput.current?.click();
                    }}
                  >
                    Choose folder
                  </button>
                  <span aria-hidden="true" className="text-fg-subtle">
                    ·
                  </span>
                  <button
                    type="button"
                    className="sj-link"
                    disabled={busy}
                    onClick={() => {
                      zipInput.current?.click();
                    }}
                  >
                    Choose .zip
                  </button>
                </div>
              </div>
            )}
          </div>

          {phase.kind === "failed" && phase.stage === "prepare" && (
            <div className="mt-6">
              <Failure phase={phase} />
            </div>
          )}

          <div className="mt-6 flex flex-col">
            <div className="flex items-baseline justify-end gap-6">
              <button
                type="button"
                className="sj-link inline-flex shrink-0 items-center gap-1"
                aria-expanded={showRules}
                aria-controls={ids.rules}
                onClick={() => {
                  setShowRules((v) => !v);
                }}
              >
                Rules
                <span className={cn("jb-chevron", showRules && "jb-chevron-open")}>
                  <ChevronIcon />
                </span>
              </button>
            </div>
            <div id={ids.rules} aria-hidden={!showRules} className={cn("sj-reveal", showRules && "sj-reveal-open")}>
              <div className="min-h-0 overflow-hidden">
                <p className="pt-3 text-xs leading-relaxed text-fg-muted">
                  <code className="font-mono">.gitignore</code> and <code className="font-mono">.computeignore</code> apply,
                  plus {SPEC.builtin_ignores.join(", ")}. Files are hashed in this browser; only what the controller does
                  not already hold is uploaded. A folder picker cannot see empty folders or exec bits and follows
                  symlinks - use a zip or the CLI if those matter. Interrupted? Pick the same folder again: parts the
                  controller already has are not sent twice.
                </p>
              </div>
            </div>
          </div>
        </section>

        <section aria-label="Options" className="sj-card sj-plain">
          <div className="flex h-7 items-center">
            <h2 className="ov-card-title">Options</h2>
          </div>
          <div className="mt-8 flex flex-col gap-7">
            <Field
              id={ids.name}
              label={
                <>
                  {"Name "}
                  <span className="sr-only">(overrides the manifest)</span>
                </>
              }
            >
              <input
                id={ids.name}
                className="jb-field w-full"
                placeholder={prepared !== null ? asText(prepared.manifest.manifest.name) : "from the manifest"}
                value={name}
                disabled={busy}
                onChange={(e) => {
                  setName(e.target.value);
                }}
              />
            </Field>
            <Field
              id={ids.priority}
              label={
                <>
                  {"Priority "}
                  <span className="sr-only">(-10 to 10)</span>
                </>
              }
            >
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="sj-step"
                  aria-label="Lower priority"
                  disabled={busy || priorityBase <= -10}
                  onClick={() => {
                    stepPriority(-1);
                  }}
                >
                  −
                </button>
                <input
                  id={ids.priority}
                  type="text"
                  inputMode="numeric"
                  className="jb-field w-16 text-center font-mono tabular-nums"
                  placeholder={typeof manifestPriority === "number" ? String(manifestPriority) : "0"}
                  value={priority}
                  disabled={busy}
                  aria-invalid={badPriority !== null}
                  aria-describedby={badPriority !== null ? ids.priorityError : undefined}
                  onChange={(e) => {
                    setPriority(e.target.value);
                  }}
                />
                <button
                  type="button"
                  className="sj-step"
                  aria-label="Raise priority"
                  disabled={busy || priorityBase >= 10}
                  onClick={() => {
                    stepPriority(1);
                  }}
                >
                  +
                </button>
                <span aria-hidden="true" className="nd-label ml-auto">
                  -10 to 10
                </span>
              </div>
              {badPriority !== null && (
                <p id={ids.priorityError} className="sj-in text-xs text-status-failed">
                  {badPriority}
                </p>
              )}
            </Field>
            <Field id={ids.policy} label="Placement policy">
              <ListboxField id={ids.policy} value={policy} options={policyOptions} onChange={setPolicy} disabled={busy} />
            </Field>
            <Field id={ids.node} label="Node">
              <ListboxField id={ids.node} value={node} options={nodeOptions} onChange={setNode} disabled={busy} />
            </Field>
          </div>

          <div className="mt-auto flex flex-col gap-5 pt-8">
            {phase.kind === "uploading" &&
              (phase.progress === null ? (
                <p role="status" className="sj-in text-sm text-fg-muted">
                  Asking the controller what it already has…
                </p>
              ) : (
                <Progress
                  label="Uploading"
                  done={phase.progress.sentBytes}
                  total={phase.progress.totalBytes}
                  detail={`${String(phase.progress.sentBlobs)}/${String(phase.progress.totalBlobs)} files · ${formatBytes(phase.progress.sentBytes)} of ${formatBytes(phase.progress.totalBytes)}`}
                />
              ))}
            {phase.kind === "submitting" && (
              <p role="status" className="sj-in text-sm text-fg-muted">
                Uploaded {phase.report.uploadedBlobs} files ({formatBytes(phase.report.uploadedBytes)}), skipped{" "}
                {phase.report.skippedBlobs} already on the controller
                {phase.report.resumedParts > 0 ? `, resumed ${String(phase.report.resumedParts)} parts` : ""}. Submitting…
              </p>
            )}
            {phase.kind === "failed" && phase.stage !== "prepare" && (
              <div className="flex flex-col gap-2">
                <Failure phase={phase} />
                {phase.stage === "upload" && (
                  <p className="text-xs text-fg-subtle">
                    Interrupted? Pick the same folder again: parts the controller already has are not sent twice.
                  </p>
                )}
              </div>
            )}
            <div className="flex items-center justify-end gap-6">
              {(phase.kind === "uploading" || phase.kind === "submitting") && (
                <button type="button" className="sj-link" onClick={cancel}>
                  Cancel upload
                </button>
              )}
              <button
                type="button"
                className="sj-go"
                disabled={prepared === null || busy || badPriority !== null}
                onClick={() => {
                  if (prepared !== null) void send(prepared);
                }}
              >
                {phase.kind === "failed" && phase.stage !== "prepare" ? "Retry upload and submit" : "Upload and submit"}
              </button>
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
