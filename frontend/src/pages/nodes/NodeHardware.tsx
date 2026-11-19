/**
 * What the node can do: `GET /v1/nodes/{id}` `hardware` (the agent's Hello
 * summary, proto field names, 64-bit values as strings), its measured
 * `scores`, and the disks of its newest live sample (`GET .../resources`).
 * A field the node did not send is "not reported", never a guess.
 *
 * Layout: the 3D tower lit by live load (centrepiece) beside the specs as big calm
 * read-outs - processor and each GPU as heroes, storage as thin ring gauges in
 * the same column; scores in a frosted table below.
 */
import type { ReactNode } from "react";

import { useNodeResources, type NodeDetail } from "../../api/fabric";
import { ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table } from "../../components/Table";
import { cn } from "../../lib/cn";
import { formatBytes, formatDateTime, percentOf, toNumber } from "../../lib/format";
import { FrostTable } from "./FrostTable";
import { PcSetupScene } from "./PcSetupScene";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): string | null {
  if (typeof value === "string" && value !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

const NOT_REPORTED = "not reported";

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

function Spec({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <p className="nd-label">{label}</p>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Missing() {
  return <span className="text-base font-normal tracking-normal text-fg-subtle">{NOT_REPORTED}</span>;
}

/** A thin ring: used share of a disk. */
function Ring({ percent }: { percent: number | null }) {
  const r = 26;
  const c = 2 * Math.PI * r;
  const used = percent === null ? 0 : (Math.max(0, Math.min(100, percent)) / 100) * c;
  return (
    <svg viewBox="0 0 64 64" width="50" height="50" aria-hidden="true" className="shrink-0">
      <circle cx="32" cy="32" r={r} fill="none" stroke="currentColor" className="text-fg" strokeOpacity={0.08} strokeWidth={3} />
      <circle
        cx="32"
        cy="32"
        r={r}
        fill="none"
        strokeWidth={3}
        strokeLinecap="round"
        strokeDasharray={`${used.toFixed(1)} ${c.toFixed(1)}`}
        transform="rotate(-90 32 32)"
        className="nd-ring stroke-fg-muted"
      />
      <text x="32" y="35.5" textAnchor="middle" className="fill-fg font-mono text-[12.5px]">
        {percent === null ? "—" : `${String(Math.round(percent))}%`}
      </text>
    </svg>
  );
}

type Disk = { mount: string; free_bytes: number; total_bytes: number };

function Disks({ state, onRetry }: { state: { pending: boolean; error: unknown; disks: Disk[] }; onRetry: () => void }) {
  if (state.pending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading disks…
      </p>
    );
  }
  if (state.error !== null) {
    return <ErrorState title="Could not load the node's resources" error={state.error} onRetry={onRetry} />;
  }
  if (state.disks.length === 0) {
    return <p className="text-sm text-fg-muted">Not reported (no live sample from this node right now).</p>;
  }
  return (
    <ul className="nd-stagger grid grid-cols-[repeat(auto-fit,minmax(10.5rem,1fr))] gap-x-6 gap-y-5" aria-label="Disks of the newest live sample">
      {state.disks.map((d) => (
        <li key={d.mount} className="flex min-w-0 items-center gap-3.5">
          <Ring percent={percentOf(d.total_bytes - d.free_bytes, d.total_bytes)} />
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-mono text-xs text-fg-muted" title={d.mount}>
              {d.mount}
            </span>
            <span className="mt-1 text-base text-fg tabular-nums">{formatBytes(d.free_bytes)}</span>
            <span className="text-xs text-fg-subtle">free of {formatBytes(d.total_bytes)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

/** One GPU as a hero: its name at the processor's size, VRAM as a big light figure beside it. */
function GpuHero({ g, label }: { g: GpuFact; label: string }) {
  return (
    <div className="nd-hair-t grid grid-cols-[minmax(0,1fr)_auto] items-start gap-10 py-4" data-testid={`gpu-${g.index}`}>
      <Spec label={label}>
        <p className="text-[1.5rem] leading-tight font-light tracking-[-0.02em] text-fg">{g.model ?? <Missing />}</p>
        <p className="mt-1.5 font-mono text-[0.6875rem] text-fg-subtle">
          driver {g.driver ?? "—"} · compute {g.cc ?? "—"}
        </p>
      </Spec>
      <Spec label="VRAM">
        <p className="ov-figure text-[2.25rem] leading-none whitespace-nowrap text-fg">{g.vram ?? <Missing />}</p>
      </Spec>
    </div>
  );
}

function Scores({ node }: { node: NodeDetail }) {
  const scores = node.scores ?? { status: "none", metrics: [] };
  return (
    <section aria-label="Measured scores">
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <h2 className="nd-label">Measured scores</h2>
        {scores.status === "measured" && (
          <span className="font-mono text-[0.6875rem] text-fg-subtle">{formatDateTime(scores.measured_at)}</span>
        )}
      </div>
      {scores.status !== "measured" ? (
        <p className="text-sm text-fg-muted">
          {scores.status === "pending"
            ? "Not measured yet: the agent scores its hardware once after it first comes online."
            : "This node does not report hardware scores."}
        </p>
      ) : (
        <FrostTable className="nd-scores">
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
                align: "right",
                cell: (m) => (
                  <span title={m.reason ?? undefined}>
                    <Badge tone={m.status === "ok" ? "ok" : m.status === "failed" ? "failed" : "unknown"}>{m.status}</Badge>
                  </span>
                ),
              },
            ]}
          />
        </FrostTable>
      )}
    </section>
  );
}

export function NodeHardware({ node }: { node: NodeDetail }) {
  const resources = useNodeResources(node.node_id);
  const hw = isRecord(node.hardware) ? node.hardware : {};
  const ram = toNumber(hw.ram_bytes);
  const caps = Array.isArray(hw.capabilities) ? (hw.capabilities as unknown[]).map(text).filter((c) => c !== null) : [];
  const gpuList = gpus(hw);
  const cores = toNumber(hw.cpu_cores);
  const threads = toNumber(hw.cpu_threads);
  const disks: Disk[] = resources.data?.live.at(-1)?.disks ?? [];
  const cpuModel = text(hw.cpu_model);

  return (
    <div className="flex flex-col gap-14">
      {/* one screen: the tower takes exactly the specs column's height (it fills its grid cell) */}
      <div className="grid grid-cols-1 gap-10 xl:grid-cols-[minmax(0,0.78fr)_minmax(0,1fr)] xl:gap-12">
        <div className="relative min-h-[26rem] min-w-0 xl:min-h-0">
          <PcSetupScene node={node} />
        </div>

        <div className="flex flex-col" data-testid="hardware-card">
          <Spec label="Processor" className="pb-4">
            <p className="text-[1.5rem] leading-tight font-light tracking-[-0.02em] text-fg">{cpuModel ?? <Missing />}</p>
          </Spec>
          <div className="nd-hair-t grid grid-cols-2 gap-10 py-4">
            <Spec label="Cores / threads">
              <p className="ov-figure text-[2.25rem] leading-none whitespace-nowrap text-fg">
                {cores === null ? <Missing /> : `${String(cores)} / ${threads === null ? "?" : String(threads)}`}
              </p>
            </Spec>
            <Spec label="Memory">
              <p className="ov-figure text-[2.25rem] leading-none whitespace-nowrap text-fg">{ram === null ? <Missing /> : formatBytes(ram)}</p>
            </Spec>
          </div>
          {gpuList.length === 0 ? (
            <Spec label="Graphics" className="nd-hair-t py-4">
              <p className="text-base text-fg-subtle">none reported</p>
            </Spec>
          ) : (
            gpuList.map((g) => <GpuHero key={g.index} g={g} label={gpuList.length > 1 ? `Graphics · GPU ${g.index}` : "Graphics"} />)
          )}
          <Spec label="Storage" className="nd-hair-t py-4">
            <Disks
              state={{ pending: resources.isPending, error: resources.isError ? resources.error : null, disks }}
              onRetry={() => {
                void resources.refetch();
              }}
            />
          </Spec>
          <div className="nd-hair-t grid grid-cols-2 gap-10 pt-4">
            <Spec label="Operating system">
              <p className="text-base text-fg">{node.os}</p>
              <p className="mt-1 font-mono text-xs text-fg-muted">{node.platform}</p>
            </Spec>
            <Spec label="Capabilities">
              {caps.length === 0 ? (
                <p className="text-base text-fg-subtle">{NOT_REPORTED}</p>
              ) : (
                <ul className="flex flex-wrap gap-x-4 gap-y-1.5 font-mono text-[0.8125rem] text-fg">
                  {caps.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              )}
            </Spec>
          </div>
        </div>
      </div>

      <Scores node={node} />
    </div>
  );
}
