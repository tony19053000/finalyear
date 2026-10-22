/**
 * V0.14 G5 tester r1: adversarial states of the Plan tab and the Planner page
 * (server errors, hostile strings, empty/degenerate answers).
 */
import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import contract from "./fixtures/explainContract.json";
import type { DecisionList, DecisionOut } from "../pages/jobs/jobsApi";
import { PlanTable } from "../components/jobs/PlanTable";
import { USER_SESSION, fakeController, json, renderApp, type Handler } from "./fakeFabric";
import { ACTIVE_CALIBRATION, ONE_REPORT, REPORT_RECORD } from "./fixtures/evalReports";
import { UNKNOWN_EVAL_REPORT_REASON } from "../pages/planner/plannerApi";

const FIXTURE = contract.payload as unknown as DecisionList;

function dec(i: number): DecisionOut {
  const d = structuredClone(FIXTURE.decisions[i]);
  if (d === undefined) throw new Error("fixture shape changed");
  return d;
}

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Plan tab, hostile and degenerate answers", () => {
  it("renders server strings as text, never as markup", () => {
    const d = dec(1);
    d.margin_sentence = '<img src=x onerror="window.__pwned=1"> split <b>over</b> 2';
    const first = d.candidates[0];
    if (first === undefined) throw new Error("fixture");
    first.reject_text = "<script>window.__pwned=1</script>";
    const { container } = render(<PlanTable decisions={[d]} pinned={false} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("a decision with an empty candidate list does not crash", () => {
    const d = dec(0);
    d.candidates = [];
    render(<PlanTable decisions={[d]} pinned={false} />);
    expect(screen.getByRole("region", { name: "Decision 21" })).toBeTruthy();
  });

  it("several attempts (retries) each get their own section with their own actuals", () => {
    const a = dec(0);
    const b = structuredClone(a);
    b.id = 22;
    b.attempt_no = 2;
    b.actual = null;
    for (const c of b.candidates) {
      for (const t of Object.values(c.estimate?.terms ?? {})) {
        t.actual_s = null;
        t.ratio = null;
      }
    }
    render(<PlanTable decisions={[a, b]} pinned={false} />);
    expect(screen.getByRole("region", { name: "Decision 21" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Decision 22" })).toBeTruthy();
    const second = screen.getByRole("region", { name: "Decision 22" });
    expect(within(second).queryByRole("table", { name: "Predicted vs actual of decision 22" })?.textContent ?? "").not.toMatch(/\d+\.\d+s.*×/);
  });

  it("a ratio of exactly 0 or a tiny actual is shown, null is a dash, never 'NaN'/'Infinity'", () => {
    const d = dec(0);
    const t = d.candidates[0]?.estimate?.terms.compute;
    if (t === undefined) throw new Error("fixture");
    t.actual_s = 0;
    t.ratio = 0;
    const { container } = render(<PlanTable decisions={[d]} pinned={false} />);
    expect(container.textContent).not.toMatch(/NaN|Infinity|undefined|null/);
  });

  it("the whole fixture (every state) never prints NaN/undefined/null", () => {
    const { container } = render(<PlanTable decisions={FIXTURE.decisions} pinned={false} />);
    expect(container.textContent).not.toMatch(/NaN|Infinity|undefined|\bnull\b/);
  });
});

function routes(over: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session: USER_SESSION }),
    "GET /v1/planner/calibration": () => json(200, ACTIVE_CALIBRATION),
    "GET /v1/planner/eval-reports": () => json(200, ONE_REPORT),
    "GET /v1/planner/eval-reports/4": () => json(200, REPORT_RECORD),
    ...over,
  };
}

describe("Planner page, failures", () => {
  it("calibration 500: an error with retry, the reports section still renders", async () => {
    fakeController(
      routes({ "GET /v1/planner/calibration": () => json(500, { error: { code: "internal", message: "boom" } }) }),
    );
    renderApp("/planner");
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(await screen.findByTestId("eval-report-detail")).toBeTruthy();
  });

  it("eval-reports 500 (not 404): an error box, not the 'not available' message", async () => {
    fakeController(
      routes({ "GET /v1/planner/eval-reports": () => json(500, { error: { code: "internal", message: "boom" } }) }),
    );
    renderApp("/planner");
    expect(await screen.findByText("Could not load evaluation reports")).toBeTruthy();
    expect(screen.queryByText("Evaluation reports are not available")).toBeNull();
  });

  // D321 (G4 + G5 merge): the controller's unknown-report 404 carries the
  // `unknown_eval_report` reason, so these say "not found" (not an error box).
  it("a listed report whose detail 404s (deleted meanwhile): 'not found', page survives", async () => {
    fakeController(
      routes({
        "GET /v1/planner/eval-reports/4": () =>
          json(404, { error: { code: "not_found", message: "gone", reason: UNKNOWN_EVAL_REPORT_REASON } }),
      }),
    );
    renderApp("/planner");
    expect(await screen.findByText("Report #4 not found")).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
  });

  it("an unknown ?report=999 does not crash the page", async () => {
    fakeController(
      routes({
        "GET /v1/planner/eval-reports/999": () =>
          json(404, { error: { code: "not_found", message: "no", reason: UNKNOWN_EVAL_REPORT_REASON } }),
      }),
    );
    renderApp("/planner?report=999");
    expect(await screen.findByText("Report #999 not found")).toBeTruthy();
  });
});
