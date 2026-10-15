import { useSession, useWhoAmI } from "../api/hooks";
import { useDocumentTitle } from "../app/useDocumentTitle";
import { Card } from "../components/Card";
import { EmptyState, ErrorState } from "../components/States";
import { PageHeader } from "./PageHeader";

function formatTime(iso: string | null | undefined): string {
  if (iso == null) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-3 py-1.5">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className={mono ? "font-mono text-xs leading-5 text-fg" : "text-sm text-fg"}>{value}</dd>
    </div>
  );
}

/** Who am I (`GET /v1/auth/whoami`) and this browser session (`GET /v1/session`). */
function AccountCard() {
  const whoami = useWhoAmI();
  const session = useSession();
  return (
    <Card title="Your account" data-testid="account-card">
      {whoami.isPending ? (
        <p role="status" className="text-sm text-fg-muted">
          Loading…
        </p>
      ) : whoami.isError ? (
        <ErrorState
          title="Could not load your account"
          error={whoami.error}
          onRetry={() => {
            void whoami.refetch();
          }}
        />
      ) : (
        <dl>
          <Row label="User" value={whoami.data.user.name} />
          <Row label="Role" value={whoami.data.user.role} />
          <Row label="Session id" value={whoami.data.token_id ?? "—"} mono />
          <Row label="Session ends" value={formatTime(session.data?.expires_at)} />
          <Row label="Idle timeout" value={formatTime(session.data?.idle_expires_at)} />
        </dl>
      )}
    </Card>
  );
}

export function SettingsPage() {
  useDocumentTitle("Settings");
  return (
    <>
      <PageHeader title="Settings" description="Your account, API tokens, invites, secrets and data." />
      <div className="flex flex-col gap-5">
        <AccountCard />
        <EmptyState title="Fabric settings are not built yet">
          API tokens, invites, users, secrets and data sets will be managed here. From a terminal:{" "}
          <code className="font-mono">compute tokens list</code>, <code className="font-mono">compute secret list</code>.
        </EmptyState>
      </div>
    </>
  );
}
