import { useState } from "react";

import { describeError } from "../../api/errors";
import { useToast } from "../../components/toastContext";
import { downloadLabels, downloadRecords } from "./benchApi";
import { FrostCard } from "./benchUi";
import { IncompleteDownloadError, isCompleteParquet, labelsFileName, recordsFileName, saveBlob } from "./download";

type Which = "records" | "labels";

const FETCH: Record<Which, (campaignId: string) => Promise<Blob>> = {
  records: (id) => downloadRecords(id),
  labels: (id) => downloadLabels(id),
};

const FILE: Record<Which, (campaignId: string) => string> = { records: recordsFileName, labels: labelsFileName };

/** One file: what it holds, the CLI that writes the same bytes, the honey download action. */
function FileBlock({
  title,
  file,
  children,
  command,
  action,
  busy,
  disabled,
  onClick,
}: {
  title: string;
  file: string;
  children: string;
  command: string;
  action: string;
  busy: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-5">
      <div>
        <h2 className="ov-card-title">{title}</h2>
        <p className="mt-1.5 truncate font-mono text-[0.75rem] text-fg-muted" title={file}>
          {file}
        </p>
      </div>
      <p className="max-w-xl text-[0.9375rem] leading-7 text-fg-muted">{children}</p>
      <p className="font-mono text-[0.75rem] break-all text-fg-subtle">
        <span className="nd-label mr-3">same bytes as</span>
        {command}
      </p>
      <div className="mt-auto pt-2">
        <button type="button" className="jb-honey-btn bm-btn" aria-busy={busy || undefined} disabled={busy || disabled} onClick={onClick}>
          {busy && <span aria-hidden="true" className="size-3 animate-spin rounded-full border-2 border-current border-t-transparent" />}
          {action}
        </button>
      </div>
    </div>
  );
}

/** Download: the run records and the labels table as Parquet (docs/TELEMETRY.md). */
export function DownloadTab({ campaignId }: { campaignId: string }) {
  const toast = useToast();
  const [busy, setBusy] = useState<Which | null>(null);

  const download = async (which: Which) => {
    if (busy !== null) return;
    setBusy(which);
    try {
      const blob = await FETCH[which](campaignId);
      if (!(await isCompleteParquet(blob))) throw new IncompleteDownloadError(which);
      saveBlob(blob, FILE[which](campaignId));
      toast({ title: `Saved ${FILE[which](campaignId)}`, description: `${String(blob.size)} bytes.`, tone: "ok" });
    } catch (err) {
      const { message, code } = describeError(err);
      toast({ title: `Could not download the ${which}`, description: code ? `${message} (${code})` : message, tone: "failed" });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="nd-stagger flex flex-col gap-8">
      <FrostCard label="Downloads" className="nd-glass grid grid-cols-2 gap-x-16 px-10 py-9">
        <FileBlock
          title="Run records"
          file={recordsFileName(campaignId)}
          command={`compute telemetry export --campaign ${campaignId}`}
          action="Download records (Parquet)"
          busy={busy === "records"}
          disabled={busy !== null && busy !== "records"}
          onClick={() => void download("records")}
        >
          Every run record of this campaign (telemetry schema, one row per attempt) - the dataset the planner learns from.
        </FileBlock>
        <FileBlock
          title="Labels"
          file={labelsFileName(campaignId)}
          command={`compute bench labels ${campaignId} -o FILE.parquet`}
          action="Download labels (Parquet)"
          busy={busy === "labels"}
          disabled={busy !== null && busy !== "labels"}
          onClick={() => void download("labels")}
        >
          One row per (group, configuration) with its costs and the group&apos;s label.
        </FileBlock>
      </FrostCard>
      <FrostCard title="Read the files" className="nd-glass">
        <div className="px-10 pt-5 pb-9">
          <pre aria-label="read the files" tabIndex={0} className="jb-log text-fg">
            <code>{`import pyarrow.parquet as pq\nrecords = pq.read_table("${recordsFileName(campaignId)}")\nlabels = pq.read_table("${labelsFileName(campaignId)}")`}</code>
          </pre>
        </div>
      </FrostCard>
    </div>
  );
}
