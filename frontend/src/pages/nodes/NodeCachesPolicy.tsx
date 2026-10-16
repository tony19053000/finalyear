/**
 * Caches (`cache` of `GET /v1/nodes/{id}`: count + bytes per kind, from the
 * node's cache reports, D101) and the owner policy (D142). The policy is set
 * on the laptop itself (`meshforge-agent policy set`) - agent-local by design
 * - so the dashboard shows it read-only.
 */
import type { NodeDetail } from "../../api/fabric";
import { Card } from "../../components/Card";
import { CodeBlock } from "../../components/CodeBlock";
import { Badge } from "../../components/Status";
import { Table } from "../../components/Table";
import { formatBytes } from "../../lib/format";

const CACHE_LABELS: Record<string, string> = {
  blob: "Blobs (content store)",
  tree: "Project trees",
  env: "Python environments",
  image: "Container images",
};

function policyValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return JSON.stringify(value);
}

export function NodeCaches({ node }: { node: NodeDetail }) {
  const kinds = Object.entries(node.cache ?? {});
  return (
    <Card title="Caches" data-testid="caches-card">
      {kinds.length === 0 ? (
        <p className="text-sm text-fg-muted">Not reported - the node has not sent a cache report yet.</p>
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
    </Card>
  );
}

export function NodePolicy({ node }: { node: NodeDetail }) {
  const policy = node.owner_policy;
  const notEnforced = node.not_enforced ?? [];
  return (
    <Card title="Owner policy" actions={<Badge tone="neutral">read-only</Badge>} data-testid="policy-card">
      {policy == null ? (
        <p className="text-sm text-fg-muted">
          Not reported - the node is offline, or its agent is older than V0.7 G5.
        </p>
      ) : (
        <dl>
          {Object.entries(policy).map(([key, value]) => (
            <div key={key} className="grid grid-cols-[14rem_1fr] gap-3 py-1">
              <dt className="font-mono text-xs leading-5 text-fg-muted">{key}</dt>
              <dd className="font-mono text-xs leading-5 text-fg">{policyValue(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {notEnforced.length > 0 && (
        <div className="mt-3 flex flex-col gap-1">
          <p className="text-sm text-status-degraded">Caps no runtime limit enforces on this node:</p>
          <ul className="flex flex-wrap gap-1.5">
            {notEnforced.map((item) => (
              <li key={item}>
                <Badge tone="degraded">{item}</Badge>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="mt-3 text-xs text-fg-muted">The laptop's owner sets this on the laptop itself:</p>
      <CodeBlock className="mt-1" label="Change the owner policy" code={"meshforge-agent policy show\nmeshforge-agent policy set KEY=VALUE"} />
    </Card>
  );
}
