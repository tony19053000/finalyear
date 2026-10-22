/**
 * V0.14 G5 (D300): the Planner page against a fake controller - calibration
 * factors and versions, evaluation reports (empty, one, leaked), a controller
 * without the evaluator vs an unknown report id (D321), criterion 2's paired
 * comparisons (D322), and the regret bars' numbers (the API's, never
 * recomputed). Eval-report answers are the typed D318 fixture.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { USER_SESSION, fakeController, json, renderApp, type Handler } from "../../test/fakeFabric";
import {
  ACTIVE_CALIBRATION,
  EMPTY_REPORTS,
  LEAKED_RECORD,
  NO_CALIBRATION,
  ONE_REPORT,
  REJECTED_V8,
  REPORT_RECORD,
  TWO_REPORTS,
} from "../../test/fixtures/evalReports";
import { UNKNOWN_EVAL_REPORT_REASON } from "./plannerApi";
import { ciLabel, pointsCi, regretBars, verdictText } from "./plannerView";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function routes(over: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session: USER_SESSION }),
    "GET /v1/planner/calibration": (_init, url) =>
      json(200, url.includes("version=8") ? REJECTED_V8 : ACTIVE_CALIBRATION),
    "GET /v1/planner/eval-reports": () => json(200, ONE_REPORT),
    "GET /v1/planner/eval-reports/4": () => json(200, REPORT_RECORD),
    "GET /v1/planner/eval-reports/5": () => json(200, LEAKED_RECORD),
    ...over,
  };
}

describe("Planner page", () => {
  it("is in the main navigation for every signed-in user", async () => {
    fakeController(routes());
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Planner" }).getAttribute("href")).toBe("/planner");
  });

  it("shows the active calibration's factors with n and key level, the versions and what is never scaled", async () => {
    fakeController(routes());
    renderApp("/planner");
    const factors = await screen.findByRole("table", { name: "Calibration factors of version 7" });
    const rows = within(factors).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[1]?.textContent).toContain("node_01JA00000000000000000000L2");
    expect(rows[1]?.textContent).toContain("×1.30");
    expect(rows[1]?.textContent).toContain("14");
    expect(rows[2]?.textContent).toContain("node_family");
    expect(rows[2]?.textContent).toContain("split_hash");
    expect(screen.getByTestId("calibration-in-use").textContent).toContain("in use v7");
    expect(screen.getByText(/Never scaled:/).textContent).toContain("risk (derived)");
    const versions = screen.getByRole("table", { name: "Calibration versions, newest first" });
    expect(versions.textContent).toContain("rejected");
    expect(versions.textContent).toContain("held-out error 0.310 > 1.05 x 0.112 (active v7)");
  });

  it("opens another calibration version from the history (and keeps it in the URL)", async () => {
    const fabric = fakeController(routes());
    renderApp("/planner");
    await screen.findByRole("table", { name: "Calibration factors of version 7" });
    await userEvent.click(screen.getByRole("button", { name: "v8" }));
    const factors = await screen.findByRole("table", { name: "Calibration factors of version 8" });
    expect(factors.textContent).toContain("×2.40");
    expect(fabric.calls.some((c) => c.url.includes("/v1/planner/calibration?version=8"))).toBe(true);
  });

  it("no calibration yet: says how one is made", async () => {
    fakeController(routes({ "GET /v1/planner/calibration": () => json(200, NO_CALIBRATION) }));
    renderApp("/planner");
    expect(await screen.findByText("No calibration yet")).toBeTruthy();
    expect(screen.getByTestId("calibration-in-use").textContent).toContain("in use none");
  });

  it("no reports: an empty state that names compute planner eval", async () => {
    fakeController(routes({ "GET /v1/planner/eval-reports": () => json(200, EMPTY_REPORTS) }));
    renderApp("/planner");
    expect(await screen.findByText("No evaluation reports yet")).toBeTruthy();
    expect(screen.getByText(/compute planner eval --campaign ID/)).toBeTruthy();
  });

  it("a controller without the evaluator (404): says so, no error box", async () => {
    fakeController(routes({ "GET /v1/planner/eval-reports": () => json(404, { error: { code: "not_found", message: "Not Found" } }) }));
    renderApp("/planner");
    expect(await screen.findByText("Evaluation reports are not available")).toBeTruthy();
    expect(screen.queryByText(/not found/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a proxy's non-envelope 404 on the list is also 'not available'", async () => {
    fakeController(routes({ "GET /v1/planner/eval-reports": () => new Response("gone", { status: 404 }) }));
    renderApp("/planner");
    expect(await screen.findByText("Evaluation reports are not available")).toBeTruthy();
  });

  it("an unknown report id (404 with the unknown-report reason): 'report not found', the list stays", async () => {
    fakeController(
      routes({
        "GET /v1/planner/eval-reports/99": () =>
          json(404, { error: { code: "not_found", message: "no evaluation report 99", reason: UNKNOWN_EVAL_REPORT_REASON } }),
      }),
    );
    renderApp("/planner?report=99");
    expect(await screen.findByText("Report #99 not found")).toBeTruthy();
    expect(screen.queryByText("Evaluation reports are not available")).toBeNull();
    expect(screen.getByRole("table", { name: "Evaluation reports, newest first" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a report route answering a reason-less 404 is 'not available', not 'not found'", async () => {
    fakeController(
      routes({ "GET /v1/planner/eval-reports/4": () => json(404, { error: { code: "not_found", message: "Not Found" } }) }),
    );
    renderApp("/planner");
    expect(await screen.findByText("Evaluation reports are not available")).toBeTruthy();
    expect(screen.queryByText(/not found/)).toBeNull();
  });

  it("criterion 2 (D322): the paired comparisons per rule policy, CI labelled with the report's level", async () => {
    fakeController(routes());
    renderApp("/planner");
    const detail = await screen.findByTestId("eval-report-detail");
    const paired = within(detail).getByRole("table", { name: "Criterion 2: paired regret difference per rule policy" });
    const headers = within(paired)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(["Rule policy", "Common groups", "Mean difference (% points)", "90 % CI", "Unmeasured"]);
    const rows = within(paired).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    const random = rows.find((r) => r.textContent.includes("random"));
    expect(random?.textContent).toContain("10");
    expect(random?.textContent).toContain("37.0");
    expect(random?.textContent).toContain("[25.5, 49.0]");
    expect(random?.textContent).toContain("2");
    const leastLoaded = rows.find((r) => r.textContent.includes("least_loaded"));
    expect(leastLoaded?.textContent).toContain("[-1.3, 15.5]");
    expect(detail.textContent).toContain("cost_model:calibrated:min_time");
    expect(detail.textContent).toContain("non-finite configurations dropped 1");
    // the policy table and the chart use the report's CI level too, never a fixed 95 %
    const policies = within(detail).getByRole("table", { name: "Regret per policy" });
    expect(within(policies).getByRole("columnheader", { name: "90 % CI" })).toBeTruthy();
    expect(within(detail).getByText("Mean regret per policy (bootstrap 90 % CI)")).toBeTruthy();
    expect(detail.textContent).not.toContain("95 % CI");
  });

  it("criterion 2 without comparisons (an older report): no paired table", async () => {
    const verdicts = REPORT_RECORD.report.verdicts.map((v) => {
      const older = { ...v };
      delete older.comparisons;
      return older;
    });
    const old = { ...REPORT_RECORD, report: { ...REPORT_RECORD.report, verdicts } };
    fakeController(routes({ "GET /v1/planner/eval-reports/4": () => json(200, old) }));
    renderApp("/planner");
    const detail = await screen.findByTestId("eval-report-detail");
    expect(within(detail).queryByRole("table", { name: "Criterion 2: paired regret difference per rule policy" })).toBeNull();
  });

  it("one report: verdicts, per-policy regret with CI (bars + table), families, calibrations used", async () => {
    fakeController(routes());
    renderApp("/planner");
    const detail = await screen.findByTestId("eval-report-detail");
    expect(within(detail).getByText("criterion 1: PASS")).toBeTruthy();
    expect(within(detail).getByText("criterion 2: not_separable")).toBeTruthy();
    expect(within(detail).queryByText("LEAKED")).toBeNull();
    const policies = within(detail).getByRole("table", { name: "Regret per policy" });
    const calibrated = within(policies)
      .getAllByRole("row")
      .find((r) => r.textContent.includes("cost_model:calibrated:min_time"));
    expect(calibrated?.textContent).toContain("4.5 %");
    expect(calibrated?.textContent).toContain("[1.3, 9.4] %");
    const noCounted = within(policies)
      .getAllByRole("row")
      .find((r) => r.textContent.includes("cost_model:calibrated:prefer_local"));
    expect(noCounted?.textContent).toContain("—");
    expect(within(detail).getByTestId("regret-chart")).toBeTruthy();
    expect(within(detail).getByRole("table", { name: "Regret per family and policy" }).textContent).toContain("split_hash");
    expect(within(detail).getByRole("table", { name: "Calibration used per family" }).textContent).toContain("refit");
    expect(detail.textContent).toContain("seed 20261022");
  });

  it("criterion 3 (D324): the decision-latency row from the report's block, per snapshot size", async () => {
    fakeController(routes());
    renderApp("/planner");
    const detail = await screen.findByTestId("eval-report-detail");
    expect(within(detail).getByText("criterion 3: PASS")).toBeTruthy();
    const row = within(detail).getByTestId("eval-latency");
    expect(row.textContent).toContain(
      "90 dispatched decisions timed (2 without a time) · p50 12.50 ms · p99 41.25 ms · max 133.75 ms · 1 over the 100 ms budget",
    );
    expect(row.textContent).toContain("3 nodes: 26 decisions, p99 9.50 ms");
    expect(row.textContent).toContain("4 nodes: 64 decisions, p99 41.25 ms");
    expect(row.textContent).toContain(
      "criterion 3 judges snapshots of ≥ 4 nodes: 64 decisions (at least 30 needed), p99 41.25 ms, PASS strictly below 100 ms",
    );
  });

  it("criterion 3: too few decisions, and a report stored before decision times", async () => {
    const verdicts = REPORT_RECORD.report.verdicts.map((v) =>
      v.criterion === 3 ? { ...v, status: "too_few_decisions" as const, value: null } : v,
    );
    const old = { ...REPORT_RECORD, report: { ...REPORT_RECORD.report, verdicts, latency: null } };
    fakeController(routes({ "GET /v1/planner/eval-reports/4": () => json(200, old) }));
    renderApp("/planner");
    const detail = await screen.findByTestId("eval-report-detail");
    expect(within(detail).getByText("criterion 3: too_few_decisions")).toBeTruthy();
    expect(within(detail).getByTestId("eval-latency").textContent).toContain("not recorded");
  });

  it("a leaked report: LEAKED banner, every verdict stamped leaked with what it would be", async () => {
    fakeController(routes({ "GET /v1/planner/eval-reports": () => json(200, TWO_REPORTS) }));
    renderApp("/planner");
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("LEAKED");
    expect(banner.textContent).toContain("not evidence");
    const detail = screen.getByTestId("eval-report-detail");
    expect(within(detail).getByText("criterion 1: leaked (would be PASS)")).toBeTruthy();
    expect(within(detail).getByRole("table", { name: "Calibration used per family" }).textContent).toContain("active v7");
    // the older, clean report is one click away
    await userEvent.click(screen.getByRole("button", { name: "#4" }));
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
    expect(screen.getByTestId("eval-report-detail").textContent).toContain("Report #4");
  });

  it("a report link opens that report", async () => {
    fakeController(routes({ "GET /v1/planner/eval-reports": () => json(200, TWO_REPORTS) }));
    renderApp("/planner?report=4");
    const detail = await screen.findByTestId("eval-report-detail");
    expect(detail.textContent).toContain("Report #4");
  });
});

describe("planner view helpers", () => {
  it("regret bars carry the API's CI as whisker lengths and skip policies without a mean", () => {
    const bars = regretBars(REPORT_RECORD.report.policies);
    expect(bars.map((b) => b.key)).not.toContain("cost_model:calibrated:prefer_local");
    const calibrated = bars.find((b) => b.key === "cost_model:calibrated:min_time");
    expect(calibrated?.y).toBe(4.5);
    expect(calibrated?.err[0]).toBeCloseTo(3.25);
    expect(calibrated?.err[1]).toBeCloseTo(4.9);
    const oracle = bars.find((b) => b.key === "oracle");
    expect(oracle?.err).toEqual([0, 0]);
  });

  it("CI label from the report's level; paired CI text", () => {
    expect(ciLabel(0.95)).toBe("95 % CI");
    expect(ciLabel(0.9)).toBe("90 % CI");
    expect(ciLabel(0.975)).toBe("97.5 % CI");
    expect(ciLabel(Number.NaN)).toBe("CI");
    expect(ciLabel(1)).toBe("CI");
    expect(pointsCi(-1.25, 15.5)).toBe("[-1.3, 15.5]");
    expect(pointsCi(null, 3)).toBe("—");
  });

  it("verdict text", () => {
    expect(verdictText({ criterion: 2, status: "leaked", would_be: "FAIL", value: null, threshold: null, counted_groups: 3, detail: "" })).toBe(
      "criterion 2: leaked (would be FAIL)",
    );
  });
});
