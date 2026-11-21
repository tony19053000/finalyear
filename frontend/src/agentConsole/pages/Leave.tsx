import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/toastContext";
import { consoleApi, consoleKeys, explain, type StatusView } from "../api";
import { Panel, Problem } from "../components/fields";

export function Leave({ status }: { status: StatusView }) {
  const client = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const leave = useMutation({
    mutationFn: consoleApi.leave,
    onSuccess: (r) => {
      setOpen(false);
      toast({ title: r.message, tone: "ok" });
      void client.invalidateQueries({ queryKey: consoleKeys.status });
      void navigate("/");
    },
  });
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-8">
    <div>
      <p className="nd-label">Leave</p>
      <p className="mt-3 font-display text-[3rem] leading-none font-bold tracking-[-0.04em] text-fg">Leave the hive</p>
    </div>
    <Panel title="Leave this fabric" className="ac-danger">
      <div className="flex flex-col gap-6">
        <p className="ac-lead">
          This laptop stops taking jobs at once, disconnects from the fabric
          {status.fabric_id !== null && status.fabric_id !== undefined ? ` ${status.fabric_id}` : ""} and forgets its key and
          certificate. Jobs running here now are stopped.
        </p>
        <p className="ac-row-help">
          To come back later you need a new join code; the laptop then appears as a new node. Your settings on this laptop are kept.
          The fabric admin may also remove the old entry from the dashboard.
        </p>
        <div>
          <Button variant="danger" className="ac-go" onClick={() => { setOpen(true); }}>
            Leave the fabric…
          </Button>
        </div>
      </div>
      <Dialog
        open={open}
        onClose={() => { setOpen(false); }}
        title="Leave the fabric?"
        description="This laptop forgets its fabric identity. This cannot be undone."
        footer={
          <>
            <Button onClick={() => { setOpen(false); }}>Cancel</Button>
            <Button variant="danger" loading={leave.isPending} onClick={() => { leave.mutate(); }}>
              Leave
            </Button>
          </>
        }
      >
        {leave.isError && <Problem title="Could not leave" {...explain(leave.error)} />}
      </Dialog>
    </Panel>
    </div>
  );
}
