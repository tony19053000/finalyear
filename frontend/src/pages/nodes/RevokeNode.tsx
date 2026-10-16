/**
 * Revoke a node (D138 via `POST /v1/nodes/{id}/revoke`, admin only). There
 * is no un-revoke - the laptop comes back only under a new identity - so the
 * dialog asks for the node's name to be typed before the button arms. The
 * Dialog is the native <dialog> (G2: Radix Dialog injects inline styles the
 * CSP refuses): Escape cancels, focus returns to the button.
 */
import { useState, type FormEvent } from "react";

import { describeError } from "../../api/errors";
import { useRevokeNode, type NodeRevoked } from "../../api/fabric";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/toastContext";

function resultText(result: NodeRevoked): string {
  if (result.already_revoked) return "It was already revoked; nothing changed.";
  const cut = result.session_killed ? "Its live session was cut. " : "It had no live session. ";
  const enforced =
    result.enforcement === "deferred"
      ? "Enforcing it right away failed; the controller retries within its revocation sync interval."
      : "Every plane refuses its certificate now.";
  return cut + enforced;
}

export function RevokeNode({ nodeId, hostname }: { nodeId: string; hostname: string }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const revoke = useRevokeNode(nodeId);
  const toast = useToast();
  const armed = typed === hostname && !revoke.isPending;

  const close = () => {
    if (revoke.isPending) return;
    setOpen(false);
    setTyped("");
    revoke.reset();
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!armed) return;
    revoke.mutate(undefined, {
      onSuccess: (result) => {
        setOpen(false);
        setTyped("");
        toast({ title: `${hostname} revoked`, description: resultText(result), tone: "ok" });
      },
    });
  };

  return (
    <>
      <Button
        variant="danger"
        onClick={() => {
          setOpen(true);
        }}
      >
        Revoke node
      </Button>
      <Dialog
        open={open}
        onClose={close}
        title={`Revoke ${hostname}?`}
        description="The node is cut off at once: its session ends, its running work is retried elsewhere, and its certificate is refused everywhere. There is no undo - the laptop can only rejoin under a new identity."
      >
        <form id="revoke-form" onSubmit={submit} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5 text-sm">
            <span>
              Type <span className="font-mono font-semibold">{hostname}</span> to confirm
            </span>
            <input
              // The dialog opens on a deliberate click; focus belongs in its only field.
              autoFocus
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(e) => {
                setTyped(e.target.value);
              }}
              className="h-8 rounded-md border border-border-strong bg-surface px-2.5 font-mono text-sm text-fg"
            />
          </label>
          {revoke.isError && (
            <p role="alert" className="text-sm text-status-failed">
              Not revoked: {describeError(revoke.error).message}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button onClick={close} disabled={revoke.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="danger" disabled={!armed} loading={revoke.isPending}>
              Revoke permanently
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
