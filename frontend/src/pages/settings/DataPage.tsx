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
import { CodeBlock } from "../../components/CodeBlock";
import { ErrorState } from "../../components/States";
import { useToast } from "../../components/toastContext";
import { formatBytes } from "../../lib/format";
import { useConfirmAction } from "./ConfirmAction";
import { Footnote, Intro, Loading, RowMenu, Section, SettingsList, StateWord, When, type StColumn } from "./settingsUi";
import { useAction } from "./useAction";

/** The row's "⋯" menu: Pin / Unpin (at once) and Delete (behind its confirm dialog). */
function DataActions({ input }: { input: InputOut }) {
  const client = useQueryClient();
  const toast = useToast();
  const pin = useAction(setInputPinned);
  const pinLabel = input.pinned ? "Unpin" : "Pin";
  const remove = useConfirmAction({
    title: `Delete ${input.ref}?`,
    description:
      "This version's name is removed (refused while an unfinished job uses it). Jobs that name it later fail to submit; other versions stay.",
    confirmLabel: "Delete version",
    action: () => removeInput(input.name, input.version),
    done: () => {
      void client.invalidateQueries({ queryKey: queryKeys.inputs });
      return { title: `${input.ref} deleted` };
    },
  });

  const toggle = async () => {
    const result = await pin.run(input.name, input.version, !input.pinned);
    if (result === undefined) return; // shown below from pin.state
    void client.invalidateQueries({ queryKey: queryKeys.inputs });
    toast({ title: `${input.ref} ${result.pinned ? "pinned" : "unpinned"}`, tone: "ok" });
  };

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <RowMenu
        label={`Actions for ${input.ref}`}
        busy={pin.pending}
        items={[
          {
            key: "pin",
            label: pinLabel,
            name: `${pinLabel} ${input.ref}`,
            onSelect: () => {
              void toggle();
            },
          },
          { key: "delete", label: "Delete", name: `Delete ${input.ref}`, danger: true, onSelect: remove.open },
        ]}
      />
      {pin.state.status === "error" && (
        <span role="alert" className="text-xs whitespace-normal text-status-failed">
          {describeError(pin.state.error).message}
        </span>
      )}
      {remove.dialog}
    </span>
  );
}

const PUSH_HINT = "A new version is uploaded from a laptop with the CLI; jobs then name it in inputs:.";

export function DataPage() {
  useDocumentTitle("Data");
  const inputs = useInputs();

  const columns: StColumn<InputOut>[] = [
    { id: "ref", header: "Input", mono: true, grow: true, cell: (i) => <span className="text-fg">{i.ref}</span> },
    { id: "files", header: "Files", cell: (i) => String(i.files) },
    { id: "size", header: "Size", cell: (i) => formatBytes(i.size) },
    {
      id: "pinned",
      header: "Cache",
      hint: "A pinned version is kept in every agent's cache.",
      cell: (i) => (i.pinned ? <StateWord tone="accent">pinned</StateWord> : <StateWord tone="neutral">not pinned</StateWord>),
    },
    { id: "created", header: "Created", cell: (i) => <When iso={i.created_at} /> },
    {
      id: "tree",
      header: "Tree",
      mono: true,
      hint: "The content address of this version's file tree.",
      cell: (i) => (
        <span className="cursor-help" title={i.tree_id}>
          {i.tree_id.slice(0, 19)}…
        </span>
      ),
    },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (i) => <DataActions input={i} /> },
  ];

  const rows = inputs.data?.inputs ?? [];
  const pinned = rows.filter((i) => i.pinned).length;
  return (
    <>
      <Intro>
        Datasets and models shared by the fabric, every version. Jobs name one as <span className="font-mono text-fg">name@version</span>;
        a pinned version stays in every agent&apos;s cache.
      </Intro>
      {inputs.isPending ? (
        <Loading />
      ) : inputs.isError ? (
        <ErrorState
          title="Could not load the named inputs"
          error={inputs.error}
          onRetry={() => {
            void inputs.refetch();
          }}
        />
      ) : (
        <>
          <SettingsList
            caption="Named inputs (datasets, models), every version"
            columns={columns}
            rows={rows}
            rowKey={(i) => i.ref}
            empty="No named inputs yet."
          />
          <Footnote>{`${String(rows.length)} versions · ${String(pinned)} pinned. Deleting a version is refused while an unfinished job uses it.`}</Footnote>
        </>
      )}
      <Section title="Adding data" description="New versions come from a laptop, with the CLI." hint={PUSH_HINT}>
        <p className="st-prose cursor-help" title={PUSH_HINT}>
          Push a folder as a new version; jobs then name it in <code className="font-mono text-fg">inputs:</code>
        </p>
        <CodeBlock className="st-code mt-4" label="data push command" code={"compute data push NAME ./folder [--pin]"} />
      </Section>
    </>
  );
}
