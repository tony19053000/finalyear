import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "./errors";

/** Retry transient failures (network, 5xx, 429) twice; never a 4xx answer. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  if (error instanceof ApiError) {
    return error.status >= 500 || error.status === 429;
  }
  return true;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        // Live data arrives on the event stream (D172); refetch on focus as a
        // backstop, not on a timer.
        staleTime: 10_000,
        refetchOnWindowFocus: true,
      },
      mutations: { retry: false },
    },
  });
}
