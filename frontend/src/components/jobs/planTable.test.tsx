/**
 * The Plan tab's states (V0.14 G5, D300): no decision, pinned job, rejected
 * only, still running, no replay, a pre-V0.14 row, and a finished calibrated
 * whole job / split job (on the contract fixture - the controller's own answer).
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import contract from "../../test/fixtures/explainContract.json";
import type { DecisionList, DecisionOut } from "../../pages/jobs/jobsApi";
import { PlanTable } from "./PlanTable";
import { marginText, rateText, ratioText, termNames } from "./planView";

const FIXTURE = contract.payload as unknown as DecisionList;

function decision(index: number): DecisionOut {
  const d = structuredClone(FIXTURE.decisions[index]);
  if (d === undefined) throw new Error("fixture shape changed");
  return d;
}

/** A row from before V0.14: no objective, calibration, replay, notes or actuals. */
function preV014(): DecisionOut {
  return {
    id: 3,
    attempt_no: 1,
    attempt_id: "att_old",
    outcome: "dispatched",
    policy: "least_loaded",
    chosen_node_id: "node_A",
    created_at: "2026-09-01T10:00:00Z",
    snapshot_digest: "ab".repeat(32),
    candidates: [
      {
        node_id: "node_A",
        mode: "offload",
        feasible: true,
        slot: "cpu",
        estimate: { total_s: 12, terms: { compute: { seconds: 12, source: "assumed", detail: {} } } },
        score: 1,
        rank: 1,
      },
    ],
    actual: { attempt_id: "att_old", node_id: "node_A", state: "done", phases_ms: { run: 11000 } },
  };
}

function candidates(id: number): HTMLElement {
  return screen.getByRole("table", { name: new RegExp(`Candidates of decision ${String(id)}\\b`) });
}

describe("Plan tab states", () => {
  it("no decision: says why (planner has not placed it / pinned)", () => {
    const { unmount } = render(<PlanTable decisions={[]} pinned={false} />);
    expect(screen.getByText("No placement decisions")).toBeTruthy();
    expect(screen.getByText(/has not placed this job yet/)).toBeTruthy();
    unmount();
    render(<PlanTable decisions={[]} pinned />);
    expect(screen.getByText(/pinned to a node at submit/)).toBeTruthy();
  });

  it("a finished calibrated whole job: header, chosen row, factors, notes, predicted vs actual with ratio", () => {
    render(<PlanTable decisions={[decision(0)]} pinned={false} />);
    const section = screen.getByRole("region", { name: "Decision 21" });
    const head = within(section);
    expect(head.getByText("calibration v7")).toBeTruthy();
    expect(head.getByText("replay v2")).toBeTruthy();
    expect(head.getByText("min_time")).toBeTruthy();
    const table = candidates(21);
    const chosen = within(table)
      .getAllByRole("row")
      .find((r) => r.getAttribute("aria-current") === "true");
    expect(chosen?.textContent).toContain("node_bbbbbbbb");
    expect(within(table).getAllByText("chosen")).toHaveLength(1);
    expect(chosen?.textContent).toContain("×2.00 n=14 node");
    expect(chosen?.textContent).toContain("from controller (last transfer 2h ago)");
    expect(chosen?.textContent).toContain("p_fail 4.0% (1 of 20 ends in 7d)");
    expect(table.textContent).toContain("slots_full: every CPU slot of the node is in use");
    const actual = screen.getByRole("table", { name: "Predicted vs actual of decision 21" });
    const transfer = within(actual).getAllByRole("row").find((r) => r.textContent.startsWith("transfer"));
    expect(transfer?.textContent).toContain("2.6 s");
    expect(transfer?.textContent).toContain("×0.65");
    const risk = within(actual).getAllByRole("row").find((r) => r.textContent.startsWith("risk"));
    expect(risk?.textContent).toContain("—"); // no phase measures risk
    expect(screen.getByText(/Actual: attempt/).textContent).toContain("run 36.0 s");
    // the pinned job's shadow pick
    expect(screen.getByText(/would have picked/).textContent).toContain("node_aaaaaaaa");
  });

  it("a split job: node sets with idle nodes, chunks, rates, margin cells and the server's sentence", () => {
    render(<PlanTable decisions={[decision(1)]} pinned={false} />);
    const table = candidates(22);
    expect(table.textContent).toContain("node_cccccccc (idle)");
    expect(table.textContent).toContain("30 × 10 items");
    expect(table.textContent).toContain("5.75/s (history), first chunk at 3.5 s");
    expect(table.textContent).toContain("pass: 41.0 s ≤ 46.8 s = (1 − 0.1) × 52.0 s (node_aaaaaaaa), forced: node_set");
    expect(screen.getByText(/Margin test:/).closest("p")?.textContent).toContain(
      "split over 3 nodes: 41.00s <= (1 - 0.1) x 52.00s (best single node node_aaaaaaaa), needs <= 46.80s",
    );
    expect(screen.getByText(/Margin test:/).closest("p")?.textContent).toContain("node_dddddddd (by node_aaaaaaaa)");
    // the lane was lost: no per-term actual is claimed
    expect(screen.getByText(/Actual: attempt/).textContent).toContain("failed (node_lost)");
    expect(screen.getByText(/no measured phase is honestly the same work/)).toBeTruthy();
    // a split job's own terms are columns
    for (const t of ["items_fetch", "chunk_overhead", "tail", "combine", "risk"]) {
      expect(within(table).getByRole("columnheader", { name: t })).toBeTruthy();
    }
  });

  it("still running: the actual columns are empty and say when they fill", () => {
    const d = decision(0);
    if (d.actual == null) throw new Error("fixture shape changed");
    d.actual.state = "running";
    for (const c of d.candidates) for (const t of Object.values(c.estimate?.terms ?? {})) {
      t.actual_s = null;
      t.ratio = null;
    }
    render(<PlanTable decisions={[d]} pinned={false} />);
    expect(screen.getByText("Actuals appear when the attempt ends.")).toBeTruthy();
    const actual = screen.getByRole("table", { name: "Predicted vs actual of decision 21" });
    expect(actual.textContent).not.toContain("×0.65");
  });

  it("rejected only: every candidate refused, the tab says so", () => {
    const d = decision(0);
    d.outcome = "waiting";
    d.attempt_id = null;
    d.actual = null;
    d.chosen_node_id = null;
    d.candidates = d.candidates.map((c) => ({ ...c, feasible: false, rank: null, estimate: null, reject_code: "slots_full", reject_text: "full" }));
    render(<PlanTable decisions={[d]} pinned={false} />);
    expect(screen.getByRole("note").textContent).toContain("Every candidate was rejected");
    expect(screen.getByRole("note").textContent).toContain("right now");
    expect(within(candidates(21)).queryByText("chosen")).toBeNull();
  });

  it("no replay snapshot: badged and explained", () => {
    const d = decision(1);
    d.replay_version = null;
    render(<PlanTable decisions={[d]} pinned={false} />);
    expect(screen.getByText("no replay")).toBeTruthy();
    expect(screen.getByText(/no replay snapshot/)).toBeTruthy();
  });

  it("a pre-V0.14 row: no factors, no risk, objective dash, uncalibrated - and no crash", () => {
    render(<PlanTable decisions={[preV014()]} pinned={false} />);
    expect(screen.getByText("uncalibrated")).toBeTruthy();
    expect(screen.getByText("no replay")).toBeTruthy();
    expect(screen.getByTitle(/Recorded before V0.14/)).toBeTruthy();
    const table = candidates(3);
    expect(table.textContent).not.toContain("×");
    expect(within(table).queryByRole("columnheader", { name: "risk" })).toBeNull();
    expect(screen.queryByText(/Margin test:/)).toBeNull();
  });

  it("a pinned job with decisions says what the planner did", () => {
    render(<PlanTable decisions={[decision(0)]} pinned />);
    expect(screen.getByText(/pinned to a node: the planner only checked that node/)).toBeTruthy();
  });
});

describe("planView helpers", () => {
  it("orders terms as the CLI does, unknown ones last by name", () => {
    const d = preV014();
    const c = d.candidates[0];
    if (c?.estimate == null) throw new Error("shape");
    c.estimate.terms = {
      zeta: { seconds: 1, source: "assumed" },
      risk: { seconds: 1, source: "assumed" },
      alpha: { seconds: 1, source: "assumed" },
      queue: { seconds: 0, source: "measured" },
    };
    expect(termNames(d)).toEqual(["queue", "risk", "alpha", "zeta"]);
  });

  it("formats ratio, rate and margin cells", () => {
    expect(ratioText(null)).toBe("—");
    expect(ratioText(1.234)).toBe("×1.23");
    expect(rateText({ node_id: "n", mode: "offload", feasible: true })).toBe("—");
    expect(marginText({ node_id: "n", mode: "split", feasible: false, reject_code: "split_too_small" })).toBe("split_too_small");
    expect(marginText({ node_id: "n", mode: "split", feasible: true })).toBe("—");
  });
});
