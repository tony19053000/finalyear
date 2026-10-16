import { useEffect, useRef, useState } from "react";

import { cn } from "../lib/cn";
import { Button } from "./Button";

export interface CodeBlockProps {
  code: string;
  /** Accessible name, e.g. "compute.yaml" or "join command". */
  label: string;
  className?: string;
}

/** Read-only monospace text (manifests, commands, digests). */
export function CodeBlock({ code, label, className }: CodeBlockProps) {
  return (
    <pre
      aria-label={label}
      tabIndex={0}
      className={cn(
        "overflow-x-auto rounded-md border border-border bg-surface-sunken p-3 font-mono text-xs leading-5 text-fg",
        className,
      )}
    >
      <code>{code}</code>
    </pre>
  );
}

export interface LogLine {
  /** Stable key: e.g. the byte offset of the line in its stream. */
  key: string;
  text: string;
  stream?: "stdout" | "stderr";
}

export interface LogViewProps {
  lines: readonly LogLine[];
  label: string;
  /** Lines kept in the DOM; older ones are cut (the API has the full log). */
  maxLines?: number;
  className?: string;
}

/**
 * A terminal-style log panel. Follows the tail while the user is at the
 * bottom; scrolling up pauses following, "Follow" resumes it. Text is
 * rendered as text (never HTML), so log bytes cannot inject markup.
 */
export function LogView({ lines, label, maxLines = 5000, className }: LogViewProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const shown = lines.length > maxLines ? lines.slice(lines.length - maxLines) : lines;
  const cut = lines.length - shown.length;

  useEffect(() => {
    const el = scroller.current;
    if (follow && el !== null) el.scrollTop = el.scrollHeight;
  }, [shown, follow]);

  return (
    <div className={cn("relative flex flex-col", className)}>
      <div
        ref={scroller}
        role="log"
        aria-label={label}
        tabIndex={0}
        onScroll={(event) => {
          const el = event.currentTarget;
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
          setFollow(atBottom);
        }}
        className="h-full min-h-40 overflow-auto rounded-md border border-border bg-surface-sunken p-3 font-mono text-xs leading-5"
      >
        {cut > 0 && (
          <div className="text-fg-subtle">… {cut} earlier lines not shown</div>
        )}
        {shown.length === 0 ? (
          <div className="text-fg-subtle">No output yet.</div>
        ) : (
          shown.map((line) => (
            <div
              key={line.key}
              data-stream={line.stream}
              className={cn(
                "break-all whitespace-pre-wrap",
                line.stream === "stderr" ? "text-status-degraded" : "text-fg",
              )}
            >
              {line.text}
            </div>
          ))
        )}
      </div>
      {!follow && (
        <Button
          size="sm"
          variant="secondary"
          className="absolute right-3 bottom-3"
          onClick={() => {
            setFollow(true);
          }}
        >
          Follow
        </Button>
      )}
    </div>
  );
}
