/**
 * The controller routes the Planner page's Models tab reads and drives
 * (V0.15 G3 D339/D358-D361, G5b D374). Typed from schema.d.ts.
 *
 * * `GET /v1/planner/ml/models` (any user): what the planner may use now
 *   (`auto` / `off` / `pinned`, the serving set, why it is not loaded, the
 *   dispatcher's live ML numbers), the newest sets and the retrain state.
 * * `GET /v1/planner/ml/models/{set}`: one set with its activation-gate
 *   document (held-out numbers per term, the rules it failed, regret).
 * * `GET /v1/planner/ml/predictions` (**admin**, D374): predicted vs actual
 *   per term - analytical, calibrated, ML - for the newest matched runs.
 * * `POST /v1/planner/ml/train`, `POST .../models/{set}/pin|unpin`
 *   (**admin**): start a retrain, override / roll back.
 *
 * A controller from before V0.15 G3 answers 404 on these routes: the tab says
 * "not available" (never fake data).
 */
import { apiRequest } from "../../api/client";
import { ApiError } from "../../api/errors";
import { queryKeys } from "../../api/queryKeys";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];
export type MlModelsOut = Schemas["MlModelsOut"];
export type MlInUseOut = Schemas["MlInUseOut"];
export type MlSetOut = Schemas["MlSetOut"];
export type MlSetDetailOut = Schemas["MlSetDetailOut"];
export type MlMemberOut = Schemas["MlMemberOut"];
export type MlRetrainStateOut = Schemas["MlRetrainStateOut"];
export type MlRetrainOut = Schemas["MlRetrainOut"];
export type MlTrainOut = Schemas["MlTrainOut"];
export type MlPredictionsOut = Schemas["MlPredictionsOut"];
export type MlPredictionPointOut = Schemas["MlPredictionPointOut"];
export type MlTerm = Schemas["MlTerm"];

/** Query keys of the Models tab (under the Planner prefix). */
export const mlKeys = {
  all: [...queryKeys.planner, "ml"] as const,
  models: [...queryKeys.planner, "ml", "models"] as const,
  set: (version: string) => [...queryKeys.planner, "ml", "set", version] as const,
  predictions: (limit: number) => [...queryKeys.planner, "ml", "predictions", limit] as const,
};

/** Newest sets in the history (the route's own default). */
export const ML_SETS_LIMIT = 20;
/** The predictions route's default and bound (D374). */
export const PREDICTIONS_LIMITS = [500, 2000] as const;
export type PredictionsLimit = (typeof PREDICTIONS_LIMITS)[number];

/** A model set version as the routes accept it (`sha256:<64 hex>`). */
export const SET_VERSION_RE = /^sha256:[0-9a-f]{64}$/;

export function isSetVersion(value: unknown): value is string {
  return typeof value === "string" && SET_VERSION_RE.test(value);
}

/** The path segment of a set version - refused unless it is one (no path injection). */
function setPath(setVersion: string): string {
  if (!isSetVersion(setVersion)) throw new Error("not a model set version");
  return setVersion;
}

/** A 404 from the list route: this controller has no model registry (pre-V0.15 G3). */
export function isMlUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** `GET /v1/planner/ml/models`. */
export function getMlModels(signal?: AbortSignal): Promise<MlModelsOut> {
  return apiRequest<MlModelsOut>("/v1/planner/ml/models", { signal, query: { limit: ML_SETS_LIMIT } });
}

/** `GET /v1/planner/ml/models/{set}`. */
export function getMlSet(setVersion: string, signal?: AbortSignal): Promise<MlSetDetailOut> {
  return apiRequest<MlSetDetailOut>(`/v1/planner/ml/models/${setPath(setVersion)}`, { signal });
}

/** `GET /v1/planner/ml/predictions?limit=` (admin). */
export function getMlPredictions(limit: PredictionsLimit, signal?: AbortSignal): Promise<MlPredictionsOut> {
  return apiRequest<MlPredictionsOut>("/v1/planner/ml/predictions", { signal, query: { limit } });
}

/** `POST /v1/planner/ml/train` (admin). */
export function startMlTrain(): Promise<MlTrainOut> {
  return apiRequest<MlTrainOut>("/v1/planner/ml/train", { method: "POST" });
}

/** `POST /v1/planner/ml/models/{set}/pin|unpin` (admin). */
export function pinMlSet(setVersion: string, pin: boolean): Promise<MlSetDetailOut> {
  return apiRequest<MlSetDetailOut>(
    `/v1/planner/ml/models/${setPath(setVersion)}/${pin ? "pin" : "unpin"}`,
    { method: "POST" },
  );
}
