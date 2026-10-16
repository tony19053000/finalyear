/**
 * Settings > API tokens (D176): `GET /v1/auth/tokens` (admin: `?all=true`),
 * `DELETE /v1/auth/tokens/{id}`, and a new token through `POST
 * /v1/auth/login` - the call `compute login` makes; there is no other way to
 * mint one, so the password is asked again. Browser sessions (D170) are
 * listed here too and can be ended.
 *
 * A new token is shown ONCE, inside the dialog, and dropped when the dialog
 * closes: never in the query cache, storage or the URL (checklist 25).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { describeError } from "../../api/errors";
import { useSession } from "../../api/hooks";
import { queryKeys } from "../../api/queryKeys";
import {
  TOKEN_NAME_MAX_CHARS,
  createApiToken,
  revokeToken,
  useTokens,
  type TokenOut,
} from "../../api/settings";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { CopyField } from "../../components/CopyField";
import { Dialog } from "../../components/Dialog";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatDateTime } from "../../lib/format";
import type { StatusTone } from "../../lib/status";
import { ConfirmAction } from "./ConfirmAction";
import { Field } from "./Field";
import { useAction } from "./useAction";

const TOKEN_TONES: Record<string, StatusTone> = { active: "ok", expired: "unknown", revoked: "failed" };

function kindLabel(kind: string): string {
  if (kind === "api") return "API token";
  if (kind === "browser_session") return "Browser session";
  return kind;
}

function expiresText(token: TokenOut): string {
  // A browser session ends at whichever comes first: idle or absolute expiry.
  if (token.idle_expires_at != null && Date.parse(token.idle_expires_at) < Date.parse(token.expires_at)) {
    return `${formatDateTime(token.idle_expires_at)} (idle)`;
  }
  return formatDateTime(token.expires_at);
}

const input =
  "h-8 rounded-md border border-border-strong bg-surface px-2.5 text-sm text-fg aria-[invalid=true]:border-status-failed";

/** The form and its one-time answer; unmounted (and forgotten) when the dialog closes. */
function CreateTokenForm({ userName, onDone, onBusy }: { userName: string; onDone: () => void; onBusy: (busy: boolean) => void }) {
  const client = useQueryClient();
  const [tokenName, setTokenName] = useState("dashboard");
  const [password, setPassword] = useState("");
  const create = useAction(createApiToken);
  const first = useRef<HTMLInputElement>(null);
  const nameProblem =
    tokenName.trim() === ""
      ? "Give the token a name."
      : tokenName.length > TOKEN_NAME_MAX_CHARS
        ? `At most ${String(TOKEN_NAME_MAX_CHARS)} characters.`
        : null;

  useEffect(() => {
    first.current?.focus();
  }, []);
  useEffect(() => {
    onBusy(create.pending);
  }, [create.pending, onBusy]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (nameProblem !== null || password === "" || create.pending) return;
    const typed = password;
    setPassword(""); // never kept after the attempt
    const made = await create.run(userName, typed, tokenName.trim());
    if (made !== undefined) void client.invalidateQueries({ queryKey: queryKeys.tokensAll });
  };

  if (create.state.status === "done") {
    const made = create.state.result;
    return (
      <div className="flex flex-col gap-3" data-testid="new-token">
        <CopyField label="Your new API token - shown only now; copy it somewhere safe" value={made.token} />
        <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-fg-muted">Token id</dt>
          <dd className="font-mono text-xs leading-5">{made.token_id}</dd>
          <dt className="text-fg-muted">Expires</dt>
          <dd>{formatDateTime(made.expires_at)}</dd>
        </dl>
        <p className="text-sm text-fg-muted">
          Use it as <code className="font-mono">Authorization: Bearer …</code>, or save it to a file readable only by you
          and run <code className="font-mono">compute --token-file FILE …</code>. Revoke it here when it is no longer needed.
        </p>
        <div className="flex justify-end">
          <Button variant="primary" onClick={onDone}>
            Done
          </Button>
        </div>
      </div>
    );
  }

  const error = create.state.status === "error" ? create.state.error : null;
  return (
    <form
      onSubmit={(e) => {
        void submit(e);
      }}
      className="flex flex-col gap-3"
    >
      <Field label="Token name" help={nameProblem ?? "Shown in this list, e.g. the laptop or script that uses it."}>
        {(control) => (
          <input
            {...control}
            ref={first}
            className={input}
            value={tokenName}
            maxLength={TOKEN_NAME_MAX_CHARS + 1}
            autoComplete="off"
            aria-invalid={nameProblem !== null}
            onChange={(e) => {
              setTokenName(e.target.value);
            }}
          />
        )}
      </Field>
      <Field
        label={`Your password (${userName})`}
        help="Asked again because a token can act as you until it expires."
      >
        {(control) => (
          <input
            {...control}
            className={input}
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        )}
      </Field>
      {error !== null && (
        <p role="alert" className="text-sm text-status-failed">
          No token made: {describeError(error).message}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button onClick={onDone} disabled={create.pending}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" loading={create.pending} disabled={nameProblem !== null || password === ""}>
          Create token
        </Button>
      </div>
    </form>
  );
}

function CreateToken({ userName }: { userName: string }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => {
    if (!busy) setOpen(false); // a token being minted must not be lost unseen
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
        Create API token
      </Button>
      <Dialog
        open={open}
        onClose={close}
        title="Create an API token"
        description="For the CLI or a script. The token is shown once; the controller keeps only its hash."
      >
        <CreateTokenForm userName={userName} onDone={close} onBusy={setBusy} />
      </Dialog>
    </>
  );
}

export function TokensPage({ isAdmin }: { isAdmin: boolean }) {
  useDocumentTitle("API tokens");
  const [all, setAll] = useState(false);
  const showAll = isAdmin && all;
  const tokens = useTokens(showAll);
  const session = useSession();
  const client = useQueryClient();
  const userName = session.data?.user.name ?? "";

  const columns: Column<TokenOut>[] = [
    {
      id: "name",
      header: "Name",
      cell: (t) => (
        <span className="inline-flex items-center gap-2">
          {t.name}
          {t.current && <Badge tone="accent">this session</Badge>}
        </span>
      ),
    },
    { id: "kind", header: "Kind", cell: (t) => kindLabel(t.kind) },
    ...(showAll ? [{ id: "user", header: "User", cell: (t: TokenOut) => t.user }] : []),
    { id: "state", header: "State", cell: (t) => <Badge tone={TOKEN_TONES[t.state] ?? "unknown"}>{t.state}</Badge> },
    { id: "created", header: "Created", cell: (t) => formatDateTime(t.created_at) },
    { id: "used", header: "Last used", cell: (t) => formatDateTime(t.last_used_at) },
    { id: "expires", header: "Expires", cell: (t) => expiresText(t) },
    { id: "id", header: "Id", mono: true, cell: (t) => t.id },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "right",
      cell: (t) =>
        t.state === "active" ? (
          <ConfirmAction
            label="Revoke"
            triggerName={`Revoke ${t.name} (${t.id})`}
            title={t.current ? "End this browser session?" : `Revoke ${t.name}?`}
            description={
              t.current
                ? "This is the session you are using now: you will be signed out."
                : `${kindLabel(t.kind)} ${t.id} stops working at once (open log streams using it close too). There is no undo.`
            }
            confirmLabel={t.current ? "Sign out" : "Revoke"}
            action={() => revokeToken(t.id)}
            done={(result) => {
              void client.invalidateQueries({ queryKey: queryKeys.tokensAll });
              return {
                title: result.already_revoked ? `${t.name} was already revoked` : `${t.name} revoked`,
              };
            }}
          />
        ) : null,
    },
  ];

  return (
    <Card
      title="API tokens and sessions"
      actions={
        <>
          {isAdmin && (
            <label className="flex items-center gap-1.5 text-sm text-fg-muted">
              <input
                type="checkbox"
                checked={all}
                onChange={(e) => {
                  setAll(e.target.checked);
                }}
              />
              Every user
            </label>
          )}
          {userName !== "" && <CreateToken userName={userName} />}
        </>
      }
      flush
    >
      {tokens.isPending ? (
        <p role="status" className="p-4 text-sm text-fg-muted">
          Loading…
        </p>
      ) : tokens.isError ? (
        <ErrorState
          className="m-4"
          title="Could not load the tokens"
          error={tokens.error}
          onRetry={() => {
            void tokens.refetch();
          }}
        />
      ) : (
        <Table
          caption={showAll ? "Every user's API tokens and browser sessions" : "Your API tokens and browser sessions"}
          columns={columns}
          rows={tokens.data.tokens}
          rowKey={(t) => t.id}
          empty="No API tokens. Create one for the CLI, or run `compute login` on a laptop."
        />
      )}
    </Card>
  );
}
