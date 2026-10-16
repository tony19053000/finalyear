/**
 * Settings > Account (D176 "whoami", "Fabric: controller info"): who I am
 * (`GET /v1/auth/whoami`), this browser session (`GET /v1/session`), the
 * controller (`GET /healthz`) and where owner policy lives (agent-local,
 * D142 - read-only here). Everything shown comes from those endpoints.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useHealth } from "../../api/fabric";
import { useSession, useWhoAmI } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { ErrorState } from "../../components/States";
import { StatusDot } from "../../components/Status";
import { formatDateTime } from "../../lib/format";

function Row({ label, value, mono = false }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-3 py-1.5">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className={mono ? "font-mono text-xs leading-5 text-fg" : "text-sm text-fg"}>{value}</dd>
    </div>
  );
}

function Loading() {
  return (
    <p role="status" className="text-sm text-fg-muted">
      Loading…
    </p>
  );
}

/** Who am I (`GET /v1/auth/whoami`) and this browser session (`GET /v1/session`). */
function AccountCard() {
  const whoami = useWhoAmI();
  const session = useSession();
  return (
    <Card title="Your account" data-testid="account-card">
      {whoami.isPending ? (
        <Loading />
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
          <Row label="Session ends" value={formatDateTime(session.data?.expires_at)} />
          <Row label="Idle timeout" value={formatDateTime(session.data?.idle_expires_at)} />
        </dl>
      )}
      <p className="mt-3 text-xs text-fg-muted">
        This browser session is listed with your API tokens and can be ended there or with Sign out.{" "}
        <Link className="text-accent underline underline-offset-2" to="/settings/tokens">
          API tokens
        </Link>
      </p>
    </Card>
  );
}

/** The controller this dashboard talks to (`GET /healthz` + the page's own address). */
function ControllerCard() {
  const health = useHealth();
  return (
    <Card title="Controller" data-testid="controller-card">
      {health.isPending ? (
        <Loading />
      ) : health.isError ? (
        <ErrorState
          title="Could not reach the controller"
          error={health.error}
          onRetry={() => {
            void health.refetch();
          }}
        />
      ) : (
        <dl>
          <Row label="Address" value={window.location.origin} mono />
          <Row
            label="Status"
            value={
              <StatusDot
                tone={health.data.status === "ok" ? "ok" : "degraded"}
                label={health.data.status}
              />
            }
          />
          <Row label="Agent sessions" value={String(health.data.sessions)} />
          {health.data.degraded != null && health.data.degraded.length > 0 && (
            <Row label="Degraded loops" value={health.data.degraded.join(", ")} mono />
          )}
        </dl>
      )}
    </Card>
  );
}

/** D142: owner policy is set on each laptop by its owner, never from here. */
function OwnerPolicyCard() {
  return (
    <Card title="Owner policy" data-testid="owner-policy-card">
      <p className="text-sm text-fg-muted">
        Each laptop&apos;s owner decides what the fabric may do there: whether it accepts jobs, how much CPU, memory and
        VRAM one job may use, who may run jobs, network access. The policy lives on that laptop (D142), so the dashboard
        shows it read-only on each node&apos;s page (Caches &amp; policy) and cannot change it. On the laptop:
      </p>
      <CodeBlock className="mt-3" label="owner policy command" code={"meshforge-agent policy show\nmeshforge-agent policy set KEY=VALUE"} />
    </Card>
  );
}

export function AccountPage() {
  useDocumentTitle("Settings");
  return (
    <div className="flex flex-col gap-5">
      <AccountCard />
      <ControllerCard />
      <OwnerPolicyCard />
    </div>
  );
}
