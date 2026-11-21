/**
 * A destructive action behind a confirm dialog (checklist item 3): the
 * project's native <dialog> (G2 deviation 1), Escape cancels, focus starts on
 * Cancel (the first control) and returns to the trigger on close. A refusal
 * from the controller is shown in the dialog; success closes it and toasts.
 *
 * A hook, not a button: the trigger is an item of a row's "⋯" menu, which
 * closes when chosen, so the dialog (and its state) lives beside the menu.
 */
import { useState, type ReactNode } from "react";

import { describeError } from "../../api/errors";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/toastContext";
import { useAction } from "./useAction";

export interface ConfirmActionProps<R> {
  title: string;
  description: ReactNode;
  confirmLabel: string;
  /** The call; its answer goes to `done`. */
  action: () => Promise<R>;
  /** Toast text after success (and anything else to do, e.g. refetch). */
  done: (result: R) => { title: string; description?: string };
  /** A non-destructive confirm (e.g. "Pin") uses the primary style. */
  destructive?: boolean;
}

/** `open()` shows the confirm dialog; render `dialog` next to the trigger. */
export function useConfirmAction<R>({
  title,
  description,
  confirmLabel,
  action,
  done,
  destructive = true,
}: ConfirmActionProps<R>): { open: () => void; dialog: ReactNode } {
  const [open, setOpen] = useState(false);
  const call = useAction(action);
  const toast = useToast();

  const close = () => {
    if (call.pending) return;
    setOpen(false);
    call.reset();
  };

  const confirm = async () => {
    const result = await call.run();
    if (result === undefined) return; // the error is shown in the dialog
    setOpen(false);
    call.reset();
    const message = done(result);
    toast({ title: message.title, description: message.description, tone: "ok" });
  };

  const dialog = (
    <Dialog
      open={open}
      onClose={close}
      title={title}
      description={description}
      footer={
        <>
          <Button onClick={close} disabled={call.pending}>
            Cancel
          </Button>
          <Button
            variant={destructive ? "danger" : "primary"}
            loading={call.pending}
            onClick={() => {
              void confirm();
            }}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {call.state.status === "error" && (
        <p role="alert" className="text-sm text-status-failed">
          Not done: {describeError(call.state.error).message}
        </p>
      )}
    </Dialog>
  );

  return {
    open: () => {
      setOpen(true);
    },
    dialog,
  };
}
