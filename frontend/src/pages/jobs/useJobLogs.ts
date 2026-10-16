import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { queryKeys } from "../../api/queryKeys";
import type { SocketLike } from "../../events/eventStream";
import { LogFollower, type LogSnapshot } from "./logFollower";

export const LOG_MAX_LINES = 5000;

export interface UseJobLogs {
  snapshot: LogSnapshot;
  pause: () => void;
  resume: () => void;
}

const INITIAL: LogSnapshot = {
  lines: [],
  dropped: 0,
  status: "connecting",
  end: null,
  offsets: { stdout: 0, stderr: 0 },
  detail: null,
};

/**
 * One log follower per job page (kept at page level so switching tabs does
 * not drop the buffer). A 4401 / refused handshake re-checks the session -
 * a dead session then leads to the login page through the shell.
 */
export function useJobLogs(jobId: string, socketFactory?: (url: string) => SocketLike): UseJobLogs {
  const client = useQueryClient();
  // Keyed by job: another job's snapshot is never shown, not even for a render.
  const [state, setState] = useState<{ jobId: string; snapshot: LogSnapshot }>({ jobId, snapshot: INITIAL });
  const follower = useRef<LogFollower | null>(null);

  useEffect(() => {
    const made = new LogFollower({
      jobId,
      socketFactory,
      maxLines: LOG_MAX_LINES,
      onUpdate: (snapshot) => {
        setState({ jobId, snapshot });
      },
      onUnauthorized: () => {
        void client.invalidateQueries({ queryKey: queryKeys.session });
      },
    });
    follower.current = made;
    made.start();
    return () => {
      made.stop();
      follower.current = null;
    };
  }, [jobId, socketFactory, client]);

  const pause = useCallback(() => follower.current?.pause(), []);
  const resume = useCallback(() => follower.current?.resume(), []);
  return { snapshot: state.jobId === jobId ? state.snapshot : INITIAL, pause, resume };
}
