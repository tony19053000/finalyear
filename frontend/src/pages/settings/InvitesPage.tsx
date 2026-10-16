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
 * design, D135), so the dialog offers what it has: TTL and "re-key a node".
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
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { CopyField } from "../../components/CopyField";
import { Dialog } from "../../components/Dialog";
import { QrCode } from "../../components/QrCode";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatDateTime, shortId } from "../../lib/format";
import type { StatusTone } from "../../lib/status";
import { ConfirmAction } from "./ConfirmAction";
import { Field } from "./Field";
import { useAction } from "./useAction";

const INVITE_TONES: Record<string, StatusTone> = { pending: "busy", used: "ok", revoked: "failed", expired: "unknown" };

const select = "h-8 rounded-md border border-border-strong bg-surface px-2 text-sm text-fg";

function IssuedCode({ issued, onDone }: { issued: InviteCreated; onDone: () => void }) {
  return (
    <div className="flex flex-col gap-4" data-testid="issued-invite">
      <CopyField label="Join code - shown only now" value={issued.code} />
      <div className="flex flex-wrap items-start gap-4">
        <QrCode text={issued.code} label="QR code of the join code" size={208} className="rounded-md" />
        <dl className="grid min-w-0 flex-1 grid-cols-[7rem_1fr] gap-x-3 gap-y-1 text-sm">
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
        <span>On the new laptop (with the MeshForge agent installed), run one of these and paste the code when asked:</span>
        <CodeBlock label="join commands" code={"meshforge-agent join\ncompute fabric join"} />
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
  const first = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    first.current?.focus();
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
  const error = create.state.status === "error" ? create.state.error : null;
  return (
    <form
      onSubmit={(e) => {
        void submit(e);
      }}
      className="flex flex-col gap-3"
    >
      <Field label="Code expires after">
        {(control) => (
          <select
            {...control}
            ref={first}
            className={select}
            value={ttl}
            onChange={(e) => {
              setTtl(e.target.value);
            }}
          >
            {INVITE_TTL_CHOICES.map((choice) => (
              <option key={choice.label} value={choice.seconds === null ? "" : String(choice.seconds)}>
                {choice.label}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field
        label="For"
        help="Re-keying gives an enrolled laptop a new certificate under the same node id (its old one stops working)."
      >
        {(control) => (
          <select
            {...control}
            className={select}
            value={replace}
            onChange={(e) => {
              setReplace(e.target.value);
            }}
          >
            <option value="">A new laptop</option>
            {replaceable.map((n) => (
              <option key={n.node_id} value={n.node_id}>
                Re-key {n.hostname} ({shortId(n.node_id)})
              </option>
            ))}
          </select>
        )}
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

function CreateInvite() {
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
        Create invite
      </Button>
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

export function InvitesPage({ isAdmin }: { isAdmin: boolean }) {
  useDocumentTitle("Invites");
  const invites = useInvites(isAdmin);
  const client = useQueryClient();

  if (!isAdmin) {
    return (
      <EmptyState title="Invites are made by admins">
        Ask a fabric admin for a join code (they create it here or with <code className="font-mono">compute fabric invite</code>).
      </EmptyState>
    );
  }

  const columns: Column<InviteOut>[] = [
    { id: "id", header: "Invite", mono: true, cell: (i) => i.id },
    { id: "state", header: "State", cell: (i) => <Badge tone={INVITE_TONES[i.state] ?? "unknown"}>{i.state}</Badge> },
    { id: "by", header: "Created by", cell: (i) => i.created_by },
    { id: "created", header: "Created", cell: (i) => formatDateTime(i.created_at) },
    { id: "expires", header: "Expires", cell: (i) => formatDateTime(i.expires_at) },
    {
      id: "used",
      header: "Used by",
      cell: (i) =>
        i.used_by_node != null ? (
          <Link className="font-mono text-xs text-accent underline-offset-2 hover:underline" to={`/nodes/${encodeURIComponent(i.used_by_node)}`}>
            {i.used_by_node}
          </Link>
        ) : (
          "—"
        ),
    },
    { id: "replaces", header: "Re-keys", mono: true, cell: (i) => i.replace_node_id ?? "—" },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (i) =>
        i.state === "pending" ? (
          <ConfirmAction
            label="Revoke"
            triggerName={`Revoke invite ${i.id}`}
            title="Revoke this invite?"
            description={`The join code of ${i.id} stops working at once. A laptop that already joined with it is not affected.`}
            confirmLabel="Revoke invite"
            action={() => revokeInvite(i.id)}
            done={() => {
              void client.invalidateQueries({ queryKey: queryKeys.invites });
              return { title: `Invite ${i.id} revoked` };
            }}
          />
        ) : null,
    },
  ];

  return (
    <Card title="Invites" actions={<CreateInvite />} flush>
      {invites.isPending ? (
        <p role="status" className="p-4 text-sm text-fg-muted">
          Loading…
        </p>
      ) : invites.isError ? (
        <ErrorState
          className="m-4"
          title="Could not load the invites"
          error={invites.error}
          onRetry={() => {
            void invites.refetch();
          }}
        />
      ) : (
        <Table
          caption="Invites, newest first (codes are never shown again)"
          columns={columns}
          rows={invites.data.invites}
          rowKey={(i) => i.id}
          empty="No invites yet. Create one to add a laptop to the fabric."
        />
      )}
    </Card>
  );
}
