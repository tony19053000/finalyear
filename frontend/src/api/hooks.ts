/** TanStack Query hooks over endpoints.ts (D171): written by hand, typed by the schema. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { closeSession, getSession, getWhoAmI, listNodes, openSession, type SessionOut } from "./endpoints";
import { ApiError } from "./errors";
import { queryKeys } from "./queryKeys";

/**
 * The browser session: `data` is the session, `null` when not logged in
 * (a 401 is an answer here, not an error). Other failures are errors.
 */
export function useSession() {
  return useQuery<SessionOut | null>({
    queryKey: queryKeys.session,
    queryFn: async ({ signal }) => {
      try {
        return await getSession(signal);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null;
        throw err;
      }
    },
    staleTime: 60_000,
  });
}

export function useLogin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ name, password }: { name: string; password: string }) => openSession(name, password),
    onSuccess: (session) => {
      // A new user may see other data: drop everything the last one loaded.
      client.clear();
      client.setQueryData(queryKeys.session, session);
    },
  });
}

export function useLogout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: closeSession,
    onSettled: () => {
      // Whatever the answer, this tab forgets the user (the cookies are
      // cleared by the controller; a dead session is logged out anyway).
      client.clear();
      client.setQueryData(queryKeys.session, null);
    },
  });
}

export function useWhoAmI() {
  return useQuery({ queryKey: queryKeys.whoami, queryFn: ({ signal }) => getWhoAmI(signal) });
}

export function useNodes() {
  return useQuery({ queryKey: queryKeys.nodes, queryFn: ({ signal }) => listNodes(signal) });
}
