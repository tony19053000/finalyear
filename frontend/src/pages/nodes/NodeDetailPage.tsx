/**
 * Node detail (0.8.4): `GET /v1/nodes/{id}` (live through node.status /
 * node.metrics events), the live load per resource, hardware + disks +
 * scores, network links, caches, the owner policy (read-only, D142) and -
 * for an admin - Revoke.
 *
 * Layout (frame D ops console in the frame B language): the name with its
 * status light, an open facts strip, frame-B text tabs (a real ARIA tablist:
 * arrow keys / Home / End move between them; the open tab is kept in
 * `?tab=` so a shared link opens it).
 */
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api/errors";
import { useNode } from "../../api/fabric";
import { useNodes, useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { EmptyState, ErrorState } from "../../components/States";
import { cn } from "../../lib/cn";
import { formatAgo, formatDateTime, formatMs, secondsSince } from "../../lib/format";
import { useServerNow } from "../../lib/serverClock";
import { PageHeader } from "../PageHeader";
import { AgentVersion } from "./AgentVersion";
import { LiveMetrics } from "./LiveMetrics";
import { NodeCaches, NodePolicy } from "./NodeCachesPolicy";
import { NodeHardware } from "./NodeHardware";
import { NodeNetwork } from "./NodeNetwork";
import { isSilent, STATE_LIGHT } from "./nodeLight";
import { NodeStatus } from "./NodeStatus";
import { RevokeNode } from "./RevokeNode";

const TABS = [
  { value: "live", label: "Live" },
  { value: "hardware", label: "Hardware" },
  { value: "network", label: "Network" },
  { value: "caches", label: "Caches & policy" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

function Fact({ label, children, testId, className }: { label: string; children: ReactNode; testId?: string; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="nd-label">{label}</dt>
      <dd className="mt-2.5 text-[0.9375rem] leading-6 break-words text-fg" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

const unknown = <span className="text-fg-subtle">not reported</span>;

/**
 * Pinned name block: see-through at rest (the comb shows behind the name);
 * once it sticks to the top while scrolling it gets a frosted band so the
 * content sliding under it stays readable.
 */
function StickyHead({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(false);
  useEffect(() => {
    const onScroll = () => {
      const el = ref.current;
      if (el !== null) setStuck(el.getBoundingClientRect().top <= 0.5 && window.scrollY > 0);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
    };
  }, []);
  return (
    <div ref={ref} className={cn("nd-sticky-head", stuck && "nd-sticky-head-stuck")}>
      {children}
    </div>
  );
}

function BackLink() {
  return (
    <Link to="/nodes" className="text-sm text-fg-muted transition-colors duration-300 hover:text-accent">
      ← All nodes
    </Link>
  );
}

/** Frame-B text tabs: plain text, the open one white and bold over a short underline. */
function DetailTabs({ value, onChange }: { value: TabValue; onChange: (v: TabValue) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.value === value);
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = TABS.length - 1;
    const tab = TABS[next];
    if (tab === undefined) return;
    e.preventDefault();
    onChange(tab.value);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label="Node details" className="flex flex-wrap items-end gap-x-10 gap-y-3" onKeyDown={onKeyDown}>
      {TABS.map((t, k) => {
        const on = t.value === value;
        return (
          <button
            key={t.value}
            ref={(el) => {
              refs.current[k] = el;
            }}
            type="button"
            role="tab"
            id={`node-tab-${t.value}`}
            aria-selected={on}
            aria-controls={`node-panel-${t.value}`}
            tabIndex={on ? 0 : -1}
            onClick={() => {
              onChange(t.value);
            }}
            className={`relative pb-2 text-[0.9375rem] transition-colors duration-300 ${on ? "font-semibold text-fg" : "text-fg-muted hover:text-fg"}`}
          >
            {t.label}
            <span
              aria-hidden="true"
              className={`absolute bottom-0 left-0 h-0.5 rounded-full bg-fg transition-all duration-300 ${on ? "w-5 opacity-100" : "w-0 opacity-0"}`}
            />
          </button>
        );
      })}
    </div>
  );
}

export function NodeDetailPage() {
  const { nodeId = "" } = useParams();
  const node = useNode(nodeId);
  const nodes = useNodes();
  const session = useSession();
  const now = useServerNow();
  const [params, setParams] = useSearchParams();
  useDocumentTitle(node.data?.hostname ?? "Node");
  const isAdmin = session.data?.user.role === "admin";
  const tabParam = params.get("tab");
  const tab: TabValue = TABS.some((t) => t.value === tabParam) ? (tabParam as TabValue) : "live";
  const setTab = (v: TabValue) => {
    const next = new URLSearchParams(params);
    if (v === "live") next.delete("tab");
    else next.set("tab", v);
    setParams(next, { replace: true });
  };

  if (node.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the node…
      </p>
    );
  }
  if (node.isError) {
    const missing = node.error instanceof ApiError && (node.error.status === 404 || node.error.status === 400);
    return (
      <>
        <PageHeader title="Node" />
        {missing ? (
          <EmptyState title="No such node" action={<BackLink />}>
            The fabric has no node <span className="font-mono">{nodeId}</span>.
          </EmptyState>
        ) : (
          <ErrorState
            title="Could not load the node"
            error={node.error}
            onRetry={() => {
              void node.refetch();
            }}
          />
        )}
      </>
    );
  }

  const n = node.data;
  const others = nodes.data?.nodes ?? [];
  const title = n.display_name ? n.display_name : n.hostname;
  const subtitle = n.display_name ? `${n.hostname} · ${n.os} · ${n.platform}` : `${n.os} · ${n.platform}`;
  const stateTitle = isSilent(n) ? `${n.state} · no live stream` : n.state;

  let panel: ReactNode;
  if (tab === "live") panel = <LiveMetrics nodeId={n.node_id} />;
  else if (tab === "hardware") panel = <NodeHardware node={n} />;
  else if (tab === "network") panel = <NodeNetwork nodeId={n.node_id} nodes={others} />;
  else
    panel = (
      <div className="nd-equal grid grid-cols-1 items-stretch gap-5 xl:grid-cols-2">
        <NodeCaches node={n} />
        <NodePolicy node={n} />
      </div>
    );

  return (
    <>
      {/* the name stays pinned at the top; the facts and tabs scroll up beneath it */}
      <StickyHead>
      <div className="pt-3">
        <BackLink />
      </div>
      <header className="mt-5 flex flex-wrap items-end justify-between gap-6 pb-6">
        <div className="min-w-0">
          <div className="flex items-center gap-4">
            <h1 className="min-w-0 truncate font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">{title}</h1>
            <span
              aria-hidden="true"
              title={stateTitle}
              className={`nd-light mt-2 ${STATE_LIGHT[n.state] ?? "bg-status-unknown"}`}
              data-testid="title-light"
            />
          </div>
          <p className="mt-2.5 text-sm text-fg-muted">{subtitle}</p>
        </div>
        {isAdmin && n.state !== "revoked" && <RevokeNode nodeId={n.node_id} hostname={n.hostname} />}
      </header>
      </StickyHead>

      <dl className="nd-hair-y nd-stagger grid grid-cols-2 gap-x-10 gap-y-8 py-8 sm:grid-cols-3 lg:grid-cols-5" data-testid="summary-card">
        <Fact label="Status" testId="node-status">
          <NodeStatus node={n} />
        </Fact>
        <Fact label="Last heartbeat" testId="node-heartbeat">
          <time dateTime={n.last_seen_at} title={formatDateTime(n.last_seen_at)}>
            {formatAgo(secondsSince(n.last_seen_at, now))}
          </time>
        </Fact>
        <Fact label="Agent version">{n.agent_version == null ? unknown : <AgentVersion node={n} />}</Fact>
        <Fact label="Round trip">
          {n.rtt_ms == null ? (
            unknown
          ) : (
            <>
              <span className="tabular-nums">{formatMs(n.rtt_ms)}</span>
              <span className="ml-2 text-xs text-fg-muted">avg {formatMs(n.rtt_ewma_ms)}</span>
            </>
          )}
        </Fact>
        <Fact label="Node id">
          <span className="block truncate font-mono text-[0.8125rem]" title={n.node_id}>
            {n.node_id}
          </span>
        </Fact>
        <Fact label="First seen">{formatDateTime(n.first_seen_at)}</Fact>
        <Fact label="Address">
          {n.peer_address == null ? unknown : <span className="font-mono text-[0.8125rem]">{n.peer_address}</span>}
        </Fact>
        <Fact label="Certificate" className="sm:col-span-2">
          {n.certificate ? (
            <span className="flex flex-col">
              <span className="truncate font-mono text-[0.8125rem]" title={n.certificate.serial}>
                {n.certificate.serial}
              </span>
              <span className="text-xs text-fg-muted">valid until {formatDateTime(n.certificate.not_after)}</span>
            </span>
          ) : (
            <span className="text-fg-muted">never enrolled</span>
          )}
        </Fact>
        <Fact label="Protocol">{n.protocol_version == null ? unknown : <span className="tabular-nums">v{n.protocol_version}</span>}</Fact>
      </dl>

      <div className="mt-10 mb-8">
        <DetailTabs value={tab} onChange={setTab} />
      </div>
      <div role="tabpanel" id={`node-panel-${tab}`} aria-labelledby={`node-tab-${tab}`} key={tab} className="nd-in pb-6">
        {panel}
      </div>
    </>
  );
}
