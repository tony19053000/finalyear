/** TanStack Query hooks over endpoints.ts (D171): written by hand, typed by the schema. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { closeSession, getSession, getWhoAmI, listNodes, openSession, type SessionOut } from "./endpoints";
import { ApiError } from "./errors";
import { queryKeys } from "./queryKeys";

/**
 * The browser session: `data` is the session, `null` when not logged in
 * (the controller answers `{session: null}`; a 401 from an older controller
 * means the same). Other failures are errors.
 */
export function useSession() {
  return useQuery<SessionOut | null>({
    queryKey: queryKeys.session,
    queryFn: async ({ signal }) => {
      try {
        return (await getSession(signal)).session ?? null;
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

/** Lets the shell tell "the user is signing out" from "the session died". */
export const LOGOUT_MUTATION_KEY = ["logout"] as const;

/**
 * `DELETE /v1/session`. The caller leaves the shell first and then calls
 * `forgetUser()` - whatever the answer (a dead session is logged out anyway),
 * so the next user of this tab never sees cached data.
 */
export function useLogout() {
  const client = useQueryClient();
  const mutation = useMutation({ mutationKey: LOGOUT_MUTATION_KEY, mutationFn: closeSession });
  const forgetUser = () => {
    client.clear();
    client.setQueryData(queryKeys.session, null);
  };
  return { ...mutation, forgetUser };
}

export function useWhoAmI() {
  return useQuery({ queryKey: queryKeys.whoami, queryFn: ({ signal }) => getWhoAmI(signal) });
}

export function useNodes() {
  return useQuery({ queryKey: queryKeys.nodes, queryFn: ({ signal }) => listNodes(signal) });
}
