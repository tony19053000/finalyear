/**
 * Jobs list filters <-> the page URL (`/jobs?state=running&node=node_…`), so
 * a filtered view can be reloaded, bookmarked and shared. Ids and states
 * only - nothing secret ever goes into a URL (checklist 25).
 */
import { JOB_STATES, NO_FILTERS, TERMINAL_JOB_STATES, isJobState, type JobFilters, type JobState } from "./jobsApi";

export const ACTIVE_STATES: JobState[] = JOB_STATES.filter((s) => !TERMINAL_JOB_STATES.has(s));
export const FINISHED_STATES: JobState[] = JOB_STATES.filter((s) => TERMINAL_JOB_STATES.has(s));

/** The state <select>'s value for a set of states ("" = any). */
export function stateChoice(states: readonly JobState[]): string {
  if (states.length === 0) return "";
  const same = (group: readonly JobState[]) => group.length === states.length && group.every((s) => states.includes(s));
  if (same(ACTIVE_STATES)) return "active";
  if (same(FINISHED_STATES)) return "finished";
  return states.length === 1 ? (states[0] ?? "") : states.join(",");
}

export function statesFromChoice(choice: string): JobState[] {
  if (choice === "active") return [...ACTIVE_STATES];
  if (choice === "finished") return [...FINISHED_STATES];
  return choice.split(",").filter(isJobState);
}

export function filtersFromSearch(params: URLSearchParams): JobFilters {
  const states = params.getAll("state").filter(isJobState);
  return {
    states: [...new Set(states)],
    owner: params.get("owner") ?? "",
    node: params.get("node") ?? "",
    createdAfter: params.get("after") ?? "",
    createdBefore: params.get("before") ?? "",
  };
}

export function searchFromFilters(filters: JobFilters): URLSearchParams {
  const params = new URLSearchParams();
  for (const state of filters.states) params.append("state", state);
  if (filters.owner) params.set("owner", filters.owner);
  if (filters.node) params.set("node", filters.node);
  if (filters.createdAfter) params.set("after", filters.createdAfter);
  if (filters.createdBefore) params.set("before", filters.createdBefore);
  return params;
}

export function hasFilters(filters: JobFilters): boolean {
  return (
    filters.states.length > 0 ||
    filters.owner !== NO_FILTERS.owner ||
    filters.node !== NO_FILTERS.node ||
    filters.createdAfter !== "" ||
    filters.createdBefore !== ""
  );
}
