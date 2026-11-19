/**
 * Caches (`cache` of `GET /v1/nodes/{id}`: count + bytes per kind, from the
 * node's cache reports, D101) and the owner policy (D142). The policy is set
 * on the laptop itself (`meshforge-agent policy set`) - agent-local by design
 * - so the dashboard shows it read-only.
 */
import type { NodeDetail } from "../../api/fabric";
import { Table } from "../../components/Table";
import { formatBytes } from "../../lib/format";
import { FrostTable } from "./FrostTable";

const CACHE_LABELS: Record<string, string> = {
  blob: "Blobs (content store)",
  tree: "Project trees",
  env: "Python environments",
  image: "Container images",
};

/** Plain names for the owner-policy settings (the raw key stays in the row's title). */
const POLICY_LABELS: Record<string, string> = {
  accepting: "Accepting jobs",
  accept_jobs: "Accepting jobs",
  max_cpu_percent: "CPU limit",
  max_ram_bytes: "RAM limit",
  max_gpu_vram_bytes: "VRAM limit",
  max_gpu_jobs: "GPU jobs limit",
  accept_gpu_sessions: "GPU sessions",
  only_when_plugged_in: "Only when plugged in",
  quiet_hours: "Quiet hours",
  allowed_users_count: "Allowed users",
};

function policyLabel(key: string): string {
  return POLICY_LABELS[key] ?? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** A policy value as text; `*_bytes` keys read as sizes (the raw number stays in the title). */
function policyValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number" && key.endsWith("_bytes")) return formatBytes(value);
  if (typeof value === "number" && key.endsWith("_percent")) return `${String(value)}%`;
  if (typeof value === "string" || typeof value === "number") return String(value);
  return JSON.stringify(value);
}

export function NodeCaches({ node }: { node: NodeDetail }) {
  const kinds = Object.entries(node.cache ?? {});
  const totalBytes = kinds.reduce((a, [, c]) => a + c.bytes, 0);
  const totalItems = kinds.reduce((a, [, c]) => a + c.count, 0);
  return (
    <FrostTable
      title="Caches"
      testId="caches-card"
      className="nd-cache"
      meta={
        kinds.length > 0 ? (
          <span className="font-mono text-[0.6875rem] tracking-[0.12em] text-fg-subtle uppercase tabular-nums">
            {formatBytes(totalBytes)} · {totalItems} items
          </span>
        ) : undefined
      }
    >
      {kinds.length === 0 ? (
        <p className="px-9 pt-2 pb-5 text-sm text-fg-muted">Not reported - the node has not sent a cache report yet.</p>
      ) : (
        <Table
          caption="What the node caches, per kind"
          rows={kinds}
          rowKey={([kind]) => kind}
          columns={[
            { id: "kind", header: "Kind", cell: ([kind]) => CACHE_LABELS[kind] ?? kind },
            { id: "count", header: "Items", align: "right", mono: true, cell: ([, c]) => c.count },
            { id: "bytes", header: "Size", align: "right", mono: true, cell: ([, c]) => formatBytes(c.bytes) },
          ]}
        />
      )}
    </FrostTable>
  );
}

export function NodePolicy({ node }: { node: NodeDetail }) {
  const policy = node.owner_policy;
  // "max_gpu_vram_bytes:no_cgroup_on_windows" -> the key it is about
  const notEnforcedKeys = new Set((node.not_enforced ?? []).map((x) => x.split(":")[0] ?? x));
  const entries = policy == null ? [] : Object.entries(policy);
  return (
    <FrostTable
      title="Owner policy"
      testId="policy-card"
      className="nd-policy"
      meta={<span className="font-mono text-[0.6875rem] tracking-[0.12em] text-fg-subtle uppercase">read-only</span>}
    >
      {policy == null ? (
        <p className="px-9 pt-2 pb-5 text-sm text-fg-muted">Not reported - the node is offline, or its agent is older than V0.7 G5.</p>
      ) : (
        <Table
          caption="The owner policy of the node's live session"
          rows={entries}
          rowKey={([key]) => key}
          columns={[
            {
              id: "key",
              header: "Setting",
              cell: ([key]) => (
                <span className="text-fg" title={key}>
                  {policyLabel(key)}
                </span>
              ),
            },
            {
              id: "value",
              header: "Value",
              align: "right",
              mono: true,
              cell: ([key, value]) => (
                <span title={typeof value === "number" ? String(value) : undefined}>
                  {policyValue(key, value)}
                  {notEnforcedKeys.has(key) && <span className="ml-2 text-[0.6875rem] text-status-degraded">not enforced</span>}
                </span>
              ),
            },
          ]}
        />
      )}
    </FrostTable>
  );
}
