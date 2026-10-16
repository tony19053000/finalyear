/**
 * Settings > Secrets (D176, D144): `GET /v1/secrets` (names and times only -
 * no endpoint ever returns a value), `PUT /v1/secrets/{name}` (raw value),
 * `DELETE /v1/secrets/{name}`. The same calls as `compute secret`.
 *
 * The value is write-only: typed into a password field (or read from a file),
 * sent once, then cleared. It is never put in the query cache, storage, the
 * URL, the DOM after sending, or a log line (checklist 25).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { describeError } from "../../api/errors";
import { queryKeys } from "../../api/queryKeys";
import {
  SECRET_MAX_BYTES,
  SECRET_MIN_BYTES,
  deleteSecret,
  normaliseSecretName,
  putSecret,
  useSecrets,
  utf8Length,
  type SecretOut,
} from "../../api/settings";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { Dialog } from "../../components/Dialog";
import { ErrorState } from "../../components/States";
import { useToast } from "../../components/toastContext";
import { Table, type Column } from "../../components/Table";
import { formatDateTime } from "../../lib/format";
import { ConfirmAction } from "./ConfirmAction";
import { Field } from "./Field";
import { useAction } from "./useAction";

const input =
  "h-8 rounded-md border border-border-strong bg-surface px-2.5 text-sm text-fg aria-[invalid=true]:border-status-failed";

function valueProblem(bytes: number): string | null {
  if (bytes === 0) return null; // nothing typed yet: no nagging
  if (bytes < SECRET_MIN_BYTES) return `At least ${String(SECRET_MIN_BYTES)} bytes (short values would be masked all over the logs).`;
  if (bytes > SECRET_MAX_BYTES) return `At most ${String(SECRET_MAX_BYTES / 1024)} KiB.`;
  return null;
}

function SetSecretForm({
  existing,
  onDone,
  onBusy,
}: {
  existing: readonly string[];
  onDone: () => void;
  onBusy: (busy: boolean) => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const [rawName, setRawName] = useState("");
  const [value, setValue] = useState("");
  const [fileNote, setFileNote] = useState<string | null>(null);
  // A file's value is kept as its exact bytes (T-1): never decoded as text.
  const [fileBytes, setFileBytes] = useState<Uint8Array<ArrayBuffer> | null>(null);
  const save = useAction(putSecret);
  const first = useRef<HTMLInputElement>(null);
  const name = normaliseSecretName(rawName);
  const bytes = fileBytes !== null ? fileBytes.length : utf8Length(value);
  const problem = valueProblem(bytes);
  const replacing = name !== null && existing.includes(name);
  const ready = name !== null && bytes >= SECRET_MIN_BYTES && problem === null && !save.pending;

  useEffect(() => {
    first.current?.focus();
  }, []);
  useEffect(() => {
    onBusy(save.pending);
  }, [save.pending, onBusy]);

  const readFile = async (file: File | undefined) => {
    if (file === undefined) return;
    if (file.size > SECRET_MAX_BYTES) {
      setFileNote(`${file.name} is larger than ${String(SECRET_MAX_BYTES / 1024)} KiB.`);
      return;
    }
    const read = new Uint8Array(await file.arrayBuffer());
    setValue("");
    setFileBytes(read);
    setFileNote(`Value read from ${file.name} (${String(read.length)} bytes, sent exactly as in the file).`);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    const sent = fileBytes ?? value;
    setValue(""); // write-only: gone from the page once sent
    setFileBytes(null);
    setFileNote(null);
    const stored = await save.run(name, sent);
    if (stored === undefined) return;
    void client.invalidateQueries({ queryKey: queryKeys.secrets });
    toast({ title: stored.created ? `Secret ${stored.name} stored` : `Secret ${stored.name} replaced`, tone: "ok" });
    onDone();
  };

  const error = save.state.status === "error" ? save.state.error : null;
  return (
    <form
      onSubmit={(e) => {
        void submit(e);
      }}
      className="flex flex-col gap-3"
    >
      <Field
        label="Name"
        help={
          rawName.trim() !== "" && name === null
            ? "1-64 characters: A-Z, 0-9 and _, not starting with a digit."
            : name !== null && name !== rawName.trim()
              ? `Stored as ${name}.`
              : "A-Z, 0-9 and _ (lower case is upper-cased). A job asks for it by this name."
        }
      >
        {(control) => (
          <input
            {...control}
            ref={first}
            className={`${input} font-mono`}
            value={rawName}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={rawName.trim() !== "" && name === null}
            onChange={(e) => {
              setRawName(e.target.value);
            }}
          />
        )}
      </Field>
      <Field label="Value" help={problem ?? fileNote ?? "Write-only: it is never shown again, here or anywhere."}>
        {(control) => (
          <input
            {...control}
            className={`${input} font-mono`}
            type="password"
            value={value}
            autoComplete="new-password"
            aria-invalid={problem !== null}
            onChange={(e) => {
              setValue(e.target.value);
              setFileBytes(null); // typing replaces a value read from a file
              setFileNote(null);
            }}
          />
        )}
      </Field>
      <Field label="Or read the value from a file (keys, certificates, multi-line values)">
        {(control) => (
          <input
            {...control}
            type="file"
            className="text-sm text-fg-muted"
            onChange={(e) => {
              void readFile(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
        )}
      </Field>
      {replacing && (
        <p role="status" className="text-sm text-status-busy">
          {name} already exists: its value will be replaced.
        </p>
      )}
      {error !== null && (
        <p role="alert" className="text-sm text-status-failed">
          Not stored: {describeError(error).message} The value was cleared; enter it again to retry.
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button onClick={onDone} disabled={save.pending}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" loading={save.pending} disabled={!ready}>
          {replacing ? "Replace secret" : "Store secret"}
        </Button>
      </div>
    </form>
  );
}

function SetSecret({ existing }: { existing: readonly string[] }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => {
    if (!busy) setOpen(false);
  };
  return (
    <>
      <Button
        variant="primary"
        size="sm"
        onClick={() => {
          setOpen(true);
        }}
      >
        Set a secret
      </Button>
      <Dialog
        open={open}
        onClose={close}
        title="Set a secret"
        description="Stored encrypted on the controller; only your jobs that ask for it receive it, at run time."
      >
        <SetSecretForm existing={existing} onDone={close} onBusy={setBusy} />
      </Dialog>
    </>
  );
}

export function SecretsPage() {
  useDocumentTitle("Secrets");
  const secrets = useSecrets();
  const client = useQueryClient();
  const names = secrets.data?.secrets.map((s) => s.name) ?? [];

  const columns: Column<SecretOut>[] = [
    { id: "name", header: "Name", mono: true, cell: (s) => s.name },
    { id: "created", header: "Created", cell: (s) => formatDateTime(s.created_at) },
    { id: "updated", header: "Updated", cell: (s) => formatDateTime(s.updated_at) },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (s) => (
        <ConfirmAction
          label="Delete"
          triggerName={`Delete secret ${s.name}`}
          title={`Delete ${s.name}?`}
          description="Jobs that ask for it will fail to start until it is set again. The value cannot be recovered."
          confirmLabel="Delete secret"
          action={() => deleteSecret(s.name)}
          done={() => {
            void client.invalidateQueries({ queryKey: queryKeys.secrets });
            return { title: `Secret ${s.name} deleted` };
          }}
        />
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <Card title="Your secrets" actions={<SetSecret existing={names} />} flush>
        {secrets.isPending ? (
          <p role="status" className="p-4 text-sm text-fg-muted">
            Loading…
          </p>
        ) : secrets.isError ? (
          <ErrorState
            className="m-4"
            title="Could not load your secrets"
            error={secrets.error}
            onRetry={() => {
              void secrets.refetch();
            }}
          />
        ) : (
          <Table
            caption="Your secrets (names only - values are never shown)"
            columns={columns}
            rows={secrets.data.secrets}
            rowKey={(s) => s.name}
            empty="No secrets yet."
          />
        )}
      </Card>
      <Card title="Using a secret in a job">
        <p className="text-sm text-fg-muted">
          A job lists the secrets it needs in its <code className="font-mono">compute.yaml</code>; the agent gives each one
          to the job as a file (and, with <code className="font-mono">as_env</code>, an environment variable); every log line is scrubbed of it:
        </p>
        <CodeBlock className="mt-3" label="compute.yaml secrets example" code={"secrets:\n  - API_KEY                         # file /run/secrets/API_KEY\n  - {name: DB_PASS, as_env: true}   # also the env var DB_PASS"} />
      </Card>
    </div>
  );
}
