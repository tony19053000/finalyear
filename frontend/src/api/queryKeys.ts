/**
 * Every TanStack Query key in one place. The event stream (D172) invalidates
 * by these prefixes, so a page's data and its live updates cannot drift apart.
 */
export const queryKeys = {
  session: ["session"] as const,
  whoami: ["whoami"] as const,
  nodes: ["nodes"] as const,
  node: (nodeId: string) => ["nodes", nodeId] as const,
  nodeMetrics: (nodeId: string) => ["nodes", nodeId, "metrics"] as const,
  jobs: ["jobs"] as const,
  job: (jobId: string) => ["jobs", jobId] as const,
  // --- V0.8 G3 (Overview, Nodes) ---------------------------------------
  nodeResources: (nodeId: string) => ["nodes", nodeId, "resources"] as const,
  network: ["network"] as const,
  alerts: ["alerts"] as const,
  health: ["health"] as const,
  /** The Overview's active-job counts (not under "jobs": no clash with job ids). */
  activeJobs: ["overview", "active-jobs"] as const,
};
