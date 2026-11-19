/** The demo Planner answers every route the page reads and drives, with the project's numbers (demo mode only). */
import { describe, expect, it } from "vitest";

import type { MlModelsOut, MlPredictionsOut, MlSetDetailOut } from "../pages/planner/mlApi";
import type { CalibrationView, EvalReportListOut, EvalReportRecordOut, EvalStartOut } from "../pages/planner/plannerApi";
import { fallbackRows, termMetricRows } from "../pages/planner/mlView";
import { demoPlannerRoute, type DemoAnswer } from "./plannerDemoData";

const BASE = "http://localhost:5173";

function call(method: string, path: string, body?: unknown): DemoAnswer {
  const out = demoPlannerRoute(method, new URL(path, BASE), body === undefined ? undefined : JSON.stringify(body));
  if (out === undefined) throw new Error(`no demo answer for ${method} ${path}`);
  return out;
}

function get(path: string): unknown {
  const out = call("GET", path);
  expect(out.status).toBe(200);
  return out.body;
}

describe("demo planner: calibration", () => {
  it("the active version is within 15 % of measured, with a history of accepted and rejected fits", () => {
    const view = get("/v1/planner/calibration") as CalibrationView;
    expect(view.active_version).toBe(12);
    expect(view.in_use.in_use_version).toBe(12);
    const err = view.calibration?.holdout_error_new ?? 0;
    expect(Math.round((Math.exp(err) - 1) * 100)).toBe(15);
    expect(view.recent.length).toBeGreaterThanOrEqual(6);
    expect(new Set(view.recent.map((v) => v.status))).toEqual(new Set(["active", "superseded", "rejected"]));
    expect(view.calibration?.factors.length).toBeGreaterThan(10);
    expect(view.not_calibrated.risk).toBe("derived");
  });

  it("every listed version answers ?version=, an unknown one is a 404", () => {
    const view = get("/v1/planner/calibration") as CalibrationView;
    for (const v of view.recent) {
      const one = get(`/v1/planner/calibration?version=${String(v.version)}`) as CalibrationView;
      expect(one.calibration?.version).toBe(v.version);
      expect(one.calibration?.status).toBe(v.status);
      expect(one.calibration?.factors.length).toBeGreaterThan(0);
    }
    expect(call("GET", "/v1/planner/calibration?version=99").status).toBe(404);
  });
});

describe("demo planner: evaluation reports", () => {
  it("lists a pass / fail / leaked / failed mix, newest first; the newest says ~6 ms per decision", () => {
    const list = get("/v1/planner/eval-reports?limit=20") as EvalReportListOut;
    const created = list.reports.map((r) => Date.parse(r.created_at));
    expect([...created].sort((a, b) => b - a)).toEqual(created);
    expect(new Set(list.reports.map((r) => r.status))).toEqual(new Set(["done", "failed"]));
    const statuses = new Set(list.reports.flatMap((r) => r.verdicts.map((v) => v.status)));
    for (const s of ["PASS", "FAIL", "not_separable", "too_few_groups", "leaked"]) expect(statuses.has(s as never)).toBe(true);
    const newest = get(`/v1/planner/eval-reports/${String(list.reports[0]?.id)}`) as EvalReportRecordOut;
    expect(newest.report?.latency?.p50_ms).toBeCloseTo(6.1);
    expect(newest.report?.latency?.decisions).toBe(432);
    expect(newest.report?.verdicts.map((v) => v.status)).toEqual(["PASS", "PASS", "PASS"]);
    for (const r of list.reports) {
      const one = get(`/v1/planner/eval-reports/${String(r.id)}`) as EvalReportRecordOut;
      expect(one.status).toBe(r.status);
      if (one.status === "done") expect(one.report?.policies.length).toBe(7);
      else expect(one.reason).not.toBeNull();
    }
    const missing = call("GET", "/v1/planner/eval-reports/999");
    expect(missing.status).toBe(404);
    expect(missing.body).toMatchObject({ error: { reason: "unknown_eval_report" } });
  });

  it("run evaluation: 202, one at a time (409), done after a while; an unknown campaign fails", () => {
    const list = get("/v1/planner/eval-reports") as EvalReportListOut;
    const camp = list.reports[0]?.campaign_ids[0] ?? "";
    const started = call("POST", "/v1/planner/eval-reports", { campaign_ids: [camp], holdout: null, allow_leak: false, background: true });
    expect(started.status).toBe(202);
    const id = (started.body as EvalStartOut).report_id;
    expect((get(`/v1/planner/eval-reports/${String(id)}`) as EvalReportRecordOut).status).toBe("running");
    const again = call("POST", "/v1/planner/eval-reports", { campaign_ids: [camp], holdout: null, allow_leak: false });
    expect(again.status).toBe(409);
    expect(again.body).toMatchObject({ error: { reason: "eval_already_running", report_id: id } });
    expect(call("POST", "/v1/planner/eval-reports", { campaign_ids: [], holdout: null, allow_leak: false }).status).toBe(422);
  });
});

describe("demo planner: ML models", () => {
  it("an active set in use, a retired one, a rejected one, and a retrain running", () => {
    const models = get("/v1/planner/ml/models?limit=20") as MlModelsOut;
    const statuses = models.sets.map((s) => s.status);
    expect(statuses).toContain("active");
    expect(statuses).toContain("superseded");
    expect(statuses).toContain("rejected");
    expect(models.in_use.in_use_set).toBe(models.sets.find((s) => s.status === "active")?.set_version);
    expect(models.retrain.running).toBe(true);
    for (const s of models.sets) {
      const detail = get(`/v1/planner/ml/models/${s.set_version}`) as MlSetDetailOut;
      expect(termMetricRows(detail.members, detail.gate).length).toBe(8);
    }
  });

  it("pin / unpin move the set's status and refuse what the server refuses; train while running starts nothing", () => {
    const models = get("/v1/planner/ml/models") as MlModelsOut;
    const retired = models.sets.find((s) => s.status === "superseded")?.set_version ?? "";
    const rejected = models.sets.find((s) => s.status === "rejected")?.set_version ?? "";
    expect(call("POST", `/v1/planner/ml/models/${rejected}/pin`).status).toBe(409);
    expect(call("POST", `/v1/planner/ml/models/${retired}/unpin`).status).toBe(409);
    expect(call("POST", `/v1/planner/ml/models/${retired}/pin`).body).toMatchObject({ status: "pinned" });
    expect((get("/v1/planner/ml/models") as MlModelsOut).in_use).toMatchObject({ in_use_set: retired, kind: "pinned" });
    expect(call("POST", `/v1/planner/ml/models/${retired}/unpin`).body).toMatchObject({ status: "superseded" });
    expect((get("/v1/planner/ml/models") as MlModelsOut).in_use.kind).toBe("active");
    expect(call("POST", "/v1/planner/ml/train").body).toMatchObject({ started: false });
  });

  it("predictions: realistic error - calibration mostly within 15 %, ML tighter where used", () => {
    const out = get("/v1/planner/ml/predictions?limit=500") as MlPredictionsOut;
    expect(out.decisions_read).toBe(500);
    const bigger = get("/v1/planner/ml/predictions?limit=2000") as MlPredictionsOut;
    expect(bigger.decisions_read).toBeGreaterThan(500);
    const compute = out.points.filter((p) => p.term === "compute");
    const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
    const calErr = mean(compute.map((p) => Math.abs(Math.log((p.calibrated_s ?? 1) / p.actual_s))));
    const mlErr = mean(compute.filter((p) => p.used).map((p) => Math.abs(Math.log((p.ml_s ?? 1) / p.actual_s))));
    expect(calErr).toBeGreaterThan(0.1);
    expect(calErr).toBeLessThan(0.16);
    expect(mlErr).toBeLessThan(calErr);
    const used = fallbackRows(out.points, "compute").find((r) => r.key === "used");
    expect(used?.share).toBeGreaterThan(0.6);
  });

  it("is not a planner route: no answer", () => {
    expect(demoPlannerRoute("GET", new URL("/v1/nodes", BASE))).toBeUndefined();
  });
});
