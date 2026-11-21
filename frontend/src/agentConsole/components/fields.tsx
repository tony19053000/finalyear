import { useId, type ReactNode } from "react";

import { cn } from "../../lib/cn";

/**
 * The console's building blocks, in the dashboard's language: glass panels
 * with quiet titles, hairline inputs that warm to honey, switches, plain
 * problem notes with a coloured edge (no pills, no glow).
 */

/** A hairline text input (the sign-in page's look). */
export const INPUT = "ac-input";

/** A glass panel with a quiet title row (the Benchmarks card). */
export function Panel({
  title,
  actions,
  corner,
  children,
  className,
  plain = false,
  as: Tag = "h2",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  /** Round icon buttons in the card's notched top-right corner (the Overview's). */
  corner?: ReactNode;
  children: ReactNode;
  className?: string;
  plain?: boolean;
  as?: "h1" | "h2";
}) {
  return (
    <section className={cn(plain ? "ac-plain" : "ov-card ac-panel", "relative min-w-0", className)}>
      {(title !== undefined || actions !== undefined) && (
        <header className={cn("ac-panel-head flex flex-wrap items-center justify-between gap-x-6 gap-y-2", corner !== undefined && "pr-24")}>
          {title !== undefined && <Tag className="ov-card-title">{title}</Tag>}
          {actions}
        </header>
      )}
      {corner !== undefined && <div className="ov-notch">{corner}</div>}
      <div className="ac-panel-body">{children}</div>
    </section>
  );
}

/** The Overview's corner arrow (↗). */
export function ArrowCorner() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <path d="M5 11L11 5M6 5h5v5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** A small Overview card holding one big light figure. */
export function FigureCard({ title, children, sub, className }: { title: string; children: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <section className={cn("ov-card ac-fig-card flex min-w-0 flex-col justify-between", className)} aria-label={title}>
      <h2 className="ov-card-title">{title}</h2>
      <div className="min-w-0">
        <div className="ov-figure ac-fig-big">{children}</div>
        {sub !== undefined && <p className="ac-note mt-2 truncate">{sub}</p>}
      </div>
    </section>
  );
}

/** A small status dot and its word (the dashboard's Light). */
export function Dot({ tone, children }: { tone: "ok" | "warn" | "off" | "failed" | "honey"; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2.5" data-tone={tone}>
      <span aria-hidden="true" className={cn("ac-dot", `ac-dot-${tone}`)} />
      <span>{children}</span>
    </span>
  );
}

export interface ToggleProps {
  label: ReactNode;
  /** One plain sentence: what turning it on means for the owner. */
  description: ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  name?: string;
}

/** A switch with its label and one-sentence explanation (still a checkbox to assistive tech). */
export function Toggle({ label, description, checked, onChange, disabled, name }: ToggleProps) {
  const id = useId();
  return (
    <div className="ac-row flex items-start justify-between gap-8">
      <div className="min-w-0">
        <label htmlFor={id} className="ac-row-label">
          {label}
        </label>
        <p id={`${id}-d`} className="ac-row-help">
          {description}
        </p>
      </div>
      <input
        id={id}
        name={name}
        type="checkbox"
        className="ac-switch mt-1 shrink-0"
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-d`}
        onChange={(e) => {
          onChange(e.target.checked);
        }}
      />
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
    <div className="ac-row flex items-start justify-between gap-8">
      <div className="min-w-0">
        <label htmlFor={id} className="ac-row-label">
          {label}
        </label>
        <p id={`${id}-d`} className="ac-row-help">
          {description}
        </p>
        {error !== undefined && (
          <p id={`${id}-e`} role="alert" className="mt-2 text-sm text-status-failed">
            {error}
          </p>
        )}
      </div>
      <div className="flex w-36 shrink-0 items-baseline gap-2">
        <input
          id={id}
          name={name}
          type="number"
          inputMode="numeric"
          className={cn("ac-input ac-input-num", error !== undefined && "ac-input-bad")}
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
        {suffix !== undefined && <span className="font-mono text-xs text-fg-muted">{suffix}</span>}
      </div>
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
    <div role="alert" data-code={code} className={cn("ac-problem", className)}>
      <h2 className="text-[0.9375rem] font-semibold text-status-failed">{title}</h2>
      <p className="mt-1.5 text-sm text-fg">{message}</p>
      {fix !== undefined && fix !== "" && <p className="mt-1.5 text-sm text-fg-muted">{fix}</p>}
    </div>
  );
}
