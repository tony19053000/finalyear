import { useId, type ReactNode } from "react";

import { cn } from "../../lib/cn";

/** The dashboard's text-input look (pages/nodes/RevokeNode.tsx). */
export const INPUT =
  "h-8 w-full rounded-md border border-border-strong bg-surface px-2.5 text-sm text-fg placeholder:text-fg-subtle disabled:opacity-50";

export interface ToggleProps {
  label: ReactNode;
  /** One plain sentence: what turning it on means for the owner. */
  description: ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  name?: string;
}

/** A checkbox with its label and one-sentence explanation. */
export function Toggle({ label, description, checked, onChange, disabled, name }: ToggleProps) {
  const id = useId();
  return (
    <div className="flex items-start gap-3 py-2">
      <input
        id={id}
        name={name}
        type="checkbox"
        className="mt-1 size-4 shrink-0 accent-accent"
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-d`}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
      />
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-fg">
          {label}
        </label>
        <p id={`${id}-d`} className="text-sm text-fg-muted">
          {description}
        </p>
      </div>
    </div>
  );
}

export interface NumberFieldProps {
  label: ReactNode;
  description: ReactNode;
  value: string;
  onChange: (value: string) => void;
  suffix?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  name?: string;
  error?: string | undefined;
}

/** A labelled number box; empty = "no limit" where the caller says so. */
export function NumberField({ label, description, value, onChange, suffix, placeholder, min, max, step, name, error }: NumberFieldProps) {
  const id = useId();
  return (
    <div className="py-2">
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {label}
      </label>
      <p id={`${id}-d`} className="text-sm text-fg-muted">
        {description}
      </p>
      <div className="mt-1 flex max-w-xs items-center gap-2">
        <input
          id={id}
          name={name}
          type="number"
          inputMode="numeric"
          className={cn(INPUT, error !== undefined && "border-status-failed")}
          value={value}
          placeholder={placeholder}
          min={min}
          max={max}
          step={step}
          aria-describedby={`${id}-d${error !== undefined ? ` ${id}-e` : ""}`}
          aria-invalid={error !== undefined || undefined}
          onChange={(e) => {
            onChange(e.target.value);
          }}
        />
        {suffix !== undefined && <span className="text-sm text-fg-muted">{suffix}</span>}
      </div>
      {error !== undefined && (
        <p id={`${id}-e`} role="alert" className="mt-1 text-sm text-status-failed">
          {error}
        </p>
      )}
    </div>
  );
}

/** A problem said in plain words, with what to do. */
export function Problem({
  title,
  message,
  fix,
  code,
  className,
}: {
  title: string;
  message: string;
  fix?: string;
  code?: string | undefined;
  className?: string;
}) {
  return (
    <div
      role="alert"
      data-code={code}
      className={cn("rounded-lg border border-status-failed bg-status-failed-soft px-4 py-3", className)}
    >
      <h2 className="text-sm font-semibold text-status-failed">{title}</h2>
      <p className="text-sm text-fg">{message}</p>
      {fix !== undefined && fix !== "" && <p className="mt-1 text-sm text-fg-muted">{fix}</p>}
    </div>
  );
}
