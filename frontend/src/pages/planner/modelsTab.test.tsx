/**
 * V0.15 G5b (D374): the Planner page's Models tab against a fake controller -
 * the tab itself (URL `?tab=models`), every empty state (no registry, ML off,
 * no ml extra, no model yet), the set history and a set's per-term CV +
 * held-out numbers and gate line (incl. regret, old `pending_g4` rows), the
 * predicted-vs-actual scatter's points (= the fixture's), the fallback
 * counts, and the admin controls: pin / unpin / train need a confirmation,
 * a user sees a read-only tab and never asks for the admin-only predictions.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ADMIN_SESSION, USER_SESSION, fakeController, json, renderApp, type Handler } from "../../test/fakeFabric";
import { ONE_REPORT, ACTIVE_CALIBRATION, REPORT_RECORD } from "../../test/fixtures/evalReports";
import {
  GATE,
  OLD_GATE,
  PREDICTIONS,
  REJECTED_GATE,
  SET_A,
  SET_B,
  SET_C,
  detail,
  models,
} from "../../test/fixtures/mlModels";
import { scatterOf } from "./mlView";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function routes(admin: boolean, over: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session: admin ? ADMIN_SESSION : USER_SESSION }),
    "GET /v1/planner/calibration": () => json(200, ACTIVE_CALIBRATION),
    "GET /v1/planner/eval-reports": () => json(200, ONE_REPORT),
    "GET /v1/planner/eval-reports/4": () => json(200, REPORT_RECORD),
    "GET /v1/planner/ml/models": () => json(200, models()),
    [`GET /v1/planner/ml/models/${SET_A}`]: () => json(200, detail(SET_A, OLD_GATE)),
    [`GET /v1/planner/ml/models/${SET_B}`]: () => json(200, detail(SET_B, GATE)),
    [`GET /v1/planner/ml/models/${SET_C}`]: () => json(200, detail(SET_C, REJECTED_GATE)),
    "GET /v1/planner/ml/predictions": () => json(200, PREDICTIONS),
    ...over,
  };
}

async function openModels(admin = true, over: Record<string, Handler> = {}) {
  const fabric = fakeController(routes(admin, over));
  renderApp("/planner?tab=models");
  await screen.findByRole("tab", { name: "Models", selected: true });
  return fabric;
}

describe("Planner page: Models tab", () => {
  it("is a tab next to calibration & evaluation, kept in the URL", async () => {
    const fabric = fakeController(routes(true));
    renderApp("/planner");
    await screen.findByRole("table", { name: "Calibration factors of version 7" });
    expect(fabric.count("GET /v1/planner/ml/models")).toBe(0); // inactive tabs are not mounted
    await userEvent.click(screen.getByRole("tab", { name: "Models" }));
    expect(await screen.findByTestId("ml-in-use")).toBeTruthy();
    expect(fabric.count("GET /v1/planner/ml/models")).toBe(1);
  });

  it("shows the set in use, the live numbers and the retrain state", async () => {
    await openModels();
    const inUse = await screen.findByTestId("ml-in-use");
    expect(inUse.textContent).toContain("Planning with set bbbbbbbbbbbb (active, mode auto)");
    expect(inUse.textContent).toContain("last predict 1.23 ms (wall clock)");
    expect(inUse.textContent).toContain("predictor errors 2 since start");
    expect(inUse.textContent).toContain("No retrain yet.");
    expect(screen.getByText("ML in use")).toBeTruthy();
  });

  it("lists the sets with status and gate reason (incl. regret_worse)", async () => {
    await openModels();
    const table = await screen.findByRole("table", { name: "Model sets, newest first" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]?.textContent).toContain("rejected");
    expect(rows[0]?.textContent).toContain("regret_worse");
    expect(rows[1]?.textContent).toContain("active");
    expect(rows[1]?.textContent).toContain("compute=lightgbm");
  });

  it("shows the serving set's per-term CV and held-out numbers and the gate with its regret check", async () => {
    await openModels();
    const metrics = await screen.findByRole("table", { name: new RegExp(`Per-term numbers of set ${SET_B}`) });
    const rows = within(metrics).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.querySelector("td")?.textContent)).toEqual(["transfer", "compute"]); // compute explain order
    const compute = rows[1];
    expect(compute?.textContent).toContain("lightgbm");
    expect(compute?.textContent).toContain("0.123 ± 0.010");
    expect(compute?.textContent).toContain("0.091 / 0.201 / 0.201");
    expect(compute?.textContent).toContain("87.5 %");
    const transfer = rows[0];
    expect(transfer?.textContent).toContain("insufficient_data");
    const gate = screen.getByTestId("ml-gate").textContent;
    expect(gate).toContain("Gate accepted against calibration");
    expect(gate).toContain("passed - groups 14, candidate mean pct 4.500, current mean pct 6.250");
  });

  it("opens another set from the history (URL ?set=), a rejected gate and an old pending_g4 row", async () => {
    const fabric = await openModels();
    await screen.findByTestId("ml-set-detail");
    await userEvent.click(screen.getByRole("button", { name: `Show set ${SET_C}` }));
    await waitFor(() => {
      expect(screen.getByTestId("ml-gate").textContent).toContain("Gate rejected (regret_worse)");
    });
    expect(screen.getByTestId("ml-gate").textContent).toContain("rejected - groups 12 - candidate 12.0 % > current 8.0 % x 1.05");
    expect(fabric.calls.some((c) => c.key === `GET /v1/planner/ml/models/${SET_C}`)).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: `Show set ${SET_A}` }));
    await waitFor(() => {
      expect(screen.getByTestId("ml-gate").textContent).toContain("not checked (set made before the regret check existed)");
    });
  });

  it("a set in the URL is opened directly", async () => {
    fakeController(routes(true));
    renderApp(`/planner?tab=models&set=${SET_C}`);
    await waitFor(() => {
      expect(screen.getByTestId("ml-gate").textContent).toContain("regret_worse");
    });
  });

  it("scatter: the points are the fixture's (calibration for every point, ML where used), with a table fallback", async () => {
    await openModels();
    const figure = await screen.findByTestId("ml-scatter");
    expect(figure.textContent).toContain("compute: predicted vs actual (s, log axes)");
    expect(within(figure).getByRole("img").getAttribute("aria-label")).toContain("6 calibration points and 4 ML points");
    const summary = screen.getByText("Points as a table (6)");
    await userEvent.click(summary);
    const table = await screen.findByTestId("ml-points");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.querySelector("td")?.textContent)).toEqual(["#11", "#12", "#13", "#14", "#15", "#16"]);
    expect(rows[4]?.textContent).toContain("no (ood)");
    expect(rows[5]?.textContent).toContain("none");
  });

  it("scatter: another term; a point with a zero value is counted, not plotted", async () => {
    await openModels();
    await screen.findByTestId("ml-scatter");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Term" }), "env");
    const figure = await screen.findByTestId("ml-scatter");
    expect(within(figure).getByRole("img").getAttribute("aria-label")).toContain("1 calibration points and 0 ML points");
    expect(figure.textContent).toContain("1 point(s) not plotted");
  });

  it("fallback rate by reason over the listed decisions", async () => {
    await openModels();
    const term = await screen.findByRole("table", { name: "How often ML acted for compute" });
    const rows = within(term).getAllByRole("row").slice(1);
    expect(rows[0]?.textContent).toContain("ML used");
    expect(rows[0]?.textContent).toContain("66.7 %");
    expect(term.textContent).toContain("not used: out of distribution");
    expect(term.textContent).toContain("planned without a model set");
    const all = screen.getByRole("table", { name: "How often ML acted, all terms" });
    expect(all.textContent).toContain("not used: exact by construction");
    expect(all.textContent).toContain("50.0 %"); // 4 used of 8
  });

  it("only the serving set's decisions", async () => {
    await openModels();
    await screen.findByTestId("ml-scatter");
    await userEvent.click(screen.getByRole("checkbox", { name: /Only decisions planned with the serving set/ }));
    expect(screen.getByText(/· 6 points$/)).toBeTruthy(); // SET_B: compute #11 #12 #13 #15 + env #11 #12
  });

  it("reads 2000 runs when asked (the route's bound), never more", async () => {
    const fabric = await openModels();
    await screen.findByTestId("ml-scatter");
    expect(fabric.calls.find((c) => c.key === "GET /v1/planner/ml/predictions")?.url).toContain("limit=500");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Newest runs" }), "2000");
    await waitFor(() => {
      expect(fabric.calls.some((c) => c.url.includes("limit=2000"))).toBe(true);
    });
    const options = within(screen.getByRole("combobox", { name: "Newest runs" })).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["500", "2000"]);
  });

  it("no points yet: an empty state", async () => {
    await openModels(true, { "GET /v1/planner/ml/predictions": () => json(200, { ...PREDICTIONS, points: [], decisions_read: 0 }) });
    expect(await screen.findByText("No predicted-vs-actual points yet")).toBeTruthy();
  });

  it("predictions route down: an error with retry, the rest of the tab stays", async () => {
    await openModels(true, {
      "GET /v1/planner/ml/predictions": () => json(503, { error: { code: "network_error", message: "the database is unavailable; retry" } }),
    });
    expect(await screen.findByText("Could not load predicted vs actual")).toBeTruthy();
    expect(screen.getByTestId("ml-in-use")).toBeTruthy();
  });

  describe("empty states", () => {
    it("a controller without the registry (404): not available, no error box", async () => {
      fakeController(routes(true, { "GET /v1/planner/ml/models": () => json(404, { error: { code: "not_found", message: "Not Found" } }) }));
      renderApp("/planner?tab=models");
      expect(await screen.findByText("ML models are not available")).toBeTruthy();
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("ML off", async () => {
      await openModels(true, { "GET /v1/planner/ml/models": () => json(200, models({}, { mode: "off", in_use_set: null, kind: null })) });
      expect(await screen.findByText("ML off")).toBeTruthy();
      expect(screen.getByTestId("ml-in-use").textContent).toContain("ML is off: the planner uses the calibration only.");
    });

    it("the ml extra missing", async () => {
      await openModels(true, {
        "GET /v1/planner/ml/models": () =>
          json(200, models({ sets: [], serving_set: null }, { in_use_set: null, kind: null }, { available: false, missing: ["sklearn", "lightgbm"] })),
      });
      expect(await screen.findByText("ml extra missing")).toBeTruthy();
      const inUse = screen.getByTestId("ml-in-use").textContent;
      expect(inUse).toContain("missing sklearn, lightgbm");
      expect(inUse).toContain("uv sync --extra ml");
      expect(screen.getByRole<HTMLButtonElement>("button", { name: "Train now" }).disabled).toBe(true);
      expect(screen.getByText("No model set has been trained yet.")).toBeTruthy();
    });

    it("no model yet", async () => {
      await openModels(true, {
        "GET /v1/planner/ml/models": () => json(200, models({ sets: [], serving_set: null }, { in_use_set: null, kind: null })),
      });
      expect(await screen.findByText("no model yet")).toBeTruthy();
      expect(screen.getByTestId("ml-in-use").textContent).toContain("compute planner ml train");
      expect(screen.queryByTestId("ml-set-detail")).toBeNull();
    });

    it("the serving set not loaded", async () => {
      await openModels(true, {
        "GET /v1/planner/ml/models": () => json(200, models({}, { in_use_set: null, unavailable: "artefact_missing" })),
      });
      expect(await screen.findByText("set not loaded")).toBeTruthy();
      expect(screen.getByTestId("ml-in-use").textContent).toContain("is not loaded (artefact_missing)");
    });
  });

  describe("admin controls", () => {
    it("pin needs a confirmation; cancel sends nothing, confirm sends one POST", async () => {
      const fabric = await openModels(true, {
        [`POST /v1/planner/ml/models/${SET_A}/pin`]: () => json(200, { ...detail(SET_A), status: "pinned" }),
      });
      await screen.findByRole("table", { name: "Model sets, newest first" });
      expect(screen.queryByRole("button", { name: `Pin set ${SET_C}` })).toBeNull(); // rejected: never offered
      await userEvent.click(screen.getByRole("button", { name: `Pin set ${SET_A}` }));
      const dialog = await screen.findByRole("dialog", { name: "Pin set aaaaaaaaaaaa?" });
      await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      expect(fabric.count(`POST /v1/planner/ml/models/${SET_A}/pin`)).toBe(0);
      await userEvent.click(screen.getByRole("button", { name: `Pin set ${SET_A}` }));
      const again = await screen.findByRole("dialog", { name: "Pin set aaaaaaaaaaaa?" });
      await userEvent.click(within(again).getByRole("button", { name: "Pin" }));
      await waitFor(() => {
        expect(fabric.count(`POST /v1/planner/ml/models/${SET_A}/pin`)).toBe(1);
      });
      expect(await screen.findByText("Set aaaaaaaaaaaa is pinned.")).toBeTruthy();
      const csrf = new Headers(fabric.calls.find((c) => c.key.startsWith("POST"))?.init.headers).get("X-MeshForge-CSRF");
      expect(csrf).toBe("test-csrf");
    });

    it("unpin a pinned set (confirmation)", async () => {
      const pinned = models({ sets: [{ ...detail(SET_A), status: "pinned" }] }, { kind: "pinned", in_use_set: SET_A });
      const fabric = await openModels(true, {
        "GET /v1/planner/ml/models": () => json(200, pinned),
        [`POST /v1/planner/ml/models/${SET_A}/unpin`]: () => json(200, { ...detail(SET_A), status: "active" }),
      });
      await userEvent.click(await screen.findByRole("button", { name: `Unpin set ${SET_A}` }));
      const dialog = await screen.findByRole("dialog", { name: "Unpin set aaaaaaaaaaaa?" });
      expect(fabric.count(`POST /v1/planner/ml/models/${SET_A}/unpin`)).toBe(0);
      await userEvent.click(within(dialog).getByRole("button", { name: "Unpin" }));
      await waitFor(() => {
        expect(fabric.count(`POST /v1/planner/ml/models/${SET_A}/unpin`)).toBe(1);
      });
    });

    it("train needs a confirmation; a refusal (409) is a toast with the server's message", async () => {
      const fabric = await openModels(true, {
        "POST /v1/planner/ml/train": () =>
          json(409, { error: { code: "conflict", message: "this controller cannot train models: the ml extra is not installed" } }),
      });
      await userEvent.click(await screen.findByRole("button", { name: "Train now" }));
      const dialog = await screen.findByRole("dialog", { name: "Train a new model set now?" });
      expect(fabric.count("POST /v1/planner/ml/train")).toBe(0);
      await userEvent.click(within(dialog).getByRole("button", { name: "Train now" }));
      await waitFor(() => {
        expect(fabric.count("POST /v1/planner/ml/train")).toBe(1);
      });
      expect(await screen.findByText("Train refused")).toBeTruthy();
      expect(screen.getAllByText(/the ml extra is not installed/).length).toBeGreaterThan(0); // toast (+ its live-region copy)
    });

    it("a running retrain disables Train now", async () => {
      await openModels(true, {
        "GET /v1/planner/ml/models": () => json(200, models({}, {}, { running: true, running_since: "2026-10-23T10:00:00+00:00" })),
      });
      expect(await screen.findByText(/Retrain running since/)).toBeTruthy();
      expect(screen.getByRole<HTMLButtonElement>("button", { name: "Train now" }).disabled).toBe(true);
    });
  });

  describe("a user (not admin)", () => {
    it("sees the models read-only: no pin / unpin / train, and never asks for the admin-only predictions", async () => {
      const fabric = await openModels(false);
      await screen.findByRole("table", { name: "Model sets, newest first" });
      await screen.findByTestId("ml-set-detail");
      expect(screen.queryByRole("button", { name: /^Pin set/ })).toBeNull();
      expect(screen.queryByRole("button", { name: /^Unpin set/ })).toBeNull();
      expect(screen.queryByRole("button", { name: "Train now" })).toBeNull();
      expect(screen.getByText(/read-only view/)).toBeTruthy();
      expect(screen.getByText(/shown to admins only/)).toBeTruthy();
      expect(fabric.count("GET /v1/planner/ml/predictions")).toBe(0);
    });
  });
});

describe("scatterOf (pure)", () => {
  it("compute: calibration for every plottable point, ML only where used; one square log domain", () => {
    const s = scatterOf(PREDICTIONS.points, "compute");
    expect(s.calibration.map((p) => [p.x, p.y])).toEqual([
      [12, 9.2],
      [25, 17.5],
      [3.3, 3.1],
      [6, 4.8],
      [44, 70],
      [1.2, 1.1],
    ]);
    expect(s.ml.map((p) => [p.x, p.y])).toEqual([
      [9.5, 9.2],
      [18, 17.5],
      [2.9, 3.1],
      [5.1, 4.8],
    ]);
    expect(s.domain).toEqual([1.1, 70]);
    expect(s.dropped).toBe(0);
  });

  it("an ML value is never plotted when `used` is false", () => {
    const points = PREDICTIONS.points.map((p) => ({ ...p, ml_s: p.ml_s ?? 1, used: false }));
    expect(scatterOf(points, "compute").ml).toHaveLength(0);
  });
});
