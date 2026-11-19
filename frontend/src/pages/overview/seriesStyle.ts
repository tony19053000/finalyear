/** Series colour classes (tokens --mf-series-*) and the donut legend texts. */
import type { Resource, ResourceTotals } from "./overviewModel";

const GiB = 1024 ** 3;

export const SERIES_CLASS: Record<Resource, { stroke: string; bg: string; text: string; fill: string }> = {
  cpu: { stroke: "stroke-series-cpu", bg: "bg-series-cpu", text: "text-series-cpu", fill: "fill-series-cpu" },
  ram: { stroke: "stroke-series-ram", bg: "bg-series-ram", text: "text-series-ram", fill: "fill-series-ram" },
  gpu: { stroke: "stroke-series-gpu", bg: "bg-series-gpu", text: "text-series-gpu", fill: "fill-series-gpu" },
};

function amount(v: number): string {
  return v < 10 ? v.toFixed(1).replace(/\.0$/, "") : String(Math.round(v));
}

export function legendText(r: Resource, t: ResourceTotals): string | null {
  if (r === "cpu") return t.cpu === null ? null : `${amount(t.cpu.used)} / ${amount(t.cpu.total)} cores`;
  if (r === "ram") return t.ram === null ? null : `${amount(t.ram.used / GiB)} / ${amount(t.ram.total / GiB)} GB`;
  return t.gpu === null ? null : `${String(t.gpu.busy)} / ${String(t.gpu.total)} GPU${t.gpu.total === 1 ? "" : "s"}`;
}


/** Total hardware only: "36 cores", "72 GB", "18 GB VRAM". */
export function totalText(r: Resource, t: ResourceTotals): string | null {
  if (r === "cpu") return t.cpu === null ? null : `${amount(t.cpu.total)} cores`;
  if (r === "ram") return t.ram === null ? null : `${amount(t.ram.total / GiB)} GB`;
  return t.gpu === null ? null : `${amount(t.gpu.vramTotal / GiB)} GB VRAM`;
}
