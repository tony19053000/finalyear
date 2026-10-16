import { useMutation, useQueryClient } from "@tanstack/react-query";

import { describeError } from "../../api/errors";
import { Button } from "../Button";
import { Dialog } from "../Dialog";
import { useToast } from "../toastContext";
import { jobKeys } from "../../pages/jobs/jobKeys";
import { cancelJob, type JobDetail } from "../../pages/jobs/jobsApi";

export interface CancelJobDialogProps {
  job: Pick<JobDetail, "job_id" | "name" | "state">;
  open: boolean;
  onClose: () => void;
}

/**
 * Confirm + cancel (POST /v1/jobs/{id}/cancel). The server is idempotent:
 * 202 = the agent is told to stop the running attempt; 200 = it had already
 * ended. Any refusal (409 conflict, 404, 403, a network error) stays in the
 * dialog with the server's reason, so the user knows what happened.
 */
export function CancelJobDialog({ job, open, onClose }: CancelJobDialogProps) {
  const client = useQueryClient();
  const toast = useToast();
  const mutation = useMutation({
    mutationFn: () => cancelJob(job.job_id),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: jobKeys.detail(job.job_id) });
      void client.invalidateQueries({ queryKey: jobKeys.lists });
      toast(
        result.cancel_requested
          ? { title: "Cancel requested", description: `${job.name}: the node is stopping the job.`, tone: "ok" }
          : { title: "Job already ended", description: `${job.name} is ${result.state}.`, tone: "info" },
      );
      onClose();
    },
  });
  const error = mutation.isError ? describeError(mutation.error) : null;
  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!mutation.isPending) {
          mutation.reset();
          onClose();
        }
      }}
      title="Cancel this job?"
      description={`"${job.name}" (${job.job_id}) is ${job.state}. A running attempt is stopped; partial outputs are not kept.`}
      footer={
        <>
          <Button
            onClick={() => {
              mutation.reset();
              onClose();
            }}
            disabled={mutation.isPending}
          >
            Keep running
          </Button>
          <Button
            variant="danger"
            loading={mutation.isPending}
            onClick={() => {
              mutation.mutate();
            }}
          >
            Cancel job
          </Button>
        </>
      }
    >
      {error !== null && (
        <p role="alert" className="text-sm text-status-failed">
          Could not cancel: {error.message}
          {error.code !== undefined && <span className="ml-1 font-mono text-2xs text-fg-muted">({error.code})</span>}
        </p>
      )}
    </Dialog>
  );
}
