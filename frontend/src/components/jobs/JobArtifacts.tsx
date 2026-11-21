import { EmptyState } from "../States";
import { blobUrl, type AttemptSummary } from "../../pages/jobs/jobsApi";

/**
 * A job's outputs (D176 "download artifacts"): each attempt that uploaded
 * outputs has one verified archive (`output_digest`, a zstd-compressed tar -
 * what `compute get` unpacks). The browser downloads it as is from
 * GET /v1/blobs/{digest} (the cookie session may read its own jobs' outputs).
 */
export function JobArtifacts({
  jobId,
  attempts,
  terminal,
  names,
}: {
  jobId: string;
  attempts: readonly AttemptSummary[];
  terminal: boolean;
  names?: ReadonlyMap<string, string>;
}) {
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
    <div className="flex flex-col gap-10">
      <section aria-label="Outputs" className="ov-card ov-frost nd-glass px-9 pt-7 pb-3">
        <h2 className="ov-card-title">Outputs</h2>
        <ul className="mt-4 divide-y divide-[#ffffff0f]">
          {withOutputs.map((a) => {
            const digest = a.output_digest ?? "";
            const file = `${jobId}-attempt${String(a.fencing_token)}-outputs.tar.zst`;
            return (
              <li key={a.attempt_id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-10 gap-y-1 py-5">
                <span className="min-w-0">
                  <span className="block text-[0.9375rem] text-fg">
                    Attempt {a.fencing_token}
                    {a.role === "combine" ? " (combined output)" : ""} on{" "}
                    <span className="font-medium">{names?.get(a.node_id) ?? a.node_id}</span>
                    <span className="ml-2 font-mono text-[0.75rem] tracking-[0.12em] text-fg uppercase">{a.state}</span>
                  </span>
                  <span className="mt-1.5 block font-mono text-xs break-all text-fg/80">{digest}</span>
                  {(a.missing_outputs ?? []).length > 0 && (
                    <span className="mt-1 block text-xs text-fg-muted">
                      Declared but not found: {(a.missing_outputs ?? []).join(", ")}
                    </span>
                  )}
                </span>
                <a href={blobUrl(digest)} download={file} className="jb-honey-btn">
                  Download outputs
                </a>
              </li>
            );
          })}
        </ul>
      </section>
      <div className="px-2 text-sm text-fg-muted">
        <p>The file is a zstd-compressed tar. Check and unpack it, or let the CLI do both:</p>
        <pre aria-label="Unpack commands" tabIndex={0} className="jb-log mt-4 text-fg">
          <code>{`sha256sum <file>   # must equal the digest above\ntar --zstd -xf <file>\n# or: compute get ${jobId}`}</code>
        </pre>
      </div>
    </div>
  );
}
