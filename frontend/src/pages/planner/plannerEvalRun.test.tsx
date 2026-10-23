/**
 * V0.15 G6b (D378): evaluation reports are start-then-poll.
 *
 * * a running report shows its progress state (role=status) and the list a
 *   "running…" badge; the page re-reads it until it is done, then shows the
 *   report;
 * * a failed report says why (reason + detail), never an empty report;
 * * admins get **Run evaluation**: it POSTs `background: true` with the
 *   parsed campaign ids and selects the new report; while one runs the
 *   button says so and is disabled; a 409 is shown;
 * * users never see the form.
 * * G5 debt: `PREDICTIONS_LIMITS` equals the OpenAPI route's default and
 *   `maximum`; the Models tab says "N of M plotted" and explains points
 *   whose calibration version was not read.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ADMIN_SESSION, USER_SESSION, fakeController, json, renderApp, type Handler } from "../../test/fakeFabric";
import {
  ACTIVE_CALIBRATION,
  FAILED_RECORD,
  ONE_REPORT,
  REPORT_RECORD,
  RUNNING_RECORD,
  stateSummary,
} from "../../test/fixtures/evalReports";
import { GATE, PREDICTIONS, SET_A, SET_B, SET_C, detail, models } from "../../test/fixtures/mlModels";
import { PREDICTIONS_LIMITS } from "./mlApi";
import { calibrationGapText, plottedText, scatterOf } from "./mlView";
import { failedText, parseCampaignIds } from "./plannerApi";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function routes(admin: boolean, over: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session: admin ? ADMIN_SESSION : USER_SESSION }),
    "GET /v1/planner/calibration": () => json(200, ACTIVE_CALIBRATION),
    "GET /v1/planner/eval-reports": () => json(200, ONE_REPORT),
    "GET /v1/planner/eval-reports/4": () => json(200, REPORT_RECORD),
    ...over,
  };
}

describe("start-then-poll (D378)", () => {
  it("a running report shows progress, then the report once done", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let done = false;
    const runningAs6 = { ...RUNNING_RECORD };
    const doneAs6 = { ...REPORT_RECORD, id: 6 };
    const fake = fakeController(
      routes(false, {
        "GET /v1/planner/eval-reports": () =>
          json(200, { reports: [done ? { ...ONE_REPORT.reports[0], id: 6 } : stateSummary(runningAs6)] }),
        "GET /v1/planner/eval-reports/6": () => json(200, done ? doneAs6 : runningAs6),
      }),
    );
    renderApp("/planner?report=6");
    const progress = await screen.findByTestId("eval-report-running");
    expect(progress.getAttribute("role")).toBe("status");
    expect(progress.textContent).toContain("Evaluating camp_01HQ0000000000000000000000");
    expect(progress.textContent).toContain("checks every 3 s");
    const list = await screen.findByRole("table", { name: "Evaluation reports, newest first" });
    expect(within(list).getByText("running…")).toBeTruthy();
    expect(screen.queryByTestId("eval-report-detail")).toBeNull();

    const before = fake.count("GET /v1/planner/eval-reports/6");
    done = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_200);
    });
    await waitFor(() => {
      expect(fake.count("GET /v1/planner/eval-reports/6")).toBeGreaterThan(before);
    });
    expect(await screen.findByTestId("eval-report-detail")).toBeTruthy();
    expect(screen.queryByTestId("eval-report-running")).toBeNull();
  });

  it("a done report is not polled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fake = fakeController(routes(false));
    renderApp("/planner?report=4");
    await screen.findByTestId("eval-report-detail");
    const before = fake.count("GET /v1/planner/eval-reports/4");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fake.count("GET /v1/planner/eval-reports/4")).toBe(before);
  });

  it("a failed report says why", async () => {
    fakeController(
      routes(false, {
        "GET /v1/planner/eval-reports": () => json(200, { reports: [stateSummary(FAILED_RECORD)] }),
        "GET /v1/planner/eval-reports/7": () => json(200, FAILED_RECORD),
      }),
    );
    renderApp("/planner?report=7");
    const alert = await screen.findByTestId("eval-report-failed");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toContain("Report #7 failed: the controller stopped while it ran");
    expect(alert.textContent).toContain("(controller_restart)");
    expect(alert.textContent).toContain("run it again");
    const list = await screen.findByRole("table", { name: "Evaluation reports, newest first" });
    expect(within(list).getByText("failed: controller_restart")).toBeTruthy();
    expect(screen.queryByTestId("eval-report-detail")).toBeNull();
  });

  it("admins run an evaluation: background POST, the new report is selected", async () => {
    let posted: unknown = null;
    const fake = fakeController(
      routes(true, {
        "POST /v1/planner/eval-reports": (init) => {
          posted = typeof init.body === "string" ? JSON.parse(init.body) : null;
          return json(202, { report_id: 6, status: "running" });
        },
        "GET /v1/planner/eval-reports/6": () => json(200, RUNNING_RECORD),
      }),
    );
    renderApp("/planner");
    const form = await screen.findByRole("form", { name: "Run an evaluation" });
    const button = within(form).getByRole("button", { name: "Run evaluation" });
    expect((button as HTMLButtonElement).disabled).toBe(true); // no campaign yet
    await userEvent.type(within(form).getByLabelText(/Campaigns/), "camp_a, camp_b camp_a");
    await userEvent.type(within(form).getByLabelText(/Held-out family/), "cpu.sort");
    await userEvent.click(within(form).getByRole("button", { name: "Run evaluation" }));
    await waitFor(() => {
      expect(fake.count("POST /v1/planner/eval-reports")).toBe(1);
    });
    expect(posted).toEqual({ campaign_ids: ["camp_a", "camp_b"], holdout: "cpu.sort", allow_leak: false, background: true });
    expect(await screen.findByTestId("eval-report-running")).toBeTruthy();
  });

  it("while a report runs, Run evaluation shows it and is disabled", async () => {
    fakeController(
      routes(true, {
        "GET /v1/planner/eval-reports": () => json(200, { reports: [stateSummary(RUNNING_RECORD)] }),
        "GET /v1/planner/eval-reports/6": () => json(200, RUNNING_RECORD),
      }),
    );
    renderApp("/planner");
    const form = await screen.findByRole("form", { name: "Run an evaluation" });
    const busy = await within(form).findByRole("button", { name: /Report #6 running/ });
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("eval-running-note").textContent).toContain("One evaluation runs at a time");
  });

  it("a refused start is shown, not swallowed", async () => {
    fakeController(
      routes(true, {
        "POST /v1/planner/eval-reports": () =>
          json(409, {
            error: { code: "conflict", message: "an evaluation is already running on this controller", reason: "eval_already_running" },
          }),
      }),
    );
    renderApp("/planner");
    const form = await screen.findByRole("form", { name: "Run an evaluation" });
    await userEvent.type(within(form).getByLabelText(/Campaigns/), "camp_a");
    await userEvent.click(within(form).getByRole("button", { name: "Run evaluation" }));
    const alert = await within(form.parentElement as HTMLElement).findByRole("alert");
    expect(alert.textContent).toContain("already running");
  });

  it("users never see the form", async () => {
    fakeController(routes(false));
    renderApp("/planner");
    await screen.findByRole("table", { name: "Evaluation reports, newest first" });
    expect(screen.queryByRole("form", { name: "Run an evaluation" })).toBeNull();
  });

  it("pure helpers", () => {
    expect(parseCampaignIds(" a, b\nc ,, a ")).toEqual(["a", "b", "c"]);
    expect(parseCampaignIds("")).toEqual([]);
    expect(failedText("database")).toBe("the database failed");
    expect(failedText("brand_new")).toBe("brand_new");
    expect(failedText(null)).toBe("failed");
  });
});

/* ------------------------------------------------------------ G5 debt */

interface OpenApiParam {
  name: string;
  in: string;
  schema: { default?: number; maximum?: number };
}

describe("Models tab debt (V0.15 G6b)", () => {
  it("PREDICTIONS_LIMITS equals the route's OpenAPI default and maximum", () => {
    // vitest runs in frontend/ (the npm scripts' directory): openapi.json is the generated contract
    const doc = JSON.parse(readFileSync(resolve(process.cwd(), "openapi.json"), "utf-8")) as {
      paths: Record<string, { get: { parameters: OpenApiParam[] } }>;
    };
    const route = doc.paths["/v1/planner/ml/predictions"];
    expect(route).toBeDefined();
    const limit = route?.get.parameters.find((p) => p.name === "limit" && p.in === "query");
    expect(limit?.schema.default).toBe(PREDICTIONS_LIMITS[0]);
    expect(limit?.schema.maximum).toBe(PREDICTIONS_LIMITS[1]);
  });

  it("the scatter says N of M plotted and counts points without a calibrated value", () => {
    const first = PREDICTIONS.points[0];
    if (first === undefined) throw new Error("fixture has points");
    const points = [
      ...PREDICTIONS.points,
      { ...first, decision_id: 90, calibrated_s: null },
      { ...first, decision_id: 91, calibrated_s: null, ml_s: null, used: false },
    ];
    const s = scatterOf(points, "compute");
    const compute = points.filter((p) => p.term === "compute").length;
    expect(s.total).toBe(compute);
    expect(s.noCalibrated).toBe(2);
    expect(s.dropped).toBe(1); // decision 91: nothing to plot
    expect(plottedText(s)).toBe(`${String(compute - 1)} of ${String(compute)} point(s) of compute plotted`);
  });

  it("explains points whose calibration version was not read", () => {
    expect(calibrationGapText(PREDICTIONS)).toBeNull();
    expect(
      calibrationGapText({ calibration_versions_cap: 16, calibration_versions_skipped: 3, calibration_skipped_points: 5 }),
    ).toBe(
      "5 point(s) have no calibrated value: their decisions used one of 3 calibration version(s) older than the newest 16 this view reads, so they are not in the calibration series.",
    );
  });

  it("the Models tab shows the plotted count and the calibration gap", async () => {
    fakeController({
      "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
      "GET /v1/planner/ml/models": () => json(200, models()),
      [`GET /v1/planner/ml/models/${SET_A}`]: () => json(200, detail(SET_A, GATE)),
      [`GET /v1/planner/ml/models/${SET_B}`]: () => json(200, detail(SET_B, GATE)),
      [`GET /v1/planner/ml/models/${SET_C}`]: () => json(200, detail(SET_C, GATE)),
      "GET /v1/planner/ml/predictions": () =>
        json(200, { ...PREDICTIONS, calibration_versions_skipped: 2, calibration_skipped_points: 4 }),
    });
    renderApp("/planner?tab=models");
    const plotted = await screen.findByTestId("ml-plotted");
    expect(plotted.textContent).toMatch(/^\d+ of \d+ point\(s\) of compute plotted/);
    expect(screen.getByTestId("ml-calibration-gap").textContent).toContain(
      "4 point(s) have no calibrated value: their decisions used one of 2 calibration version(s) older than the newest 16",
    );
  });
});
