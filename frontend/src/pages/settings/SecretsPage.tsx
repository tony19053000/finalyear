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
import { CodeBlock } from "../../components/CodeBlock";
import { Dialog } from "../../components/Dialog";
import { ErrorState } from "../../components/States";
import { useToast } from "../../components/toastContext";
import { useConfirmAction } from "./ConfirmAction";
import { FIELD_CLASS, Field } from "./Field";
import { AddButton, Footnote, Intro, Loading, RowMenu, Section, SettingsList, When, type StColumn } from "./settingsUi";
import { useAction } from "./useAction";

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
  // A file the browser could not read (moved, deleted, no permission): shown as an alert.
  const [fileError, setFileError] = useState<string | null>(null);
  // A file's value is kept as its exact bytes (T-1): never decoded as text.
  const [fileBytes, setFileBytes] = useState<Uint8Array<ArrayBuffer> | null>(null);
  const save = useAction(putSecret);
  const first = useRef<HTMLInputElement>(null);
  // D204 (V0.8 G7): every file choice, keystroke and send takes a new read
  // number; a file read that finishes after a newer choice is dropped, so a slow
  // read can never overwrite what the user chose last.
  const readSeq = useRef(0);
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
    const mine = ++readSeq.current;
    setFileError(null);
    if (file.size > SECRET_MAX_BYTES) {
      // T-5: never keep a previously chosen value behind a refusal - Store must not send the old file.
      setValue("");
      setFileBytes(null);
      setFileNote(`${file.name} is larger than ${String(SECRET_MAX_BYTES / 1024)} KiB.`);
      return;
    }
    let read: Uint8Array<ArrayBuffer>;
    try {
      read = new Uint8Array(await file.arrayBuffer());
    } catch {
      // G7 tester r1 T-9: a rejected read is reported, never an unhandled rejection.
      if (mine !== readSeq.current) return; // a newer choice won anyway
      setValue(""); // as T-5: no older value is kept behind the failure
      setFileBytes(null);
      setFileNote(null);
      setFileError(
        `Could not read ${file.name}: the browser cannot open it (moved, deleted or not permitted). Choose it again or type the value.`,
      );
      return;
    }
    if (mine !== readSeq.current) return; // a newer choice won
    setValue("");
    setFileBytes(read);
    if (read.length === 0) {
      setFileNote(`${file.name} is empty - a secret needs at least ${String(SECRET_MIN_BYTES)} bytes.`);
      return;
    }
    setFileNote(`Value read from ${file.name} (${String(read.length)} bytes, sent exactly as in the file).`);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    const sent = fileBytes ?? value;
    readSeq.current += 1; // a read still in flight must not refill the form
    setValue(""); // write-only: gone from the page once sent
    setFileBytes(null);
    setFileNote(null);
    setFileError(null);
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
      className="flex flex-col gap-5"
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
            className={`${FIELD_CLASS} font-mono`}
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
            className={`${FIELD_CLASS} font-mono`}
            type="password"
            value={value}
            autoComplete="new-password"
            aria-invalid={problem !== null}
            onChange={(e) => {
              setValue(e.target.value);
              readSeq.current += 1; // typing also beats a file read still in flight
              setFileBytes(null); // typing replaces a value read from a file
              setFileNote(null);
              setFileError(null);
            }}
          />
        )}
      </Field>
      <Field label="Or read the value from a file (keys, certificates, multi-line values)">
        {(control) => (
          <input
            {...control}
            type="file"
            className="st-file"
            onChange={(e) => {
              void readFile(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
        )}
      </Field>
      {fileError !== null && (
        <p role="alert" className="text-sm text-status-failed">
          {fileError}
        </p>
      )}
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

/** The section's primary action: the dialog with the write-only form (the names come from the same query as the list). */
export function SetSecret() {
  const secrets = useSecrets();
  const existing = secrets.data?.secrets.map((s) => s.name) ?? [];
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => {
    if (!busy) setOpen(false);
  };
  return (
    <>
      <AddButton
        label="Set a secret"
        onClick={() => {
          setOpen(true);
        }}
      />
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

const USING_HINT =
  "A job lists the secrets it needs in its compute.yaml; the agent gives each one to the job as a file (and, with as_env, an environment variable); every log line is scrubbed of it.";

/** The row's "⋯" menu: Delete behind its confirm dialog. */
function SecretActions({ secret: s }: { secret: SecretOut }) {
  const client = useQueryClient();
  const remove = useConfirmAction({
    title: `Delete ${s.name}?`,
    description: "Jobs that ask for it will fail to start until it is set again. The value cannot be recovered.",
    confirmLabel: "Delete secret",
    action: () => deleteSecret(s.name),
    done: () => {
      void client.invalidateQueries({ queryKey: queryKeys.secrets });
      return { title: `Secret ${s.name} deleted` };
    },
  });
  return (
    <>
      <RowMenu
        label={`Actions for secret ${s.name}`}
        items={[{ key: "delete", label: "Delete", name: `Delete secret ${s.name}`, danger: true, onSelect: remove.open }]}
      />
      {remove.dialog}
    </>
  );
}

export function SecretsPage() {
  useDocumentTitle("Secrets");
  const secrets = useSecrets();

  const columns: StColumn<SecretOut>[] = [
    { id: "name", header: "Name", mono: true, grow: true, cell: (s) => s.name },
    { id: "created", header: "Created", cell: (s) => <When iso={s.created_at} /> },
    { id: "updated", header: "Updated", cell: (s) => <When iso={s.updated_at} /> },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (s) => <SecretActions secret={s} /> },
  ];

  return (
    <>
      <Intro actions={<SetSecret />}>
        Values your jobs can ask for by name, such as API keys and passwords. A value is write-only: once stored it is never
        shown again, here or anywhere.
      </Intro>
      {secrets.isPending ? (
        <Loading />
      ) : secrets.isError ? (
        <ErrorState
          title="Could not load your secrets"
          error={secrets.error}
          onRetry={() => {
            void secrets.refetch();
          }}
        />
      ) : (
        <>
          <SettingsList
            caption="Your secrets (names only - values are never shown)"
            columns={columns}
            rows={secrets.data.secrets}
            rowKey={(s) => s.name}
            empty="No secrets yet."
          />
          <Footnote>{`${String(secrets.data.secrets.length)} stored. Names and times only; a value is never sent back by the controller.`}</Footnote>
        </>
      )}
      <Section title="Using a secret in a job" description="List it in the job's compute.yaml." hint={USING_HINT}>
        <p className="st-prose cursor-help" title={USING_HINT}>
          The agent gives each listed secret to the job as a file (with <code className="font-mono text-fg">as_env</code>, also
          as an environment variable) and scrubs it from every log line.
        </p>
        <CodeBlock
          className="st-code mt-4"
          label="compute.yaml secrets example"
          code={"secrets:\n  - API_KEY                         # file /run/secrets/API_KEY\n  - {name: DB_PASS, as_env: true}   # also the env var DB_PASS"}
        />
      </Section>
    </>
  );
}
