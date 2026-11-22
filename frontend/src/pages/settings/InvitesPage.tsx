/**
 * Settings > Invites (D176, admin only): `POST /v1/fabric/invites` (TTL,
 * optional node to re-key), `GET /v1/fabric/invites` (never a code),
 * `POST /v1/fabric/invites/{id}/revoke`. The same calls as `compute fabric
 * invite | invites`.
 *
 * The join code is shown ONCE, in the dialog, as text (copy) and as a QR code
 * of exactly that string - what `meshforge-agent join` / `compute fabric join`
 * accept (D199). Closing the dialog unmounts it: the code is never in the
 * query cache, storage or the URL (checklist 25).
 *
 * The API has no invite label or multi-use count (a code is single-use by
 * design, D135), so the dialog offers what it has: TTL and "re-key a node",
 * both in hive's own listbox (no native select).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { describeError } from "../../api/errors";
import { useNodes } from "../../api/hooks";
import { queryKeys } from "../../api/queryKeys";
import {
  INVITE_TTL_CHOICES,
  createInvite,
  revokeInvite,
  useInvites,
  type InviteCreated,
  type InviteOut,
} from "../../api/settings";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { CodeBlock } from "../../components/CodeBlock";
import { CopyField } from "../../components/CopyField";
import { Dialog } from "../../components/Dialog";
import { ListboxField, type ListboxOption } from "../../components/jobs/ListboxField";
import { QrCode } from "../../components/QrCode";
import { EmptyState, ErrorState } from "../../components/States";
import { formatDateTime, shortId } from "../../lib/format";
import { dash } from "../benchmarks/benchShared";
import { useConfirmAction } from "./ConfirmAction";
import { Field } from "./Field";
import { AddButton, Footnote, Intro, Loading, MaskedId, RowMenu, SettingsList, StateWord, When, type StColumn } from "./settingsUi";
import { useAction } from "./useAction";

const INVITE_TONES: Record<string, string> = { pending: "busy", used: "ok", revoked: "failed", expired: "unknown" };

function IssuedCode({ issued, onDone }: { issued: InviteCreated; onDone: () => void }) {
  return (
    <div className="flex flex-col gap-4" data-testid="issued-invite">
      <CopyField label="Join code - shown only now" value={issued.code} />
      <div className="flex flex-wrap items-start gap-4">
        <QrCode text={issued.code} label="QR code of the join code" size={208} className="rounded-md" />
        <dl className="st-issued grid min-w-0 flex-1 grid-cols-[7rem_1fr] items-baseline gap-x-3 gap-y-2.5 text-sm">
          <dt className="text-fg-muted">Invite</dt>
          <dd className="font-mono text-xs leading-5 break-all">{issued.invite_id}</dd>
          <dt className="text-fg-muted">Controller</dt>
          <dd className="font-mono text-xs leading-5 break-all">{issued.address}</dd>
          <dt className="text-fg-muted">CA pin</dt>
          <dd className="font-mono text-xs leading-5 break-all">{issued.ca_pin}</dd>
          <dt className="text-fg-muted">Expires</dt>
          <dd>{formatDateTime(issued.expires_at)}</dd>
          {issued.replace_node_id != null && (
            <>
              <dt className="text-fg-muted">Re-keys</dt>
              <dd className="font-mono text-xs leading-5 break-all">{issued.replace_node_id}</dd>
            </>
          )}
        </dl>
      </div>
      <div className="flex flex-col gap-1.5 text-sm text-fg-muted">
        <span>On the new laptop (with the Hive agent installed), run one of these and paste the code when asked:</span>
        <CodeBlock className="st-code" label="join commands" code={"meshforge-agent join\ncompute fabric join"} />
        <span>The code works once. The controller keeps only its hash; it cannot show it again.</span>
      </div>
      <div className="flex justify-end">
        <Button variant="primary" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

/** The form and its one-time answer; unmounted (and forgotten) when the dialog closes. */
function CreateInviteForm({ onDone, onBusy }: { onDone: () => void; onBusy: (busy: boolean) => void }) {
  const client = useQueryClient();
  const nodes = useNodes();
  const [ttl, setTtl] = useState("");
  const [replace, setReplace] = useState("");
  const create = useAction(createInvite);
  const form = useRef<HTMLFormElement>(null);

  useEffect(() => {
    // the first control (the expiry list's trigger) takes the focus
    form.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, []);
  useEffect(() => {
    onBusy(create.pending);
  }, [create.pending, onBusy]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const made = await create.run({
      ttlS: ttl === "" ? null : Number(ttl),
      replaceNodeId: replace === "" ? null : replace,
    });
    if (made !== undefined) void client.invalidateQueries({ queryKey: queryKeys.invites });
  };

  if (create.state.status === "done") return <IssuedCode issued={create.state.result} onDone={onDone} />;

  const replaceable = (nodes.data?.nodes ?? []).filter((n) => n.state !== "revoked");
  const ttlOptions: ListboxOption[] = INVITE_TTL_CHOICES.map((choice) => ({
    value: choice.seconds === null ? "" : String(choice.seconds),
    label: choice.label,
  }));
  const forOptions: ListboxOption[] = [
    { value: "", label: "A new laptop" },
    ...replaceable.map((n) => ({ value: n.node_id, label: `Re-key ${n.hostname}`, detail: shortId(n.node_id) })),
  ];
  const error = create.state.status === "error" ? create.state.error : null;
  return (
    <form
      ref={form}
      onSubmit={(e) => {
        void submit(e);
      }}
      className="flex flex-col gap-5"
    >
      <Field label="Code expires after">
        {(control) => <ListboxField id={control.id} value={ttl} options={ttlOptions} onChange={setTtl} />}
      </Field>
      <Field
        label="For"
        help="Re-keying gives an enrolled laptop a new certificate under the same node id (its old one stops working)."
      >
        {(control) => <ListboxField id={control.id} value={replace} options={forOptions} onChange={setReplace} />}
      </Field>
      {error !== null && (
        <p role="alert" className="text-sm text-status-failed">
          No invite made: {describeError(error).message}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button onClick={onDone} disabled={create.pending}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" loading={create.pending}>
          Create invite
        </Button>
      </div>
    </form>
  );
}

/** The section's primary action (admins): the dialog with the form and the one-time code. */
export function CreateInvite() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => {
    if (!busy) setOpen(false);
  };
  return (
    <>
      <AddButton
        label="Create invite"
        onClick={() => {
          setOpen(true);
        }}
      />
      <Dialog
        open={open}
        onClose={close}
        title="Invite a laptop"
        description="A single-use join code that lets one laptop enrol into this fabric."
        className="max-w-2xl"
      >
        <CreateInviteForm onDone={close} onBusy={setBusy} />
      </Dialog>
    </>
  );
}

/** The row's "⋯" menu: Revoke (pending codes only) behind its confirm dialog. */
function InviteActions({ invite: i }: { invite: InviteOut }) {
  const client = useQueryClient();
  const revoke = useConfirmAction({
    title: "Revoke this invite?",
    description: `The join code of ${i.id} stops working at once. A laptop that already joined with it is not affected.`,
    confirmLabel: "Revoke invite",
    action: () => revokeInvite(i.id),
    done: () => {
      void client.invalidateQueries({ queryKey: queryKeys.invites });
      return { title: `Invite ${i.id} revoked` };
    },
  });
  if (i.state !== "pending") return null;
  return (
    <>
      <RowMenu
        label={`Actions for invite ${i.id}`}
        items={[{ key: "revoke", label: "Revoke", name: `Revoke invite ${i.id}`, danger: true, onSelect: revoke.open }]}
      />
      {revoke.dialog}
    </>
  );
}

export function InvitesPage({ isAdmin }: { isAdmin: boolean }) {
  useDocumentTitle("Invites");
  const invites = useInvites(isAdmin);

  if (!isAdmin) {
    return (
      <EmptyState title="Invites are made by admins">
        Ask a fabric admin for a join code (they create it here or with <code className="font-mono">compute fabric invite</code>).
      </EmptyState>
    );
  }

  const columns: StColumn<InviteOut>[] = [
    { id: "id", header: "Invite", mono: true, grow: true, cell: (i) => <span className="text-fg"><MaskedId id={i.id} /></span> },
    { id: "state", header: "State", cell: (i) => <StateWord tone={INVITE_TONES[i.state] ?? "unknown"}>{i.state}</StateWord> },
    { id: "by", header: "Created by", cell: (i) => i.created_by },
    { id: "created", header: "Created", cell: (i) => <When iso={i.created_at} /> },
    { id: "expires", header: "Expires", cell: (i) => <When iso={i.expires_at} /> },
    {
      id: "used",
      header: "Used by",
      hint: "The node that joined with the code.",
      mono: true,
      cell: (i) =>
        i.used_by_node != null ? (
          <Link className="text-accent underline-offset-2 hover:underline" to={`/nodes/${encodeURIComponent(i.used_by_node)}`}>
            <MaskedId id={i.used_by_node} />
          </Link>
        ) : (
          dash
        ),
    },
    {
      id: "replaces",
      header: "Re-keys",
      hint: "An enrolled node the code gives a new certificate (same node id).",
      mono: true,
      cell: (i) => (i.replace_node_id != null ? <MaskedId id={i.replace_node_id} /> : dash),
    },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (i) => <InviteActions invite={i} /> },
  ];

  const rows = invites.data?.invites ?? [];
  const pending = rows.filter((i) => i.state === "pending").length;
  return (
    <>
      <Intro actions={<CreateInvite />}>
        Single-use join codes that let one laptop enrol into this fabric, newest first. A code is shown only once, when it is
        made.
      </Intro>
      {invites.isPending ? (
        <Loading />
      ) : invites.isError ? (
        <ErrorState
          title="Could not load the invites"
          error={invites.error}
          onRetry={() => {
            void invites.refetch();
          }}
        />
      ) : (
        <>
          <SettingsList
            caption="Invites, newest first (codes are never shown again)"
            columns={columns}
            rows={rows}
            rowKey={(i) => i.id}
            empty="No invites yet. Create one to add a laptop to the fabric."
          />
          <Footnote>{`${String(rows.length)} listed · ${String(pending)} pending. The controller keeps only a code's hash; it cannot show it again.`}</Footnote>
        </>
      )}
    </>
  );
}
