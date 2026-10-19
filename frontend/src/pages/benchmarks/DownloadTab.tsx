import { useState } from "react";

import { describeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { useToast } from "../../components/toastContext";
import { downloadLabels, downloadRecords } from "./benchApi";
import { IncompleteDownloadError, isCompleteParquet, labelsFileName, recordsFileName, saveBlob } from "./download";

type Which = "records" | "labels";

const FETCH: Record<Which, (campaignId: string) => Promise<Blob>> = {
  records: (id) => downloadRecords(id),
  labels: (id) => downloadLabels(id),
};

const FILE: Record<Which, (campaignId: string) => string> = { records: recordsFileName, labels: labelsFileName };

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
    <div className="flex flex-col gap-4">
      <Card title="Run records">
        <div className="flex flex-col gap-2 text-sm text-fg-muted">
          <p>
            Every run record of this campaign (telemetry schema, one row per attempt) - the dataset the planner learns from. Same bytes as{" "}
            <code className="font-mono">compute telemetry export --campaign {campaignId}</code>.
          </p>
          <div>
            <Button
              variant="primary"
              loading={busy === "records"}
              disabled={busy !== null && busy !== "records"}
              onClick={() => void download("records")}
            >
              Download records (Parquet)
            </Button>
          </div>
        </div>
      </Card>
      <Card title="Labels">
        <div className="flex flex-col gap-2 text-sm text-fg-muted">
          <p>
            One row per (group, configuration) with its costs and the group&apos;s label. Same bytes as{" "}
            <code className="font-mono">compute bench labels {campaignId} -o FILE.parquet</code>.
          </p>
          <div>
            <Button
              variant="primary"
              loading={busy === "labels"}
              disabled={busy !== null && busy !== "labels"}
              onClick={() => void download("labels")}
            >
              Download labels (Parquet)
            </Button>
          </div>
        </div>
      </Card>
      <CodeBlock
        label="read the files"
        code={`import pyarrow.parquet as pq\nrecords = pq.read_table("${recordsFileName(campaignId)}")\nlabels = pq.read_table("${labelsFileName(campaignId)}")`}
      />
    </div>
  );
}
