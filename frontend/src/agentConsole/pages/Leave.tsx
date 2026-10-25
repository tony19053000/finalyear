import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/toastContext";
import { consoleApi, consoleKeys, explain, type StatusView } from "../api";
import { Problem } from "../components/fields";

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
    <Card title="Leave this fabric">
      <div className="flex flex-col gap-3 text-sm text-fg">
        <p>
          This laptop stops taking jobs at once, disconnects from the fabric
          {status.fabric_id !== null && status.fabric_id !== undefined ? ` ${status.fabric_id}` : ""} and forgets its key and
          certificate. Jobs running here now are stopped.
        </p>
        <p className="text-fg-muted">
          To come back later you need a new join code; the laptop then appears as a new node. Your settings on this laptop are kept.
          The fabric admin may also remove the old entry from the dashboard.
        </p>
        <div>
          <Button variant="danger" onClick={() => { setOpen(true); }}>
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
    </Card>
  );
}
