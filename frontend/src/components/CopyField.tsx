import { useEffect, useId, useState } from "react";

import { cn } from "../lib/cn";
import { Button } from "./Button";

export interface CopyFieldProps {
  /** The text to show and copy (e.g. a join code shown once). */
  value: string;
  /** Visible label above the field. */
  label: string;
  className?: string;
}

/**
 * Read-only monospace text with a Copy button. The value lives only in this
 * component (never in storage or the query cache). Copy uses the Clipboard API
 * (the dashboard is HTTPS or loopback - a secure context); if the browser
 * refuses, the text is selected and the user is told to press Ctrl+C.
 */
export function CopyField({ value, label, className }: CopyFieldProps) {
  const id = useId();
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (note === null) return undefined;
    const timer = setTimeout(() => {
      setNote(null);
    }, 4000);
    return () => {
      clearTimeout(timer);
    };
  }, [note]);

  const selectAll = () => {
    const field = document.getElementById(id);
    if (field === null) return;
    const range = document.createRange();
    range.selectNodeContents(field);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setNote("Copied.");
    } catch {
      selectAll();
      setNote("The browser did not allow copying: the text is selected, press Ctrl+C (Cmd+C on a Mac).");
    }
  };

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-sm text-fg-muted" id={`${id}-label`}>
        {label}
      </span>
      <div className="flex items-start gap-2">
        <pre
          id={id}
          tabIndex={0}
          aria-labelledby={`${id}-label`}
          className="min-w-0 flex-1 rounded-md border border-border bg-surface-sunken p-2.5 font-mono text-xs leading-5 break-all whitespace-pre-wrap text-fg select-all"
        >
          {value}
        </pre>
        <Button
          size="sm"
          onClick={() => {
            void copy();
          }}
        >
          Copy
        </Button>
      </div>
      <p role="status" className="min-h-4 text-xs text-fg-muted">
        {note ?? ""}
      </p>
    </div>
  );
}
