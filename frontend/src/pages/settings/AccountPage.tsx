/**
 * Settings > Account (D176 "whoami", "Fabric: controller info"): who I am
 * (`GET /v1/auth/whoami`), this browser session (`GET /v1/session`), the
 * controller (`GET /healthz`) and where owner policy lives (agent-local,
 * D142 - read-only here). Everything shown comes from those endpoints.
 *
 * Layout: classic settings sections - a left column with the section's
 * title and one line of description, the facts on the right as label/value
 * rows, hairlines between sections. No figures, no cards.
 */
import { Link } from "react-router-dom";

import { useHealth } from "../../api/fabric";
import { useSession, useWhoAmI } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { CodeBlock } from "../../components/CodeBlock";
import { ErrorState } from "../../components/States";
import { formatDateTime } from "../../lib/format";
import { FactRow, Facts, Intro, Loading, Section, StateWord } from "./settingsUi";

/** Who am I (`GET /v1/auth/whoami`) and this browser session (`GET /v1/session`). */
function AccountSection() {
  const whoami = useWhoAmI();
  const session = useSession();
  return (
    <Section title="Your account" description="Who this dashboard is signed in as, and this browser session." testId="account-card">
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
        <>
          <Facts>
            <FactRow label="User">
              <span className="font-medium">{whoami.data.user.name}</span>
            </FactRow>
            <FactRow label="Role">
              <span className="capitalize">{whoami.data.user.role}</span>
            </FactRow>
            <FactRow label="Session id">
              <span className="font-mono text-[0.8125rem] break-all text-fg-muted">{whoami.data.token_id ?? "—"}</span>
            </FactRow>
            <FactRow label="Session ends">{formatDateTime(session.data?.expires_at)}</FactRow>
            <FactRow label="Idle timeout" hint="The session also ends after this long without use.">
              {formatDateTime(session.data?.idle_expires_at)}
            </FactRow>
          </Facts>
          <p className="st-foot">
            This browser session is listed with your{" "}
            <Link className="text-accent underline-offset-2 hover:underline" to="/settings/tokens">
              API tokens
            </Link>{" "}
            and can be ended there or with Sign out.
          </p>
        </>
      )}
    </Section>
  );
}

/** The controller this dashboard talks to (`GET /healthz` + the page's own address). */
function ControllerSection() {
  const health = useHealth();
  return (
    <Section
      title="Controller"
      description="The controller this dashboard talks to, as it reports itself."
      hint="GET /healthz, and the address this page was loaded from."
      testId="controller-card"
    >
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
        <Facts>
          <FactRow label="Status">
            <StateWord tone={health.data.status === "ok" ? "ok" : "degraded"}>{health.data.status}</StateWord>
          </FactRow>
          <FactRow label="Address">
            <span className="font-mono text-[0.8125rem] break-all">{window.location.origin}</span>
          </FactRow>
          <FactRow label="Agent sessions" hint="Agents connected to the controller right now.">
            <span className="tabular-nums">{String(health.data.sessions)}</span>
          </FactRow>
          {health.data.degraded != null && health.data.degraded.length > 0 && (
            <FactRow label="Degraded loops">
              <span className="font-mono text-[0.8125rem]">{health.data.degraded.join(", ")}</span>
            </FactRow>
          )}
        </Facts>
      )}
    </Section>
  );
}

const POLICY_HINT =
  "Each laptop's owner decides what the fabric may do there: whether it accepts jobs, how much CPU, memory and VRAM one job may use, who may run jobs, network access. The policy lives on that laptop (D142), so the dashboard shows it read-only on each node's page (Caches & policy) and cannot change it.";

/** D142: owner policy is set on each laptop by its owner, never from here. */
function OwnerPolicySection() {
  return (
    <Section title="Owner policy" description="Set on each laptop by its owner; read-only here." hint={POLICY_HINT} testId="owner-policy-card">
      <p className="st-prose cursor-help" title={POLICY_HINT}>
        Whether a laptop accepts jobs, and how much of it one job may use, is decided on that laptop. The dashboard shows it on
        each node&apos;s page and cannot change it.
      </p>
      <CodeBlock className="st-code mt-4" label="owner policy command" code={"meshforge-agent policy show\nmeshforge-agent policy set KEY=VALUE"} />
    </Section>
  );
}

export function AccountPage() {
  useDocumentTitle("Settings");
  return (
    <>
      <Intro>Your account and this browser session, the controller this dashboard talks to, and where owner policy lives.</Intro>
      <AccountSection />
      <ControllerSection />
      <OwnerPolicySection />
    </>
  );
}
