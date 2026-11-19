import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { NodeDetail } from "../api/fabric";
import { PcSetupScene } from "../pages/nodes/PcSetupScene";
import { lightLevels } from "../pages/nodes/pcSetup";
import { nodeDetail } from "./fakeFabric";

const GiB = 1024 ** 3;

function detail(overrides: Record<string, unknown> = {}): NodeDetail {
  return nodeDetail("node_A", overrides) as unknown as NodeDetail;
}

describe("3D tower lights", () => {
  it("CPU = load, RAM fills sticks in order with the last one partial, GPU = busiest GPU", () => {
    const l = lightLevels(
      detail({
        connected: true,
        cpu_percent: 40,
        ram_used_bytes: 10 * GiB,
        ram_total_bytes: 16 * GiB, // 62.5% of 4 sticks = 2.5 sticks
        gpus: [
          { index: 0, util_percent: 20, vram_used_bytes: GiB, vram_total_bytes: 4 * GiB },
          { index: 1, util_percent: 72, vram_used_bytes: GiB, vram_total_bytes: 4 * GiB },
        ],
      }),
    );
    expect(l.cpu).toBeCloseTo(0.4);
    expect([l.ram0, l.ram1, l.ram2, l.ram3]).toEqual([1, 1, 0.5, 0]);
    expect(l.gpu).toBeCloseTo(0.72);
  });

  it("dark without a GPU, and all dark when offline", () => {
    expect(lightLevels(detail({ connected: true, cpu_percent: 10, gpus: [] })).gpu).toBe(0);
    const off = lightLevels(detail({ connected: false, cpu_percent: null }));
    expect(Object.values(off).every((v) => v === 0)).toBe(true);
  });
});

describe("PcSetupScene", () => {
  it("renders the tower with its light layers and sets the live levels as custom properties", () => {
    render(<PcSetupScene node={detail({ connected: true, cpu_percent: 40, ram_used_bytes: 4 * GiB, ram_total_bytes: 16 * GiB, gpus: [] })} />);
    const scene = screen.getByTestId("pc-setup-scene");
    expect(scene.querySelector(".pc-base")?.getAttribute("src")).toBe("/pc-setup/tower.webp");
    expect(scene.querySelectorAll(".pc-light")).toHaveLength(6);
    expect(scene.style.getPropertyValue("--pc-cpu")).toBe("0.400");
    expect(scene.style.getPropertyValue("--pc-ram0")).toBe("1.000");
    expect(scene.style.getPropertyValue("--pc-ram1")).toBe("0.000");
    expect(scene.style.getPropertyValue("--pc-gpu")).toBe("0.000");
    expect(scene.getAttribute("aria-label")).toContain("CPU 40%");
  });

  it("settles back level when the cursor leaves", () => {
    render(<PcSetupScene node={detail()} />);
    const scene = screen.getByTestId("pc-setup-scene");
    fireEvent.pointerLeave(scene);
    expect(scene.style.getPropertyValue("--pc-ry")).toBe("0deg");
    expect(scene.style.getPropertyValue("--pc-rx")).toBe("0deg");
  });

  it("says so when the picture cannot load", () => {
    render(<PcSetupScene node={detail()} />);
    const base = screen.getByTestId("pc-setup-scene").querySelector(".pc-base");
    if (base === null) throw new Error("no base image");
    fireEvent.error(base);
    expect(screen.getByTestId("pc-setup-fallback").textContent).toContain("could not load");
  });
});
