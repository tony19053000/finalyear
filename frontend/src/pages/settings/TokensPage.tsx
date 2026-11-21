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
import { CheckIcon } from "../../app/icons";
import { Button } from "../../components/Button";
import { CopyField } from "../../components/CopyField";
import { Dialog } from "../../components/Dialog";
import { ErrorState } from "../../components/States";
import { cn } from "../../lib/cn";
import { formatDateTime } from "../../lib/format";
import { useConfirmAction } from "./ConfirmAction";
import { FIELD_CLASS, Field } from "./Field";
import { AddButton, Footnote, Intro, Loading, MaskedId, RowMenu, SettingsList, StateWord, When, type StColumn } from "./settingsUi";
import { useAction } from "./useAction";

const TOKEN_TONES: Record<string, string> = { active: "ok", expired: "unknown", revoked: "failed" };

function kindLabel(kind: string): string {
  if (kind === "api") return "API token";
  if (kind === "browser_session") return "Browser session";
  return kind;
}

function Expires({ token }: { token: TokenOut }) {
  // A browser session ends at whichever comes first: idle or absolute expiry.
  if (token.idle_expires_at != null && Date.parse(token.idle_expires_at) < Date.parse(token.expires_at)) {
    return <When iso={token.idle_expires_at} suffix="(idle)" />;
  }
  return <When iso={token.expires_at} />;
}

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
        <dl className="grid grid-cols-[8rem_1fr] items-baseline gap-x-3 gap-y-2 text-sm">
          <dt className="nd-label">Token id</dt>
          <dd className="font-mono text-xs leading-5">{made.token_id}</dd>
          <dt className="nd-label">Expires</dt>
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
      className="flex flex-col gap-5"
    >
      <Field label="Token name" help={nameProblem ?? "Shown in this list, e.g. the laptop or script that uses it."}>
        {(control) => (
          <input
            {...control}
            ref={first}
            className={FIELD_CLASS}
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
            className={FIELD_CLASS}
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

/** The section's primary action: the dialog with the form and its one-time answer. */
export function CreateToken() {
  const session = useSession();
  const userName = session.data?.user.name ?? "";
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const close = () => {
    if (!busy) setOpen(false); // a token being minted must not be lost unseen
  };
  if (userName === "") return null;
  return (
    <>
      <AddButton
        label="Create API token"
        onClick={() => {
          setOpen(true);
        }}
      />
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

/** A quiet checkbox (Planner's toggle): a small square that fills honey, the words beside it. */
function Toggle({ on, onChange, children }: { on: boolean; onChange: (on: boolean) => void; children: string }) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={on}
      className="pl-toggle"
      onClick={() => {
        onChange(!on);
      }}
    >
      <span aria-hidden="true" className={cn("pl-toggle-box", on && "pl-toggle-on")}>
        {on && <CheckIcon />}
      </span>
      <span className="whitespace-nowrap">{children}</span>
    </button>
  );
}

/** The row's "⋯" menu: Revoke (or Sign out for this session) behind its confirm dialog. */
function TokenActions({ token: t }: { token: TokenOut }) {
  const client = useQueryClient();
  const revoke = useConfirmAction({
    title: t.current ? "End this browser session?" : `Revoke ${t.name}?`,
    description: t.current
      ? "This is the session you are using now: you will be signed out."
      : `${kindLabel(t.kind)} ${t.id} stops working at once (open log streams using it close too). There is no undo.`,
    confirmLabel: t.current ? "Sign out" : "Revoke",
    action: () => revokeToken(t.id),
    done: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.tokensAll });
      return {
        title: result.already_revoked ? `${t.name} was already revoked` : `${t.name} revoked`,
      };
    },
  });
  if (t.state !== "active") return null;
  return (
    <>
      <RowMenu
        label={`Actions for ${t.name} (${t.id})`}
        items={[{ key: "revoke", label: "Revoke", name: `Revoke ${t.name} (${t.id})`, danger: true, onSelect: revoke.open }]}
      />
      {revoke.dialog}
    </>
  );
}

export function TokensPage({ isAdmin }: { isAdmin: boolean }) {
  useDocumentTitle("API tokens");
  const [all, setAll] = useState(false);
  const showAll = isAdmin && all;
  const tokens = useTokens(showAll);

  const columns: StColumn<TokenOut>[] = [
    {
      id: "name",
      header: "Name",
      grow: true,
      cell: (t) => (
        <span className="inline-flex min-w-0 items-center gap-2.5">
          <span className="font-medium text-fg">{t.name}</span>
          {t.current && <span className="st-tag">this session</span>}
        </span>
      ),
    },
    { id: "kind", header: "Kind", cell: (t) => kindLabel(t.kind) },
    ...(showAll ? [{ id: "user", header: "User", cell: (t: TokenOut) => t.user }] : []),
    { id: "state", header: "State", cell: (t) => <StateWord tone={TOKEN_TONES[t.state] ?? "unknown"}>{t.state}</StateWord> },
    { id: "created", header: "Created", cell: (t) => <When iso={t.created_at} /> },
    { id: "used", header: "Last used", cell: (t) => <When iso={t.last_used_at} /> },
    {
      id: "expires",
      header: "Expires",
      hint: "A browser session ends at whichever comes first: its idle timeout (idle) or its absolute expiry.",
      cell: (t) => <Expires token={t} />,
    },
    { id: "id", header: "Id", mono: true, cell: (t) => <MaskedId id={t.id} /> },
    { id: "actions", header: <span className="sr-only">Actions</span>, align: "right", cell: (t) => <TokenActions token={t} /> },
  ];

  const rows = tokens.data?.tokens ?? [];
  const active = rows.filter((t) => t.state === "active").length;
  return (
    <>
      <Intro
        actions={
          <>
            {isAdmin && (
              <Toggle on={all} onChange={setAll}>
                Every user
              </Toggle>
            )}
            <CreateToken />
          </>
        }
      >
        API tokens let the CLI or a script act as you; browser sessions are your dashboard sign-ins. Revoking either stops it at
        once.
      </Intro>
      {tokens.isPending ? (
        <Loading />
      ) : tokens.isError ? (
        <ErrorState
          title="Could not load the tokens"
          error={tokens.error}
          onRetry={() => {
            void tokens.refetch();
          }}
        />
      ) : (
        <>
          <SettingsList
            caption={showAll ? "Every user's API tokens and browser sessions" : "Your API tokens and browser sessions"}
            columns={columns}
            rows={rows}
            rowKey={(t) => t.id}
            empty="No API tokens. Create one for the CLI, or run `compute login` on a laptop."
          />
          <Footnote title="A browser session ends at whichever comes first: its idle timeout or its absolute expiry. Expired and revoked tokens stay listed for a while.">
            {`${String(rows.length)} listed · ${String(active)} active. The controller keeps only a token's hash.`}
          </Footnote>
        </>
      )}
    </>
  );
}
