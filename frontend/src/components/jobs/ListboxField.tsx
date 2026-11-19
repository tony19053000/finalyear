import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import { CheckIcon, ChevronIcon } from "../../app/icons";
import { cn } from "../../lib/cn";
import { opensUp, useOutsideClose } from "./useOutsideClose";

export interface ListboxOption {
  value: string;
  label: string;
  /** A second, quieter line (a node id). */
  detail?: string;
}

export interface ListboxFieldProps {
  /** The trigger's id (a `<label htmlFor>` names it). */
  id: string;
  value: string;
  options: readonly ListboxOption[];
  onChange: (value: string) => void;
  className?: string;
  disabled?: boolean;
}

/** Rows are ~48px; the panel shows at most about six before it scrolls. */
const PANEL_HEIGHT = 300;

/**
 * A select drawn in hive's own language (no browser list): an underline
 * trigger with a thin chevron, a frosted list under it. ARIA listbox with
 * the active row as `aria-activedescendant`; arrows / Home / End move,
 * Enter or Space picks, Escape or Tab closes. Escape stops here, so a
 * popover around the field stays open.
 */
export function ListboxField({ id, value, options, onChange, className, disabled = false }: ListboxFieldProps) {
  const [open, setOpen] = useState(false);
  const [up, setUp] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const listId = useId();
  const optionId = (i: number) => `${listId}-o${String(i)}`;
  const selected = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );
  const current = options[selected];

  const close = useCallback(() => {
    setOpen(false);
  }, []);
  useOutsideClose(open, root, close);

  useEffect(() => {
    if (open) list.current?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const row = document.getElementById(`${listId}-o${String(active)}`);
    if (typeof row?.scrollIntoView === "function") row.scrollIntoView({ block: "nearest" });
  }, [open, active, listId]);

  const show = () => {
    setUp(opensUp(trigger.current, PANEL_HEIGHT));
    setActive(selected);
    setOpen(true);
  };
  const pick = (i: number) => {
    const option = options[i];
    if (option !== undefined && option.value !== value) onChange(option.value);
    setOpen(false);
    trigger.current?.focus();
  };

  const onTriggerKey = (event: KeyboardEvent) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      show();
    }
  };
  const onListKey = (event: KeyboardEvent) => {
    const last = options.length - 1;
    const move: Record<string, number> = {
      ArrowDown: Math.min(last, active + 1),
      ArrowUp: Math.max(0, active - 1),
      Home: 0,
      End: last,
    };
    const next = move[event.key];
    if (next !== undefined) {
      event.preventDefault();
      setActive(next);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick(active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div ref={root} className={cn("relative", className)}>
      <button
        ref={trigger}
        id={id}
        type="button"
        className="jb-field jb-field-btn w-full"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => {
          if (open) setOpen(false);
          else show();
        }}
        onKeyDown={onTriggerKey}
      >
        <span className={cn("truncate", current?.value === "" && "text-fg-subtle")}>{current?.label ?? ""}</span>
        <span className={cn("jb-chevron", open && "jb-chevron-open")}>
          <ChevronIcon />
        </span>
      </button>
      {open && (
        <ul
          ref={list}
          id={listId}
          role="listbox"
          tabIndex={-1}
          aria-labelledby={id}
          aria-activedescendant={optionId(active)}
          className={cn("jb-float jb-listbox", up ? "jb-float-up" : "jb-float-down")}
          onKeyDown={onListKey}
        >
          {options.map((option, i) => {
            const isSelected = i === selected;
            return (
              <li
                key={option.value}
                id={optionId(i)}
                role="option"
                aria-selected={isSelected}
                data-active={i === active || undefined}
                className="jb-opt"
                onMouseEnter={() => {
                  setActive(i);
                }}
                onClick={() => {
                  pick(i);
                }}
              >
                <span className="flex min-w-0 flex-col">
                  <span className={cn("truncate", isSelected ? "text-accent" : "text-fg")}>{option.label}</span>
                  {option.detail !== undefined && (
                    <span className="truncate font-mono text-[0.6875rem] text-fg-subtle">{option.detail}</span>
                  )}
                </span>
                {isSelected && (
                  <span className="shrink-0 text-accent">
                    <CheckIcon />
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
