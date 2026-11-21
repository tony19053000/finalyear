/**
 * The Planner page's queries, shared by the tabs and the headline figures
 * (one cache entry per route: the figures and a tab read the same answer).
 * Polling: a running evaluation and a running retrain are re-read until they
 * end (D378, G5b).
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { queryKeys } from "../../api/queryKeys";
import { getMlModels, mlKeys } from "./mlApi";
import { EVAL_POLL_MS, getCalibration, getEvalReport, listEvalReports } from "./plannerApi";

export const plannerKeys = {
  calibration: (version: number | null) => [...queryKeys.planner, "calibration", version ?? "active"] as const,
  reports: [...queryKeys.planner, "eval-reports"] as const,
  report: (id: number) => [...queryKeys.planner, "eval-report", id] as const,
};

/** While a retrain runs, the models list is re-read this often (ms). */
export const RETRAIN_POLL_MS = 3000;

/** `GET /v1/planner/calibration` - the active version (null) or one version. */
export function useCalibration(version: number | null) {
  return useQuery({
    queryKey: plannerKeys.calibration(version),
    queryFn: ({ signal }) => getCalibration(version, signal),
  });
}

/** The newest evaluation reports; re-read while one runs. */
export function useEvalReports() {
  return useQuery({
    queryKey: plannerKeys.reports,
    queryFn: ({ signal }) => listEvalReports(signal),
    refetchInterval: (q) => (q.state.data?.reports.some((r) => r.status === "running") === true ? EVAL_POLL_MS : false),
  });
}

/** One report (status, and once done the report); re-read while it runs. */
export function useEvalReport(id: number | null) {
  return useQuery({
    queryKey: plannerKeys.report(id ?? 0),
    queryFn: ({ signal }) => getEvalReport(id ?? 0, signal),
    enabled: id !== null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? EVAL_POLL_MS : false),
  });
}

/** The ML model sets and what is in use; re-read while a retrain runs. */
export function useMlModels() {
  return useQuery({
    queryKey: mlKeys.models,
    queryFn: ({ signal }) => getMlModels(signal),
    refetchInterval: (q) => (q.state.data?.retrain.running === true ? RETRAIN_POLL_MS : false),
  });
}

/** An element's width (px), followed with a ResizeObserver; `fallback` until measured (and in tests). */
export function useElementWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const measure = () => {
      const w = el.clientWidth;
      if (w > 0) setWidth(w);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      ro.disconnect();
    };
  }, []);
  return [ref, width] as const;
}
