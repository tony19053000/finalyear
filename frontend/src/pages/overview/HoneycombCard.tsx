/**
 * Nodes as an isometric honeycomb (honeycombScene.ts). Hover a cell: name, state,
 * CPU / RAM / GPU; click: that node's page. Below it, the state counts in
 * words (status is never colour alone) and, for keyboard and screen-reader
 * users, one link per laptop.
 */
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { NodeSummary } from "../../api/endpoints";
import { StatusDot } from "../../components/Status";
import { countByState, fabricHealth } from "../nodes/nodeView";
import { HoneycombScene, type CombNode, type CombState } from "./honeycombScene";
import { OverviewCard } from "./OverviewCard";
import { meanLoad, members, nodeLoad } from "./overviewModel";

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** jsdom has no 2D canvas (it logs "not implemented"): the comb is skipped there. */
function canDraw(): boolean {
  return typeof navigator !== "undefined" && !navigator.userAgent.includes("jsdom");
}

const GiB = 1024 ** 3;

function gb(bytes: number): string {
  const v = bytes / GiB;
  return `${v < 10 ? v.toFixed(1).replace(/\.0$/, "") : String(Math.round(v))} GB`;
}

function combNodes(nodes: readonly NodeSummary[]): CombNode[] {
  return members(nodes).map((n) => {
    const load = meanLoad(nodeLoad(n));
    return {
      id: n.node_id,
      name: n.display_name || n.hostname,
      state: n.state as CombState,
      load: load === null ? null : load / 100,
    };
  });
}

export interface HoneycombCardProps {
  nodes: readonly NodeSummary[];
  /** node_id -> logical cores (from node details). */
  cores?: ReadonlyMap<string, number>;
}

/** Footer swatches in the cells' own colours. */
const STATE_SWATCH: Record<string, string> = {
  online: "bg-[#f7b634]",
  suspect: "bg-[#b07f2c]",
  draining: "bg-[#8c6a30]",
  enrolling: "bg-[#c99a3e]",
  offline: "bg-[#2a241b] ring-1 ring-[#5c4e38]",
};

export function HoneycombCard({ nodes, cores }: HoneycombCardProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const scene = useRef<HoneycombScene | null>(null);
  const navigate = useNavigate();
  const [tip, setTip] = useState<{ id: string; x: number; y: number; flip: boolean; h: number } | null>(null);
  const list = members(nodes);
  const fabric = fabricHealth(nodes);
  const counts = countByState(list); // revoked nodes are not shown on the Overview

  // Create the scene once; size it with the card; run it only while visible.
  useEffect(() => {
    const el = canvas.current;
    const box = wrap.current;
    if (el === null || box === null || !canDraw()) return;
    let ctx: CanvasRenderingContext2D | null;
    try {
      ctx = el.getContext("2d");
    } catch {
      ctx = null;
    }
    if (ctx === null) return;
    const s = new HoneycombScene(el, ctx, { reduced: prefersReducedMotion() });
    scene.current = s;
    const size = () => {
      s.resize(box.clientWidth, box.clientHeight);
    };
    size();
    let onScreen = true;
    const update = () => {
      if (onScreen && !document.hidden) s.start();
      else s.stop();
    };
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(size);
    ro?.observe(box);
    const io =
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver((entries) => {
            onScreen = entries.some((e) => e.isIntersecting);
            update();
          });
    io?.observe(box);
    document.addEventListener("visibilitychange", update);
    update();
    return () => {
      s.stop();
      ro?.disconnect();
      io?.disconnect();
      document.removeEventListener("visibilitychange", update);
      scene.current = null;
    };
  }, []);

  const nodesKey = JSON.stringify(combNodes(nodes));
  useEffect(() => {
    scene.current?.setData(JSON.parse(nodesKey) as CombNode[]);
  }, [nodesKey]);

  const pick = (e: { clientX: number; clientY: number; currentTarget: HTMLCanvasElement }) => {
    const s = scene.current;
    if (s === null) return null;
    const r = e.currentTarget.getBoundingClientRect();
    return s.hitTest(e.clientX - r.left, e.clientY - r.top);
  };

  const tipNode = tip === null ? undefined : list.find((n) => n.node_id === tip.id);
  const tipLoad = tipNode === undefined ? null : nodeLoad(tipNode);

  return (
    <OverviewCard
      title="Nodes"
      data-testid="nodes-card"
      className="ov-comb-card h-[21rem] overflow-hidden"
    >
      {/* The comb fills the whole card, behind the title and counts. */}
      <div ref={wrap} className="absolute inset-0">
        <canvas
          ref={canvas}
          aria-hidden="true"
          className={`absolute inset-0 h-full w-full ${tip !== null ? "cursor-pointer" : ""}`}
          onPointerMove={(e) => {
            const id = pick(e);
            scene.current?.setHovered(id);
            if (id === null) {
              setTip(null);
              return;
            }
            const r = e.currentTarget.getBoundingClientRect();
            setTip({ id, x: e.clientX - r.left, y: e.clientY - r.top, flip: e.clientX - r.left > r.width - 210, h: r.height });
          }}
          onPointerLeave={() => {
            scene.current?.setHovered(null);
            setTip(null);
          }}
          onClick={(e) => {
            const id = pick(e);
            if (id !== null) void navigate(`/nodes/${encodeURIComponent(id)}`);
          }}
        />
        {list.length === 0 && (
          <p className="pointer-events-none absolute inset-x-0 bottom-6 text-center text-sm text-fg-muted">
            No laptops yet - every laptop that joins becomes a cell of the hive.
          </p>
        )}
        {tip !== null && tipNode !== undefined && tipLoad !== null && (
          <svg aria-hidden="true" className="pointer-events-none absolute inset-0 h-full w-full overflow-visible">
            <foreignObject x={tip.flip ? tip.x - 206 : tip.x + 14} y={Math.min(tip.y + 14, tip.h - 130)} width={192} height={128}>
          <div className="ov-tooltip px-3 py-2.5 text-xs" data-testid="comb-tooltip">
            <p className="truncate text-sm font-medium text-fg">{tipNode.display_name || tipNode.hostname}</p>
            <p className="mb-2 text-xs text-fg-muted" data-testid="comb-tooltip-state">
              {tipNode.state}
            </p>
            {/* What this laptop brings to the hive (hardware, not usage). */}
            <p className="flex justify-between font-mono tabular-nums">
              <span className="text-series-cpu">CPU</span>{" "}
              <span className="text-fg">{cores?.get(tipNode.node_id) === undefined ? "—" : `${String(cores.get(tipNode.node_id))} cores`}</span>
            </p>
            <p className="flex justify-between font-mono tabular-nums">
              <span className="text-series-ram">RAM</span>{" "}
              <span className="text-fg">{tipNode.ram_total_bytes ? gb(tipNode.ram_total_bytes) : "—"}</span>
            </p>
            <p className="flex justify-between font-mono tabular-nums">
              <span className="text-series-gpu">GPU</span>{" "}
              <span className={tipNode.gpus.length === 0 ? "text-fg-subtle" : "text-fg"}>
                {tipNode.gpus.length === 0 ? "—" : `${gb(tipNode.gpus.reduce((a, g) => a + g.vram_total_bytes, 0))} VRAM`}
              </span>
            </p>
          </div>
            </foreignObject>
          </svg>
        )}
      </div>
      <div className="pointer-events-none absolute top-7 right-7 z-10 flex justify-end">
        {/* Totals for screen readers (the cells and the counts say it visually). */}
        <p className="sr-only" data-testid="health-fabric">
          <StatusDot tone={fabric.tone} label={fabric.headline} />
        </p>
        <span className="sr-only">
          Total <span data-testid="nodes-total">{list.length}</span>
        </span>
        <dl className="flex flex-wrap items-center gap-x-5 gap-y-1 font-mono text-[0.625rem] font-medium tracking-[0.14em] uppercase">
          {[...counts.entries()].map(([state, count]) => (
            <div key={state} className="flex items-center gap-1.5">
              <span aria-hidden="true" className={`h-2 w-2 rounded-[2px] ${STATE_SWATCH[state] ?? "bg-[#2a251c]"}`} />
              <dd className="text-fg tabular-nums" data-testid={`nodes-${state}`}>
                {count}
              </dd>
              <dt className="text-fg-muted">{state}</dt>
            </div>
          ))}
        </dl>
      </div>
      <ul className="sr-only" aria-label="Laptops">
        {list.map((n) => (
          <li key={n.node_id}>
            <Link to={`/nodes/${encodeURIComponent(n.node_id)}`}>
              {n.display_name || n.hostname}, {n.state}
            </Link>
          </li>
        ))}
      </ul>
    </OverviewCard>
  );
}
