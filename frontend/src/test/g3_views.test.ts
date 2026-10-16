/** V0.8 G3: the pure helpers every Overview/Nodes number goes through (checklist 26). */
import { afterEach, describe, expect, it } from "vitest";

import type { NodeSummary } from "../api/endpoints";
import type { NetworkOut } from "../api/fabric";
import { formatAgo, formatBytes, formatMs, formatPercent, percentOf, shortId, toNumber } from "../lib/format";
import { noteServerTime, resetServerClock, serverClockOffsetMs, serverNow } from "../lib/serverClock";
import { peerRows } from "../pages/nodes/networkView";
import { countByState, fabricHealth, sortNodes, utilization } from "../pages/nodes/nodeView";
import { node } from "./fakeFabric";

const n = (id: string, o: Record<string, unknown> = {}) => node(id, o) as unknown as NodeSummary;

describe("format", () => {
  it("bytes, percent, rates and times", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(16 * 1024 ** 3)).toBe("16.0 GiB");
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(-1)).toBe("—");
    expect(formatPercent(42.5)).toBe("43%");
    expect(formatPercent(undefined)).toBe("—");
    expect(percentOf(1, 4)).toBe(25);
    expect(percentOf(1, 0)).toBeNull();
    expect(formatMs(0.42)).toBe("0.42 ms");
    expect(formatMs(12.4)).toBe("12 ms");
    expect(formatMs(1500)).toBe("1.5 s");
    expect(formatAgo(0.5)).toBe("just now");
    expect(formatAgo(-3)).toBe("just now"); // clock skew never prints "in the future"
    expect(formatAgo(42)).toBe("42 s ago");
    expect(formatAgo(125)).toBe("2 min ago");
    expect(formatAgo(7300)).toBe("2 h ago");
    expect(formatAgo(200_000)).toBe("2 d ago");
  });

  it("64-bit strings and short ids", () => {
    expect(toNumber("17179869184")).toBe(17179869184);
    expect(toNumber(5)).toBe(5);
    expect(toNumber("1e3")).toBeNull();
    expect(toNumber(null)).toBeNull();
    expect(shortId("node_01J00000000000000000ABCDEF")).toBe("node_…ABCDEF");
    expect(shortId("short")).toBe("short");
  });
});

describe("serverClock", () => {
  afterEach(() => {
    resetServerClock();
  });
  it("follows the controller's clock by the median offset", () => {
    expect(serverNow(1000)).toBe(1000);
    noteServerTime(65_000, 5_000); // controller 60 s ahead
    noteServerTime(65_100, 5_000);
    noteServerTime(999_999, 5_000); // one stray sample does not move the median far
    expect(serverClockOffsetMs()).toBe(60_100);
    expect(serverNow(0)).toBe(60_100);
  });
});

describe("nodeView", () => {
  const nodes = [
    n("a", { state: "online", cpu_percent: 20, ram_used_bytes: 2, ram_total_bytes: 8, gpus: [{ index: 0, util_percent: 50, vram_used_bytes: 1, vram_total_bytes: 4 }] }),
    n("b", { state: "online", cpu_percent: 60, ram_used_bytes: 6, ram_total_bytes: 8 }),
    n("c", { state: "offline", connected: false, cpu_percent: null, ram_used_bytes: null, ram_total_bytes: null }),
    n("d", { state: "revoked", connected: false, cpu_percent: null }),
  ];

  it("counts by state in display order", () => {
    expect([...countByState(nodes).entries()]).toEqual([
      ["online", 2],
      ["offline", 1],
      ["revoked", 1],
    ]);
  });

  it("health ignores revoked nodes and never invents a state", () => {
    expect(fabricHealth(nodes)).toMatchObject({ tone: "degraded", headline: "2 of 3 nodes online", online: 2, total: 3 });
    expect(fabricHealth([])).toMatchObject({ tone: "unknown", headline: "No nodes yet" });
    expect(fabricHealth(nodes.slice(0, 2)).headline).toBe("All 2 nodes online");
    expect(fabricHealth(nodes.slice(2, 3)).tone).toBe("failed");
  });

  it("utilisation is over reporting nodes only", () => {
    expect(utilization(nodes)).toEqual({
      reporting: 2,
      cpuMean: 40,
      ramUsed: 8,
      ramTotal: 16,
      gpuCount: 1,
      gpuUtilMean: 50,
      vramUsed: 1,
      vramTotal: 4,
    });
    expect(utilization(nodes.slice(2)).cpuMean).toBeNull();
  });

  it("sorts by a column both ways; unknown counts lowest; ties by name", () => {
    expect(sortNodes(nodes, "cpu", "descending").map((x) => x.node_id)).toEqual(["b", "a", "c", "d"]);
    expect(sortNodes(nodes, "cpu", "ascending").map((x) => x.node_id)).toEqual(["c", "d", "a", "b"]);
    expect(sortNodes(nodes, "state", "ascending").map((x) => x.node_id)).toEqual(["a", "b", "c", "d"]);
    expect(sortNodes(nodes, "name", "descending").map((x) => x.node_id)).toEqual(["d", "c", "b", "a"]);
  });
});

describe("networkView", () => {
  it("both directions per peer; unmeasured pairs are null, failures kept", () => {
    const network = {
      now: "",
      stale_after_s: 600,
      prober: {},
      controller_links: [],
      links: [
        {
          src_node_id: "me",
          dst_node_id: "p1",
          rtt_ms: 1,
          bw_bytes_s: 1e8,
          samples: 3,
          measured_at: "",
          age_s: 5,
          stale: false,
          source: "measured",
          last_failure: null,
        },
      ],
      failures: [{ src_node_id: "p2", dst_node_id: "me", error: "timeout", at: "t" }],
    } as unknown as NetworkOut;
    const rows = peerRows(network, "me", ["p1", "p2"]);
    expect(rows.map((r) => [r.peer, r.direction, r.link?.bw_bytes_s ?? null, r.failure?.error ?? null])).toEqual([
      ["p1", "from_peer", 1e8, null],
      ["p1", "to_peer", null, null],
      ["p2", "from_peer", null, null],
      ["p2", "to_peer", null, "timeout"],
    ]);
  });
});
