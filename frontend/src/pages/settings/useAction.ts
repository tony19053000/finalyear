/**
 * One settings action (create, revoke, delete, pin) with its state kept in
 * the component - NOT `useMutation`, whose cache keeps `variables` and the
 * answer: a password, a secret value, a join code or a new API token would
 * stay in memory after the dialog closed (D176/D199: never cached).
 */
import { useCallback, useEffect, useRef, useState } from "react";

export type ActionState<R> =
  | { status: "idle" }
  | { status: "pending" }
  | { status: "error"; error: unknown }
  | { status: "done"; result: R };

export interface Action<A extends unknown[], R> {
  state: ActionState<R>;
  pending: boolean;
  /** Runs the call; resolves with the answer, or `undefined` when it failed (see `state`). */
  run: (...args: A) => Promise<R | undefined>;
  /** Forget the last answer or error (closing a dialog calls it). */
  reset: () => void;
}

export function useAction<A extends unknown[], R>(call: (...args: A) => Promise<R>): Action<A, R> {
  const [state, setState] = useState<ActionState<R>>({ status: "idle" });
  const mounted = useRef(true);
  const busy = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      if (busy.current) return undefined; // a double click sends one request
      busy.current = true;
      setState({ status: "pending" });
      try {
        const result = await call(...args);
        if (mounted.current) setState({ status: "done", result });
        return result;
      } catch (error) {
        if (mounted.current) setState({ status: "error", error });
        return undefined;
      } finally {
        busy.current = false;
      }
    },
    [call],
  );

  const reset = useCallback(() => {
    setState({ status: "idle" });
  }, []);

  return { state, pending: state.status === "pending", run, reset };
}
