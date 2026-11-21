/**
 * The Hardware tab's 3D tower (frontend/blender/pc_setup.py renders it): one
 * three-quarter still with one light-up layer per part, and the mapping from
 * a node's live numbers to how bright each layer is. Pure, so it is tested
 * without images.
 */
import type { NodeDetail } from "../../api/fabric";
import { percentOf } from "../../lib/format";

export const PC_SETUP_BASE = `${import.meta.env.BASE_URL}pc-setup/`;

export const LIGHTS = ["cpu", "ram0", "ram1", "ram2", "ram3", "gpu"] as const;
export type Light = (typeof LIGHTS)[number];

export function towerUrl(light?: Light): string {
  return `${PC_SETUP_BASE}tower${light === undefined ? "" : `-${light}`}.webp`;
}

function unit(v: number): number {
  return Math.max(0, Math.min(1, v));
}

/**
 * Opacity of every light layer from the node's live numbers: CPU = load;
 * RAM = used share x 4 sticks, filled in order (the last one partly);
 * GPU = busiest GPU's utilisation (dark when the node reports none).
 * Offline or no live sample = all dark.
 */
export function lightLevels(node: NodeDetail): Record<Light, number> {
  const live = node.connected && node.cpu_percent != null;
  const sticks = ((live ? (percentOf(node.ram_used_bytes, node.ram_total_bytes) ?? 0) : 0) / 100) * 4;
  const gpu = live && node.gpus.length > 0 ? Math.max(...node.gpus.map((g) => g.util_percent)) : 0;
  return {
    cpu: live ? unit((node.cpu_percent ?? 0) / 100) : 0,
    ram0: unit(sticks),
    ram1: unit(sticks - 1),
    ram2: unit(sticks - 2),
    ram3: unit(sticks - 3),
    gpu: unit(gpu / 100),
  };
}
