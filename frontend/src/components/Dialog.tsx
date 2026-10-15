import { useEffect, useId, useRef, type ReactNode } from "react";

import { cn } from "../lib/cn";

export interface DialogProps {
  open: boolean;
  /** Called on Escape, the close button, or a backdrop click. */
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Action row (e.g. Cancel + a destructive confirm). */
  footer?: ReactNode;
  className?: string;
}

/**
 * A modal dialog on the native <dialog> element (D175).
 *
 * Deliberately not Radix Dialog: Radix's scroll lock (react-remove-scroll)
 * injects an inline <style> element, which the controller's CSP
 * (`style-src 'self'`, D181) refuses - a console error on every open. The
 * native element gives the same guarantees with no injected CSS: top layer,
 * inert background, focus moved in and restored on close, Escape closes.
 */
export function Dialog({ open, onClose, title, description, children, footer, className }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) {
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description !== undefined ? descriptionId : undefined}
      onCancel={(event) => {
        // Escape: keep React the owner of `open`.
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        // A click on the backdrop targets the <dialog> itself.
        if (event.target === event.currentTarget) onClose();
      }}
      className={cn(
        "mf-dialog m-auto w-full max-w-lg rounded-lg border border-border-strong bg-surface-raised p-0 text-fg shadow-overlay",
        className,
      )}
    >
      {open && (
        <div className="flex flex-col gap-4 p-5">
          <div className="flex flex-col gap-1">
            <h2 id={titleId} className="text-lg font-semibold">
              {title}
            </h2>
            {description !== undefined && (
              <p id={descriptionId} className="text-sm text-fg-muted">
                {description}
              </p>
            )}
          </div>
          {children}
          {footer !== undefined && <div className="flex justify-end gap-2">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}
