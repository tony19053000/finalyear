/**
 * V0.15 G6b tester round 1 (from brief 5h D378 + debt list): the Planner
 * page's start-then-poll states under hostile / racing controller answers,
 * and the Models tab "N of M plotted" arithmetic on odd points.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ADMIN_SESSION,
  fakeController,
  json,
  renderApp,
  type Handler,
} from "../../test/fakeFabric";
import {
  ACTIVE_CALIBRATION,
  FAILED_RECORD,
  ONE_REPORT,
  REPORT_RECORD,
  RUNNING_RECORD,
  stateSummary,
} from "../../test/fixtures/evalReports";
import {
  GATE,
  PREDICTIONS,
  SET_A,
  SET_B,
  SET_C,
  detail,
  models,
} from "../../test/fixtures/mlModels";
import type { MlPredictionPointOut } from "./mlApi";
import { calibrationGapText, plottedText, scatterOf } from "./mlView";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function routes(over: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
    "GET /v1/planner/calibration": () => json(200, ACTIVE_CALIBRATION),
    "GET /v1/planner/eval-reports": () => json(200, ONE_REPORT),
    "GET /v1/planner/eval-reports/4": () => json(200, REPORT_RECORD),
    ...over,
  };
}

describe("Planner page: start-then-poll under races", () => {
  // defect T5 (minor): it.fails = known defect; remove `.fails` when fixed
  it.fails(
    "a 409 (another admin already started one) refreshes the list so the running report is shown",
    async () => {
      let started = false;
      const fake = fakeController(
        routes({
          "GET /v1/planner/eval-reports": () =>
            json(200, {
              reports: started
                ? [stateSummary(RUNNING_RECORD)]
                : ONE_REPORT.reports,
            }),
          "GET /v1/planner/eval-reports/6": () => json(200, RUNNING_RECORD),
          "POST /v1/planner/eval-reports": () => {
            started = true; // someone else's evaluation began meanwhile
            return json(409, {
              error: {
                code: "conflict",
                message: "an evaluation is already running on this controller",
                reason: "eval_already_running",
                report_id: 6,
              },
            });
          },
        }),
      );
      renderApp("/planner");
      const form = await screen.findByRole("form", {
        name: "Run an evaluation",
      });
      await userEvent.type(within(form).getByLabelText(/Campaigns/), "camp_a");
      const before = fake.count("GET /v1/planner/eval-reports");
      await userEvent.click(
        within(form).getByRole("button", { name: "Run evaluation" }),
      );
      await waitFor(() => {
        expect(fake.count("POST /v1/planner/eval-reports")).toBe(1);
      });
      // the refused start must not leave a stale list: the running report appears
      await waitFor(
        () => {
          expect(fake.count("GET /v1/planner/eval-reports")).toBeGreaterThan(
            before,
          );
        },
        { timeout: 1500 },
      );
      expect(await screen.findByTestId("eval-running-note")).toBeTruthy();
    },
  );

  // defect T6 (minor): after a 202 the button re-enables until the list shows the running row
  it.fails("double-clicking Run evaluation sends one POST", async () => {
    const fake = fakeController(
      routes({
        "POST /v1/planner/eval-reports": () =>
          json(202, { report_id: 6, status: "running" }),
        "GET /v1/planner/eval-reports/6": () => json(200, RUNNING_RECORD),
      }),
    );
    renderApp("/planner");
    const form = await screen.findByRole("form", { name: "Run an evaluation" });
    await userEvent.type(within(form).getByLabelText(/Campaigns/), "camp_a");
    const button = within(form).getByRole("button", { name: "Run evaluation" });
    const user = userEvent.setup({ delay: 30 }); // a human's double click is not instantaneous
    await user.dblClick(button);
    await screen.findByTestId("eval-report-running");
    expect(fake.count("POST /v1/planner/eval-reports")).toBe(1);
  });

  it("more than 20 campaigns disables the button and Enter sends nothing", async () => {
    const fake = fakeController(routes());
    renderApp("/planner");
    const form = await screen.findByRole("form", { name: "Run an evaluation" });
    const ids = Array.from({ length: 21 }, (_, i) => `c${String(i)}`).join(" ");
    await userEvent.type(
      within(form).getByLabelText(/Campaigns/),
      `${ids}{Enter}`,
    );
    expect(
      within(form).getByRole<HTMLButtonElement>("button", {
        name: "Run evaluation",
      }).disabled,
    ).toBe(true);
    expect(fake.count("POST /v1/planner/eval-reports")).toBe(0);
    expect(screen.getByText(/At most 20 campaigns/)).toBeTruthy();
  });

  it("running -> failed while watching: the panel switches and polling stops", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let failed = false;
    const fake = fakeController(
      routes({
        "GET /v1/planner/eval-reports": () =>
          json(200, {
            reports: [
              stateSummary(
                failed ? { ...FAILED_RECORD, id: 6 } : RUNNING_RECORD,
              ),
            ],
          }),
        "GET /v1/planner/eval-reports/6": () =>
          json(200, failed ? { ...FAILED_RECORD, id: 6 } : RUNNING_RECORD),
      }),
    );
    renderApp("/planner?report=6");
    await screen.findByTestId("eval-report-running");
    failed = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_200);
    });
    await screen.findByTestId("eval-report-failed");
    const n = fake.count("GET /v1/planner/eval-reports/6");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(fake.count("GET /v1/planner/eval-reports/6")).toBe(n);
    expect(fake.count("GET /v1/planner/eval-reports")).toBeLessThan(10);
  });

  it("a poll that fails (503) keeps the running view and recovers", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    fakeController(
      routes({
        "GET /v1/planner/eval-reports": () =>
          json(200, { reports: [stateSummary(RUNNING_RECORD)] }),
        "GET /v1/planner/eval-reports/6": () => {
          n += 1;
          if (n === 2)
            return json(503, {
              error: {
                code: "network_error",
                message: "the database is unavailable; try again",
              },
            });
          return json(200, n > 3 ? REPORT_RECORD : RUNNING_RECORD);
        },
      }),
    );
    renderApp("/planner?report=6");
    await screen.findByTestId("eval-report-running");
    for (let i = 0; i < 6; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_200);
      });
    }
    await screen.findByTestId("eval-report-detail", undefined, {
      timeout: 3000,
    });
  });

  it("an unknown status or a done report without a body does not crash the page", async () => {
    fakeController(
      routes({
        "GET /v1/planner/eval-reports": () =>
          json(200, {
            reports: [{ ...stateSummary(RUNNING_RECORD), status: "queued" }],
          }),
        "GET /v1/planner/eval-reports/6": () =>
          json(200, { ...RUNNING_RECORD, status: "done", report: null }),
      }),
    );
    renderApp("/planner?report=6");
    await screen.findByRole("table", {
      name: "Evaluation reports, newest first",
    });
    await waitFor(() => {
      expect(
        screen.queryByText(
          /Something went wrong|Unexpected Application Error/i,
        ),
      ).toBeNull();
    });
  });

  it("a failed report's detail is rendered as text, never as markup", async () => {
    const evil = {
      ...FAILED_RECORD,
      reason: "crashed",
      detail: '<img src=x onerror="window.__pwned=1">',
    };
    fakeController(
      routes({
        "GET /v1/planner/eval-reports": () =>
          json(200, { reports: [stateSummary(evil)] }),
        "GET /v1/planner/eval-reports/7": () => json(200, evil),
      }),
    );
    renderApp("/planner?report=7");
    const alert = await screen.findByTestId("eval-report-failed");
    expect(alert.querySelector("img")).toBeNull();
    expect(alert.textContent).toContain("<img src=x");
  });
});

/* ------------------------------------------------------------ plotted arithmetic */

function pt(over: Partial<MlPredictionPointOut>): MlPredictionPointOut {
  return {
    decision_id: 1,
    term: "compute",
    node_id: "n",
    family: "f",
    analytical_s: 1,
    calibrated_s: 1,
    ml_s: null,
    used: false,
    reason: null,
    actual_s: 1,
    ml_model_version: null,
    ...over,
  };
}

describe("N of M plotted", () => {
  it("no points: 0 of 0, no domain", () => {
    const s = scatterOf([], "compute");
    expect(plottedText(s)).toBe("0 of 0 point(s) of compute plotted");
    expect(s.domain).toBeNull();
  });

  it("counts points, not markers; ML-only points count as plotted", () => {
    const s = scatterOf(
      [
        pt({ decision_id: 1, ml_s: 2, used: true }), // both series, one point
        pt({ decision_id: 2, calibrated_s: null, ml_s: 3, used: true }), // ML only
        pt({ decision_id: 3, calibrated_s: null }), // nothing
        pt({ decision_id: 4, actual_s: 0 }), // zero actual
        pt({ decision_id: 5, actual_s: -1 }),
        pt({ decision_id: 6, calibrated_s: Number.NaN }),
        pt({ decision_id: 7, calibrated_s: Number.POSITIVE_INFINITY }),
        pt({ decision_id: 8, term: "env" }), // other term
      ],
      "compute",
    );
    expect(s.total).toBe(7);
    expect(s.dropped).toBe(5);
    expect(plottedText(s)).toBe("2 of 7 point(s) of compute plotted");
    expect(s.noCalibrated).toBe(2);
    expect(s.calibration.length + 0).toBe(1);
    expect(s.ml.length).toBe(2);
  });

  it("a beyond-MAX_Y_HAT point (used, ml_s null) stays plotted by its calibration and has no ML marker", () => {
    const s = scatterOf([pt({ ml_s: null, used: true })], "compute");
    expect(s.ml.length).toBe(0);
    expect(s.calibration.length).toBe(1);
    expect(plottedText(s)).toBe("1 of 1 point(s) of compute plotted");
  });

  it("total - dropped never exceeds total or goes negative on 2000 random points", () => {
    const pts: MlPredictionPointOut[] = [];
    for (let i = 0; i < 2000; i++) {
      pts.push(
        pt({
          decision_id: i,
          calibrated_s: i % 3 === 0 ? null : i % 7 === 0 ? 0 : i + 1,
          ml_s: i % 5 === 0 ? i + 2 : null,
          used: i % 5 === 0,
          actual_s: i % 11 === 0 ? 0 : i + 1,
        }),
      );
    }
    const s = scatterOf(pts, "compute");
    expect(s.total).toBe(2000);
    const plotted = s.total - s.dropped;
    expect(plotted).toBeGreaterThanOrEqual(0);
    const ids = new Set([...s.calibration, ...s.ml].map((p) => p.decisionId));
    expect(ids.size).toBe(plotted);
  });
});

describe("calibration gap text", () => {
  it.each([0, -1])("says nothing for %s skipped points", (n) => {
    expect(
      calibrationGapText({
        calibration_versions_cap: 16,
        calibration_versions_skipped: 3,
        calibration_skipped_points: n,
      }),
    ).toBeNull();
  });
  it("names the cap and the counts", () => {
    const t = calibrationGapText({
      calibration_versions_cap: 16,
      calibration_versions_skipped: 1,
      calibration_skipped_points: 1,
    });
    expect(t).toContain("1 point(s)");
    expect(t).toContain("newest 16");
  });
});

describe("Models tab with 2000 points", () => {
  it("states N of M for the shown term and the gap", async () => {
    const points = Array.from({ length: 2000 }, (_, i) =>
      pt({
        decision_id: i,
        calibrated_s: i % 4 === 0 ? null : i + 1,
        actual_s: i + 1,
      }),
    );
    fakeController({
      "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
      "GET /v1/planner/ml/models": () => json(200, models()),
      [`GET /v1/planner/ml/models/${SET_A}`]: () =>
        json(200, detail(SET_A, GATE)),
      [`GET /v1/planner/ml/models/${SET_B}`]: () =>
        json(200, detail(SET_B, GATE)),
      [`GET /v1/planner/ml/models/${SET_C}`]: () =>
        json(200, detail(SET_C, GATE)),
      "GET /v1/planner/ml/predictions": () =>
        json(200, {
          ...PREDICTIONS,
          limit: 2000,
          calibration_versions_skipped: 1,
          calibration_skipped_points: 500,
          points,
        }),
    });
    renderApp("/planner?tab=models");
    const plotted = await screen.findByTestId("ml-plotted", undefined, {
      timeout: 10000,
    });
    expect(plotted.textContent).toContain(
      "1500 of 2000 point(s) of compute plotted",
    );
    expect(plotted.textContent).toContain("500 without a calibrated value");
  });
});
