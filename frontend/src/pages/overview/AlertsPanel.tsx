import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { AlertOut } from "../../api/fabric";
import { useAlerts } from "../../api/fabric";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { formatAgo, formatDateTime, secondsSince, shortId } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";

const KIND_TITLES: Record<string, string> = {
  wrong_node: "Status report from the wrong node",
};

function detail(alert: AlertOut, key: string): string | undefined {
  const value = alert.details[key];
  return typeof value === "string" ? value : undefined;
}

function NodeName({ id, names }: { id: string | undefined; names: Map<string, string> }) {
  if (id === undefined) return <>an unknown node</>;
  return (
    <Link to={`/nodes/${encodeURIComponent(id)}`} className="font-medium text-accent underline-offset-2 hover:underline" title={id}>
      {names.get(id) ?? shortId(id)}
    </Link>
  );
}

/** What happened, in words, from the alert's own fields only. */
function AlertText({ alert, names }: { alert: AlertOut; names: Map<string, string> }) {
  if (alert.kind === "wrong_node") {
    const attempt = detail(alert, "attempt_id");
    return (
      <>
        <NodeName id={alert.node_id ?? undefined} names={names} /> reported{" "}
        <span className="font-mono">{detail(alert, "reported") ?? "a state"}</span> for attempt{" "}
        <span className="font-mono" title={attempt}>{attempt === undefined ? "?" : shortId(attempt)}</span>, which runs on{" "}
        <NodeName id={detail(alert, "placed_node_id")} names={names} />. The report was refused; nothing changed.
      </>
    );
  }
  return <span className="font-mono">{JSON.stringify(alert.details)}</span>;
}

/**
 * Security refusals an operator should see (D180): `GET /v1/alerts`, newest
 * first, with live `alert` events merged in by id (events/applyEvent.ts).
 * Admins only - the API refuses everyone else, so a user sees why it is empty.
 */
export function AlertsPanel({ isAdmin, names }: { isAdmin: boolean; names: Map<string, string> }) {
  const alerts = useAlerts(isAdmin);
  const now = useServerNow();

  let body: ReactNode;
  if (!isAdmin) {
    body = <p className="text-sm text-fg-muted">Alerts are shown to admins only.</p>;
  } else if (alerts.isPending) {
    body = (
      <p role="status" className="text-sm text-fg-muted">
        Loading alerts…
      </p>
    );
  } else if (alerts.isError) {
    body = (
      <ErrorState
        title="Could not load alerts"
        error={alerts.error}
        onRetry={() => {
          void alerts.refetch();
        }}
      />
    );
  } else {
    const list = alerts.data.pages.flatMap((page) => page.alerts);
    body =
      list.length === 0 ? (
        <p className="text-sm text-fg-muted" data-testid="alerts-empty">
          No alerts. Security refusals - for example a node reporting status for work placed on another node - appear
          here as they happen.
        </p>
      ) : (
        <>
          <ul className="flex flex-col divide-y divide-border" data-testid="alerts-list">
            {list.map((alert) => (
              <li key={alert.id} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0" data-alert-id={alert.id}>
                <div className="flex items-center justify-between gap-3">
                  <span className="flex items-center gap-2 text-sm font-medium text-fg">
                    <Badge tone="failed">{alert.kind}</Badge>
                    {KIND_TITLES[alert.kind] ?? alert.kind}
                  </span>
                  <time dateTime={alert.at} title={formatDateTime(alert.at)} className="shrink-0 text-xs text-fg-muted">
                    {formatAgo(secondsSince(alert.at, now))}
                  </time>
                </div>
                <p className="text-sm text-fg-muted">
                  <AlertText alert={alert} names={names} />
                </p>
              </li>
            ))}
          </ul>
          {alerts.hasNextPage && (
            <div className="mt-3">
              <Button
                size="sm"
                loading={alerts.isFetchingNextPage}
                onClick={() => {
                  void alerts.fetchNextPage();
                }}
              >
                Show older alerts
              </Button>
            </div>
          )}
        </>
      );
  }
  return (
    <Card title="Alerts" data-testid="alerts-card">
      {body}
    </Card>
  );
}
