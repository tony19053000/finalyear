/**
 * What the node can do: `GET /v1/nodes/{id}` `hardware` (the agent's Hello
 * summary, proto field names, 64-bit values as strings), its measured
 * `scores`, and the disks of its newest live sample (`GET .../resources`).
 * A field the node did not send is "not reported", never a guess.
 */
import { useNodeResources, type NodeDetail } from "../../api/fabric";
import { Card } from "../../components/Card";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table } from "../../components/Table";
import { formatBytes, formatDateTime, percentOf, formatPercent, toNumber } from "../../lib/format";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  if (typeof value === "string" && value !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

const NOT_REPORTED = "not reported";

function Fact({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-3 py-1">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className={value === null ? "text-sm text-fg-subtle" : "text-sm text-fg"}>{value ?? NOT_REPORTED}</dd>
    </div>
  );
}

interface GpuFact {
  index: string;
  model: string | null;
  vram: string | null;
  driver: string | null;
  cc: string | null;
}

function gpus(hardware: Record<string, unknown>): GpuFact[] {
  const list = hardware.gpus;
  if (!Array.isArray(list)) return [];
  return (list as unknown[]).filter(isRecord).map((g, i) => {
    const vram = toNumber(g.vram_bytes);
    return {
      index: text(g.index) ?? String(i),
      model: text(g.model),
      vram: vram === null ? null : formatBytes(vram),
      driver: text(g.driver_version),
      cc: text(g.compute_capability),
    };
  });
}

function Disks({ nodeId }: { nodeId: string }) {
  const resources = useNodeResources(nodeId);
  if (resources.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading disks…
      </p>
    );
  }
  if (resources.isError) {
    return (
      <ErrorState
        title="Could not load the node's resources"
        error={resources.error}
        onRetry={() => {
          void resources.refetch();
        }}
      />
    );
  }
  const disks = resources.data.live.at(-1)?.disks ?? [];
  if (disks.length === 0) {
    return <p className="text-sm text-fg-muted">Not reported (no live sample from this node right now).</p>;
  }
  return (
    <Table
      caption="Disks of the newest live sample"
      rows={disks}
      rowKey={(d) => d.mount}
      columns={[
        { id: "mount", header: "Mount", mono: true, cell: (d) => d.mount },
        { id: "free", header: "Free", align: "right", mono: true, cell: (d) => formatBytes(d.free_bytes) },
        { id: "total", header: "Total", align: "right", mono: true, cell: (d) => formatBytes(d.total_bytes) },
        {
          id: "used",
          header: "Used",
          align: "right",
          mono: true,
          cell: (d) => formatPercent(percentOf(d.total_bytes - d.free_bytes, d.total_bytes)),
        },
      ]}
    />
  );
}

export function NodeHardware({ node }: { node: NodeDetail }) {
  const hw = isRecord(node.hardware) ? node.hardware : {};
  const ram = toNumber(hw.ram_bytes);
  const caps = Array.isArray(hw.capabilities) ? (hw.capabilities as unknown[]).map(text).filter((c) => c !== null) : [];
  const gpuList = gpus(hw);
  const scores = node.scores ?? { status: "none", metrics: [] };
  return (
    <div className="flex flex-col gap-5">
      <Card title="Hardware" data-testid="hardware-card">
        <dl>
          <Fact label="CPU" value={text(hw.cpu_model)} />
          <Fact
            label="Cores / threads"
            value={text(hw.cpu_cores) === null ? null : `${text(hw.cpu_cores) ?? ""} / ${text(hw.cpu_threads) ?? "?"}`}
          />
          <Fact label="Memory" value={ram === null ? null : formatBytes(ram)} />
          <Fact label="GPUs" value={gpuList.length === 0 ? "none reported" : String(gpuList.length)} />
        </dl>
        {gpuList.length > 0 && (
          <Table
            className="mt-3"
            caption="GPUs"
            rows={gpuList}
            rowKey={(g) => g.index}
            columns={[
              { id: "index", header: "#", mono: true, cell: (g) => g.index },
              { id: "model", header: "Model", cell: (g) => g.model ?? NOT_REPORTED },
              { id: "vram", header: "Memory", mono: true, align: "right", cell: (g) => g.vram ?? NOT_REPORTED },
              { id: "driver", header: "Driver", mono: true, cell: (g) => g.driver ?? NOT_REPORTED },
              { id: "cc", header: "Compute capability", mono: true, cell: (g) => g.cc ?? NOT_REPORTED },
            ]}
          />
        )}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-sm text-fg-muted">Capabilities:</span>
          {caps.length === 0 ? (
            <span className="text-sm text-fg-subtle">{NOT_REPORTED}</span>
          ) : (
            caps.map((c) => (
              <Badge key={c} tone="neutral">
                {c}
              </Badge>
            ))
          )}
        </div>
      </Card>
      <Card title="Disks">
        <Disks nodeId={node.node_id} />
      </Card>
      <Card
        title="Measured scores"
        actions={<Badge tone={scores.status === "measured" ? "ok" : "unknown"}>{scores.status}</Badge>}
      >
        {scores.status !== "measured" ? (
          <p className="text-sm text-fg-muted">
            {scores.status === "pending"
              ? "Not measured yet: the agent scores its hardware once after it first comes online."
              : "This node does not report hardware scores."}
          </p>
        ) : (
          <>
            <p className="mb-2 text-xs text-fg-muted">Measured {formatDateTime(scores.measured_at)}</p>
            <Table
              caption="Hardware scores"
              rows={scores.metrics}
              rowKey={(m) => `${m.name}-${String(m.gpu_index ?? "")}`}
              columns={[
                { id: "name", header: "Metric", mono: true, cell: (m) => m.name },
                {
                  id: "value",
                  header: "Value",
                  align: "right",
                  mono: true,
                  cell: (m) => (m.mean == null ? "—" : `${m.mean.toPrecision(4)} ${m.unit ?? ""}`),
                },
                {
                  id: "status",
                  header: "Status",
                  cell: (m) => (
                    <span title={m.reason ?? undefined}>
                      <Badge tone={m.status === "ok" ? "ok" : m.status === "failed" ? "failed" : "unknown"}>{m.status}</Badge>
                    </span>
                  ),
                },
              ]}
            />
          </>
        )}
      </Card>
    </div>
  );
}
