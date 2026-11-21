/**
 * Alerts: nodes offline / suspect (`GET /v1/nodes`), failed jobs among the
 * recent ones (`GET /v1/jobs`), and - for admins - the controller's security
 * refusals (`GET /v1/alerts`, live `alert` events merged by id). A calm
 * "All clear" when there is nothing.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { NodeSummary } from "../../api/endpoints";
import { useAlerts, type AlertOut } from "../../api/fabric";
import { Button } from "../../components/Button";
import { formatAgo, formatDateTime, secondsSince, shortId } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";
import type { JobSummary } from "../jobs/jobsApi";
import { OverviewCard } from "./OverviewCard";
import { fabricAlerts } from "./overviewModel";

const KIND_TITLES: Record<string, string> = {
  wrong_node: "Status report from the wrong node",
  clock_uncertain: "Node clock uncertain",
};

function detail(alert: AlertOut, key: string): string | undefined {
  const value = alert.details[key];
  return typeof value === "string" ? value : undefined;
}

function NodeName({ id, names }: { id: string | undefined; names: ReadonlyMap<string, string> }) {
  if (id === undefined) return <>an unknown node</>;
  return (
    <Link to={`/nodes/${encodeURIComponent(id)}`} className="font-medium text-accent underline-offset-2 hover:underline" title={id}>
      {names.get(id) ?? shortId(id)}
    </Link>
  );
}

/** A security alert in words, from its own fields only. */
function AlertText({ alert, names }: { alert: AlertOut; names: ReadonlyMap<string, string> }) {
  if (alert.kind === "wrong_node") {
    const attempt = detail(alert, "attempt_id");
    return (
      <>
        <NodeName id={alert.node_id ?? undefined} names={names} /> reported{" "}
        <span className="font-mono">{detail(alert, "reported") ?? "a state"}</span> for attempt{" "}
        <span className="font-mono" title={attempt}>
          {attempt === undefined ? "?" : shortId(attempt)}
        </span>
        , which runs on <NodeName id={detail(alert, "placed_node_id")} names={names} />. The report was refused; nothing
        changed.
      </>
    );
  }
  // Any other kind: its node and its detail fields, short, in the alert's own words.
  const parts = Object.entries(alert.details)
    .map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : typeof v === "object" && v !== null ? null : String(v)] as const)
    .filter((e): e is readonly [string, string] => e[1] !== null && e[1] !== "")
    .slice(0, 3);
  return (
    <>
      {alert.node_id != null && (
        <>
          <NodeName id={alert.node_id} names={names} />
          {parts.length > 0 ? " · " : ""}
        </>
      )}
      <span className="font-mono break-words">{parts.map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")}</span>
    </>
  );
}

function kindTitle(kind: string): string {
  const known = KIND_TITLES[kind];
  if (known !== undefined) return known;
  const words = kind.replace(/[_.]/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function Dot({ tone }: { tone: "failed" | "degraded" }) {
  return (
    <span
      aria-hidden="true"
      className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${tone === "failed" ? "bg-status-failed" : "bg-status-degraded"}`}
    />
  );
}

function Row({ tone, title, children, at, now, ...rest }: { tone: "failed" | "degraded"; title: ReactNode; children: ReactNode; at: string; now: number; [k: `data-${string}`]: string }) {
  return (
    <li className="flex gap-3 py-2 first:pt-0 last:pb-0" {...rest}>
      <Dot tone={tone} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className="truncate text-[0.8125rem] font-medium text-fg">{title}</span>
          <time dateTime={at} title={formatDateTime(at)} className="shrink-0 font-mono text-[0.6875rem] text-fg-subtle">
            {formatAgo(secondsSince(at, now))}
          </time>
        </div>
        <p className="truncate text-xs text-fg-muted">{children}</p>
      </div>
    </li>
  );
}

export interface AlertsCardProps {
  isAdmin: boolean;
  nodes: readonly NodeSummary[];
  jobs: readonly JobSummary[];
  names: ReadonlyMap<string, string>;
  /** Expanded: every alert (the Load card makes room). Collapsed: the first 3. */
  expanded?: boolean;
  onToggle?: () => void;
}

const COLLAPSED_ROWS = 3;

export function AlertsCard({ isAdmin, nodes, jobs, names, expanded = false, onToggle }: AlertsCardProps) {
  const security = useAlerts(isAdmin);
  const now = useServerNow();
  const fabric = fabricAlerts(nodes, jobs);
  const secList = isAdmin && security.isSuccess ? security.data.pages.flatMap((p) => p.alerts) : [];
  const total = fabric.length + secList.length;
  const loadingSecurity = isAdmin && security.isPending;

  return (
    <OverviewCard
      title="Alerts"
      id="ov-alerts"
      data-testid="alerts-card"
      className="min-h-0 flex-1"
      actions={
        total > 0 ? (
          <button
            type="button"
            className={`ov-icon-btn font-mono text-xs ${expanded ? "ov-icon-btn-honey" : "text-status-failed"}`}
            data-testid="alerts-count"
            aria-label={expanded ? `Show fewer alerts (${String(total)})` : `Show all ${String(total)} alerts`}
            aria-expanded={expanded}
            onClick={onToggle}
          >
            {expanded ? "×" : total}
          </button>
        ) : undefined
      }
    >
      {total === 0 ? (
        loadingSecurity ? (
          <p role="status" className="text-sm text-fg-muted">
            Loading alerts…
          </p>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 py-6 text-center" data-testid="alerts-empty">
            <span aria-hidden="true" className="h-2.5 w-2.5 rounded-full bg-status-ok" />
            <p className="text-sm font-medium text-fg">All clear</p>
            <p className="max-w-64 text-xs text-fg-muted">Every laptop is answering and no recent job failed.</p>
          </div>
        )
      ) : (
        <ul className={`ov-scroll -mr-2 flex min-h-0 flex-1 flex-col divide-y divide-[#2c2924] pr-2 ${expanded ? "ov-alerts-open overflow-y-auto" : "overflow-hidden"}`} data-testid="alerts-list">
          {[
            ...fabric.map((a) => (
              <Row key={a.id} tone={a.tone} at={a.at} now={now} data-fabric-alert={a.id} title={<Link to={a.href} className="hover:underline">{a.title}</Link>}>
                {a.detail}
              </Row>
            )),
            ...secList.map((alert) => (
              <Row key={alert.id} tone="failed" at={alert.at} now={now} data-alert-id={alert.id} title={kindTitle(alert.kind)}>
                <AlertText alert={alert} names={names} />
              </Row>
            )),
          ].slice(0, expanded ? undefined : COLLAPSED_ROWS)}
        </ul>
      )}
      {isAdmin && expanded && security.hasNextPage && (
        <div className="mt-3">
          <Button
            size="sm"
            loading={security.isFetchingNextPage}
            onClick={() => {
              void security.fetchNextPage();
            }}
          >
            Show older alerts
          </Button>
        </div>
      )}
      {isAdmin && security.isError && (
        <p className="mt-3 text-xs text-status-failed">
          Could not load security alerts.{" "}
          <button
            type="button"
            className="underline"
            onClick={() => {
              void security.refetch();
            }}
          >
            Retry
          </button>
        </p>
      )}
      {!isAdmin && <p className="mt-3 text-[0.6875rem] text-fg-subtle">Security alerts are shown to admins only.</p>}
    </OverviewCard>
  );
}
