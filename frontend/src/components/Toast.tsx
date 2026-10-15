import * as RadixToast from "@radix-ui/react-toast";
import { useCallback, useRef, useState, type ReactNode } from "react";

import { cn } from "../lib/cn";
import { ToastContext, type Notify, type ToastInput } from "./toastContext";

interface ToastItem extends ToastInput {
  id: number;
}

/** At most this many toasts are on screen; older ones are dropped. */
const MAX_TOASTS = 4;

const EDGE: Record<NonNullable<ToastInput["tone"]>, string> = {
  info: "border-l-accent",
  ok: "border-l-status-ok",
  failed: "border-l-status-failed",
};

/** Provides `useToast()` and renders the toast viewport (Radix: F8 focuses it). */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const notify = useCallback<Notify>((toast) => {
    const id = nextId.current;
    nextId.current += 1;
    setToasts((list) => [...list, { ...toast, id }].slice(-MAX_TOASTS));
  }, []);

  const remove = useCallback((id: number) => {
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={notify}>
      <RadixToast.Provider swipeDirection="right" duration={6000}>
        {children}
        {toasts.map((toast) => (
          <RadixToast.Root
            key={toast.id}
            type={toast.tone === "failed" ? "foreground" : "background"}
            onOpenChange={(open) => {
              if (!open) remove(toast.id);
            }}
            className={cn(
              "grid grid-cols-[1fr_auto] items-start gap-x-3 rounded-md border border-l-4 border-border-strong bg-surface-raised px-3 py-2.5 shadow-overlay",
              EDGE[toast.tone ?? "info"],
            )}
          >
            <RadixToast.Title className="text-sm font-medium text-fg">{toast.title}</RadixToast.Title>
            <RadixToast.Close
              aria-label="Dismiss"
              className="row-span-2 rounded-sm px-1 text-fg-subtle hover:text-fg"
            >
              ×
            </RadixToast.Close>
            {toast.description !== undefined && (
              <RadixToast.Description className="text-xs text-fg-muted">
                {toast.description}
              </RadixToast.Description>
            )}
          </RadixToast.Root>
        ))}
        <RadixToast.Viewport className="fixed right-4 bottom-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2 outline-none" />
      </RadixToast.Provider>
    </ToastContext.Provider>
  );
}
