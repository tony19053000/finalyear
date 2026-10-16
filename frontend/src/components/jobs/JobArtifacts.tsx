import { CodeBlock } from "../CodeBlock";
import { EmptyState } from "../States";
import { blobUrl, type AttemptSummary } from "../../pages/jobs/jobsApi";

/**
 * A job's outputs (D176 "download artifacts"): each attempt that uploaded
 * outputs has one verified archive (`output_digest`, a zstd-compressed tar -
 * what `compute get` unpacks). The browser downloads it as is from
 * GET /v1/blobs/{digest} (the cookie session may read its own jobs' outputs).
 */
export function JobArtifacts({ jobId, attempts, terminal }: { jobId: string; attempts: readonly AttemptSummary[]; terminal: boolean }) {
  const withOutputs = [...attempts]
    .filter((a) => a.output_digest != null && a.output_digest !== "")
    .sort((a, b) => b.fencing_token - a.fencing_token);
  const missing = [...new Set(attempts.flatMap((a) => a.missing_outputs ?? []))];
  if (withOutputs.length === 0) {
    return (
      <EmptyState title="No outputs">
        {terminal
          ? "This job finished without uploading outputs (the manifest declared none, or nothing matched)."
          : "Outputs appear here when an attempt finishes and its upload is verified."}
        {missing.length > 0 && <span> Declared but not found: {missing.join(", ")}.</span>}
      </EmptyState>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-3">
        {withOutputs.map((a) => {
          const digest = a.output_digest ?? "";
          const file = `${jobId}-attempt${String(a.fencing_token)}-outputs.tar.zst`;
          return (
            <li key={a.attempt_id} className="flex flex-col gap-1 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm">
                  Attempt {a.fencing_token} on <span className="font-mono">{a.node_id}</span> ({a.state})
                </span>
                <a
                  href={blobUrl(digest)}
                  download={file}
                  className="inline-flex h-8 items-center rounded-md border border-transparent bg-accent px-3 text-sm font-medium text-accent-fg hover:bg-accent-hover"
                >
                  Download outputs
                </a>
              </div>
              <span className="font-mono text-2xs break-all text-fg-muted">{digest}</span>
              {(a.missing_outputs ?? []).length > 0 && (
                <span className="text-xs text-fg-muted">Declared but not found: {(a.missing_outputs ?? []).join(", ")}</span>
              )}
            </li>
          );
        })}
      </ul>
      <div className="text-sm text-fg-muted">
        <p>The file is a zstd-compressed tar. Check and unpack it, or let the CLI do both:</p>
        <CodeBlock
          className="mt-2"
          label="Unpack commands"
          code={`sha256sum <file>   # must equal the digest above\ntar --zstd -xf <file>\n# or: compute get ${jobId}`}
        />
      </div>
    </div>
  );
}
