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
  // --- V0.8 G5 (Settings) -----------------------------------------------
  // Lists only: a join code, a new token, a password or a secret value is
  // never cached (api/settings.ts).
  invites: ["settings", "invites"] as const,
  /** `all`: every user's tokens (admin view). Prefix `["settings", "tokens"]` covers both. */
  tokens: (all: boolean) => ["settings", "tokens", all ? "all" : "mine"] as const,
  tokensAll: ["settings", "tokens"] as const,
  secrets: ["settings", "secrets"] as const,
  inputs: ["settings", "inputs"] as const,
  // --- V0.9 G6 (Benchmarks; admin only) -----------------------------------
  /** Prefix of every campaign entry (pages/benchmarks/benchKeys.ts). */
  bench: ["bench"] as const,
};
