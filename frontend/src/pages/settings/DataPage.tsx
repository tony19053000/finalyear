/**
 * Settings > Data (D176, V0.5 D92): named inputs shared by the fabric -
 * `GET /v1/data` (every version), `PATCH /v1/data/{name}/{version}` (pin or
 * unpin: a pinned version is kept in every agent's cache), `DELETE
 * /v1/data/{name}/{version}` (409 naming the jobs while an unfinished job uses
 * it). The same calls as `compute data ls | pin | rm`. Pushing a new version
 * stays with `compute data push` (the CLI uploads the folder).
 */
import { useQueryClient } from "@tanstack/react-query";
import { describeError } from "../../api/errors";
import { queryKeys } from "../../api/queryKeys";
import { removeInput, setInputPinned, useInputs, type InputOut } from "../../api/settings";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { useToast } from "../../components/toastContext";
import { Table, type Column } from "../../components/Table";
import { formatBytes, formatDateTime } from "../../lib/format";
import { ConfirmAction } from "./ConfirmAction";
import { useAction } from "./useAction";

function PinToggle({ input }: { input: InputOut }) {
  const client = useQueryClient();
  const toast = useToast();
  const pin = useAction(setInputPinned);
  const label = input.pinned ? "Unpin" : "Pin";

  const toggle = async () => {
    const result = await pin.run(input.name, input.version, !input.pinned);
    if (result === undefined) return; // shown below from pin.state
    void client.invalidateQueries({ queryKey: queryKeys.inputs });
    toast({ title: `${input.ref} ${result.pinned ? "pinned" : "unpinned"}`, tone: "ok" });
  };

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Button
        size="sm"
        aria-label={`${label} ${input.ref}`}
        loading={pin.pending}
        onClick={() => {
          void toggle();
        }}
      >
        {label}
      </Button>
      {pin.state.status === "error" && (
        <span role="alert" className="text-xs text-status-failed">
          {describeError(pin.state.error).message}
        </span>
      )}
    </span>
  );
}

export function DataPage() {
  useDocumentTitle("Data");
  const inputs = useInputs();
  const client = useQueryClient();

  const columns: Column<InputOut>[] = [
    { id: "ref", header: "Input", mono: true, cell: (i) => i.ref },
    { id: "files", header: "Files", align: "right", cell: (i) => String(i.files) },
    { id: "size", header: "Size", align: "right", cell: (i) => formatBytes(i.size) },
    {
      id: "pinned",
      header: "Cache",
      cell: (i) => (i.pinned ? <Badge tone="accent">pinned</Badge> : <Badge>not pinned</Badge>),
    },
    { id: "created", header: "Created", cell: (i) => formatDateTime(i.created_at) },
    { id: "tree", header: "Tree", mono: true, cell: (i) => <span title={i.tree_id}>{i.tree_id.slice(0, 19)}…</span> },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (i) => (
        <span className="inline-flex items-start justify-end gap-2">
          <PinToggle input={i} />
          <ConfirmAction
            label="Delete"
            triggerName={`Delete ${i.ref}`}
            title={`Delete ${i.ref}?`}
            description="This version's name is removed (refused while an unfinished job uses it). Jobs that name it later fail to submit; other versions stay."
            confirmLabel="Delete version"
            action={() => removeInput(i.name, i.version)}
            done={() => {
              void client.invalidateQueries({ queryKey: queryKeys.inputs });
              return { title: `${i.ref} deleted` };
            }}
          />
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <Card title="Named inputs" flush>
        {inputs.isPending ? (
          <p role="status" className="p-4 text-sm text-fg-muted">
            Loading…
          </p>
        ) : inputs.isError ? (
          <ErrorState
            className="m-4"
            title="Could not load the named inputs"
            error={inputs.error}
            onRetry={() => {
              void inputs.refetch();
            }}
          />
        ) : (
          <Table
            caption="Named inputs (datasets, models), every version"
            columns={columns}
            rows={inputs.data.inputs}
            rowKey={(i) => i.ref}
            empty="No named inputs yet."
          />
        )}
      </Card>
      <Card title="Adding data">
        <p className="text-sm text-fg-muted">
          A new version is uploaded from a laptop with the CLI; jobs then name it in <code className="font-mono">inputs:</code>.
        </p>
        <CodeBlock className="mt-3" label="data push command" code={"compute data push NAME ./folder [--pin]"} />
      </Card>
    </div>
  );
}
