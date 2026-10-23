/**
 * V0.15 G5b tester round 1: the Models tab against hostile controller
 * answers - legacy `pending_g4` sets, a missing gate / regret block, an
 * empty registry that still names a serving set, a junk `?set=`, an unknown
 * set (404), 2000 points, a non-admin never reaching the predictions route.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ADMIN_SESSION,
  USER_SESSION,
  fakeController,
  json,
  renderApp,
  type Handler,
} from "../../test/fakeFabric";
import {
  ONE_REPORT,
  ACTIVE_CALIBRATION,
  REPORT_RECORD,
} from "../../test/fixtures/evalReports";
import {
  GATE,
  OLD_GATE,
  PREDICTIONS,
  SET_A,
  SET_B,
  SET_C,
  detail,
  models,
} from "../../test/fixtures/mlModels";
import type { MlPredictionsOut } from "./mlApi";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function routes(
  admin: boolean,
  over: Record<string, Handler> = {},
): Record<string, Handler> {
  return {
    "GET /v1/session": () =>
      json(200, { session: admin ? ADMIN_SESSION : USER_SESSION }),
    "GET /v1/planner/calibration": () => json(200, ACTIVE_CALIBRATION),
    "GET /v1/planner/eval-reports": () => json(200, ONE_REPORT),
    "GET /v1/planner/eval-reports/4": () => json(200, REPORT_RECORD),
    "GET /v1/planner/ml/models": () => json(200, models()),
    [`GET /v1/planner/ml/models/${SET_A}`]: () =>
      json(200, detail(SET_A, OLD_GATE)),
    [`GET /v1/planner/ml/models/${SET_B}`]: () =>
      json(200, detail(SET_B, GATE)),
    [`GET /v1/planner/ml/models/${SET_C}`]: () =>
      json(200, detail(SET_C, GATE)),
    "GET /v1/planner/ml/predictions": () => json(200, PREDICTIONS),
    ...over,
  };
}

async function open(
  over: Record<string, Handler> = {},
  path = "/planner?tab=models",
  admin = true,
) {
  const fabric = fakeController(routes(admin, over));
  renderApp(path);
  await screen.findByRole("tab", { name: "Models", selected: true });
  return fabric;
}

describe("Models tab: hostile gate documents", () => {
  it("legacy pending_g4 set: the page renders, says regret is not checked", async () => {
    await open({}, `/planner?tab=models&set=${SET_A}`);
    await waitFor(() => {
      expect(screen.getByTestId("ml-gate").textContent).toContain(
        "not checked",
      );
    });
  });

  it.each([
    [
      "no regret key",
      Object.fromEntries(
        Object.entries(GATE as Record<string, unknown>).filter(
          ([k]) => k !== "regret",
        ),
      ),
    ],
    ["regret null", { ...GATE, regret: null }],
    ["regret a list", { ...GATE, regret: [1, 2] }],
    ["gate empty", {}],
    ["terms junk", { ...GATE, terms: "x" }],
    ["gate a string", "junk"],
  ])("%s: no crash, a gate line is shown", async (_name, gate) => {
    await open({
      [`GET /v1/planner/ml/models/${SET_B}`]: () =>
        json(200, { ...detail(SET_B), gate }),
    });
    const line = await screen.findByTestId("ml-gate");
    expect(line.textContent.length).toBeGreaterThan(5);
    expect(line.textContent).not.toMatch(/undefined|NaN|\[object/);
  });
});

describe("Models tab: registry edge cases", () => {
  it("an empty registry that names no serving set: no model yet, no crash, no predictions needed", async () => {
    await open({
      "GET /v1/planner/ml/models": () =>
        json(
          200,
          models(
            { sets: [], serving_set: null },
            { in_use_set: null, kind: null },
          ),
        ),
    });
    expect(await screen.findByTestId("ml-in-use")).toBeTruthy();
    expect(screen.getByTestId("ml-in-use").textContent).toMatch(
      /no model set in use/i,
    );
  });

  it("a serving set missing from the history list does not crash the page", async () => {
    await open({
      "GET /v1/planner/ml/models": () => json(200, models({ sets: [] })),
    });
    expect(await screen.findByTestId("ml-in-use")).toBeTruthy();
  });

  it("a junk ?set= is ignored (no request for it)", async () => {
    const fabric = await open({}, "/planner?tab=models&set=../../etc/passwd");
    await screen.findByTestId("ml-in-use");
    expect(fabric.calls.some((c) => c.url.includes("etc"))).toBe(false);
  });

  it("an unknown set in the URL (404) leaves the rest of the tab usable", async () => {
    const unknown = `sha256:${"9".repeat(64)}`;
    await open(
      {
        [`GET /v1/planner/ml/models/${unknown}`]: () =>
          json(404, { error: { code: "not_found", message: "no such set" } }),
      },
      `/planner?tab=models&set=${unknown}`,
    );
    expect(await screen.findByTestId("ml-in-use")).toBeTruthy();
    expect(
      screen.getByRole("table", { name: "Model sets, newest first" }),
    ).toBeTruthy();
  });
});

describe("Models tab: predictions at the limit", () => {
  it("2000 points render with a counted aria-label and a collapsed table (not rendered until opened)", async () => {
    const points: MlPredictionsOut["points"] = Array.from(
      { length: 2000 },
      (_, i) => ({
        decision_id: i + 1,
        term: "compute",
        node_id: `n${i % 5}`,
        family: "matmul",
        analytical_s: 1 + (i % 50),
        calibrated_s: 1.2 + (i % 50),
        ml_s: i % 2 === 0 ? 1.1 + (i % 50) : null,
        used: i % 2 === 0,
        reason: i % 2 === 0 ? null : "ood",
        actual_s: 1.05 + (i % 50),
        ml_model_version: SET_B,
      }),
    );
    await open({
      "GET /v1/planner/ml/predictions": () =>
        json(200, {
          ...PREDICTIONS,
          limit: 2000,
          decisions_read: 2000,
          points,
        }),
    });
    const figure = await screen.findByTestId(
      "ml-scatter",
      {},
      { timeout: 5000 },
    );
    expect(
      within(figure).getByRole("img").getAttribute("aria-label"),
    ).toContain("2000 calibration points and 1000 ML points");
    expect(screen.queryByTestId("ml-points")).toBeNull(); // details closed: 2000 rows not mounted
    const all = screen.getByRole("table", {
      name: "How often ML acted, all terms",
    });
    expect(all.textContent).toContain("50.0 %");
  });

  it("a point whose term is not a known ML term still renders (selectable)", async () => {
    const odd = {
      ...PREDICTIONS,
      points: [
        ...PREDICTIONS.points,
        { ...PREDICTIONS.points[0], term: "future_term", decision_id: 99 },
      ],
    };
    await open({ "GET /v1/planner/ml/predictions": () => json(200, odd) });
    await screen.findByTestId("ml-scatter");
    const options = within(screen.getByRole("combobox", { name: "Term" }))
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toContain("future_term");
  });

  it("every point null/zero: nothing plotted, no crash", async () => {
    const dead = {
      ...PREDICTIONS,
      points: PREDICTIONS.points.map((p) => ({
        ...p,
        calibrated_s: null,
        ml_s: null,
        used: false,
        actual_s: 0,
      })),
    };
    await open({ "GET /v1/planner/ml/predictions": () => json(200, dead) });
    await screen.findByTestId("ml-in-use");
    expect(
      await screen.findByText(
        /can be plotted \(every value is missing or zero\)/,
      ),
    ).toBeTruthy();
    expect(screen.queryByTestId("ml-scatter")).toBeNull();
  });
});

describe("Models tab: non-admin", () => {
  it("never requests the admin-only predictions and shows no mutating controls", async () => {
    const fabric = await open({}, "/planner?tab=models", false);
    await screen.findByTestId("ml-in-use");
    expect(fabric.calls.some((c) => c.url.includes("/ml/predictions"))).toBe(
      false,
    );
    expect(screen.queryByRole("button", { name: /Train now/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Pin set/ })).toBeNull();
  });

  it("an admin whose predictions call is 403 (stale role) gets an error box, not a blank page", async () => {
    await open({
      "GET /v1/planner/ml/predictions": () =>
        json(403, { error: { code: "forbidden", message: "admin only" } }),
    });
    expect(
      await screen.findByText("Could not load predicted vs actual"),
    ).toBeTruthy();
    expect(screen.getByTestId("ml-in-use")).toBeTruthy();
  });
});

describe("Models tab: the userEvent import is used", () => {
  it("switching back to the calibration tab works", async () => {
    await open();
    await userEvent.click(screen.getByRole("tab", { name: /Calibration/ }));
    await screen.findByRole("table", { name: /Calibration factors/ });
  });
});
