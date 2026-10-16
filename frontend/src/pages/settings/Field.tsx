import { useId, type ReactNode } from "react";

export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
}

/**
 * A labelled form control with optional help text: the label names the
 * control (and only the label - help text is its description, not part of its
 * name), as screen readers and keyboard users expect (checklist 27).
 */
export function Field({
  label,
  help,
  children,
}: {
  label: ReactNode;
  help?: ReactNode;
  children: (control: FieldControlProps) => ReactNode;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      <label htmlFor={id}>{label}</label>
      {children(help !== undefined ? { id, "aria-describedby": helpId } : { id })}
      {help !== undefined && (
        <span id={helpId} className="text-xs text-fg-muted">
          {help}
        </span>
      )}
    </div>
  );
}
