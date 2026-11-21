import { useCallback, useRef, useState, type KeyboardEvent } from "react";

import { ChevronIcon } from "../../app/icons";
import { cn } from "../../lib/cn";
import { formatDayTime } from "./format";
import { opensUp, useOutsideClose } from "./useOutsideClose";

export interface DateTimeFieldProps {
  /** The trigger's id (a `<label htmlFor>` names it). */
  id: string;
  /** ISO 8601 instant, or "" for "any time". */
  value: string;
  onChange: (iso: string) => void;
  /** The time a newly picked day gets ("00:00" for "after", "23:59" for "before"). */
  defaultTime?: string;
  className?: string;
}

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;
const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"] as const;
const TIME = /^([01]\d|2[0-3]):([0-5]\d)$/;
const PANEL_HEIGHT = 380;

const pad = (n: number) => String(n).padStart(2, "0");
const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

function parse(iso: string): Date | null {
  if (iso === "") return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

interface CalendarProps {
  value: string;
  onChange: (iso: string) => void;
  defaultTime: string;
  onClear: () => void;
}

/** The month grid and the time row; mounted only while open, so it starts from the current value. */
function Calendar({ value, onChange, defaultTime, onClear }: CalendarProps) {
  const chosen = parse(value);
  const [today] = useState(() => new Date());
  const start = chosen ?? today;
  const [view, setView] = useState({ year: start.getFullYear(), month: start.getMonth() });
  const [time, setTime] = useState(chosen ? `${pad(chosen.getHours())}:${pad(chosen.getMinutes())}` : defaultTime);

  const emit = (day: Date, hhmm: string) => {
    const m = TIME.exec(hhmm);
    if (m === null) return;
    onChange(new Date(day.getFullYear(), day.getMonth(), day.getDate(), Number(m[1]), Number(m[2])).toISOString());
  };
  const shift = (by: number) => {
    setView((v) => {
      const d = new Date(v.year, v.month + by, 1);
      return { year: d.getFullYear(), month: d.getMonth() };
    });
  };

  const lead = (new Date(view.year, view.month, 1).getDay() + 6) % 7; // Monday first
  const days = new Date(view.year, view.month + 1, 0).getDate();
  const cells: (number | null)[] = [...Array<null>(lead).fill(null), ...Array.from({ length: days }, (_, i) => i + 1)];
  const title = `${MONTHS[view.month] ?? ""} ${String(view.year)}`;

  return (
    <>
      <div className="flex items-center justify-between">
        <button
          type="button"
          className="jb-cal-nav"
          aria-label="Previous month"
          onClick={() => {
            shift(-1);
          }}
        >
          <ChevronIcon dir="left" />
        </button>
        <span className="text-[0.8125rem] font-medium text-fg" aria-live="polite">
          {title}
        </span>
        <button
          type="button"
          className="jb-cal-nav"
          aria-label="Next month"
          onClick={() => {
            shift(1);
          }}
        >
          <ChevronIcon dir="right" />
        </button>
      </div>
      <div className="mt-3 grid grid-cols-7 justify-items-center gap-y-1" role="group" aria-label={title}>
        {WEEKDAYS.map((d) => (
          <span key={d} aria-hidden="true" className="nd-label pb-1">
            {d}
          </span>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <span key={`x${String(i)}`} aria-hidden="true" />;
          const date = new Date(view.year, view.month, day);
          const isChosen = chosen !== null && sameDay(chosen, date);
          return (
            <button
              key={day}
              type="button"
              aria-label={`${String(day)} ${MONTHS[view.month] ?? ""} ${String(view.year)}`}
              aria-pressed={isChosen}
              className={cn("jb-cal-day", sameDay(today, date) && "jb-cal-today", isChosen && "jb-cal-chosen")}
              onClick={() => {
                emit(date, TIME.test(time) ? time : defaultTime);
              }}
            >
              {day}
            </button>
          );
        })}
      </div>
      <div className="jb-cal-foot">
        <label className="flex items-center gap-3">
          <span className="nd-label">Time</span>
          <input
            className="jb-field jb-time"
            inputMode="numeric"
            maxLength={5}
            placeholder="HH:MM"
            aria-label="Time (HH:MM)"
            value={time}
            onChange={(event) => {
              const next = event.target.value;
              setTime(next);
              if (chosen !== null && TIME.test(next)) emit(chosen, next);
            }}
            onBlur={() => {
              if (!TIME.test(time)) setTime(chosen ? `${pad(chosen.getHours())}:${pad(chosen.getMinutes())}` : defaultTime);
            }}
          />
        </label>
        <button type="button" className="jb-clear-btn" onClick={onClear} disabled={value === ""}>
          Clear
        </button>
      </div>
    </>
  );
}

/**
 * A date-and-time field in hive's own language (no browser picker): the
 * value as "18 Nov · 10:00" (or a muted "any time") on an underline; a
 * click opens a small frosted month calendar with a mono HH:MM row and a
 * quiet "Clear". Escape closes it and stops there, so a popover around the
 * field stays open.
 */
export function DateTimeField({ id, value, onChange, defaultTime = "00:00", className }: DateTimeFieldProps) {
  const [open, setOpen] = useState(false);
  const [up, setUp] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => {
    setOpen(false);
  }, []);
  useOutsideClose(open, root, close);
  const shown = formatDayTime(value);

  const onKey = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !open) return;
    event.preventDefault();
    event.stopPropagation();
    setOpen(false);
    trigger.current?.focus();
  };

  return (
    <div ref={root} className={cn("relative", className)} onKeyDown={onKey}>
      <button
        ref={trigger}
        id={id}
        type="button"
        className="jb-field jb-field-btn w-full"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          if (!open) setUp(opensUp(trigger.current, PANEL_HEIGHT));
          setOpen(!open);
        }}
      >
        <span className={cn("truncate tabular-nums", shown === "" && "text-fg-subtle")}>{shown === "" ? "any time" : shown}</span>
        <span className={cn("jb-chevron", open && "jb-chevron-open")}>
          <ChevronIcon />
        </span>
      </button>
      {open && (
        <div role="dialog" aria-label="Pick a date and time" className={cn("jb-float jb-cal", up ? "jb-float-up" : "jb-float-down")}>
          <Calendar
            value={value}
            onChange={onChange}
            defaultTime={defaultTime}
            onClear={() => {
              onChange("");
              setOpen(false);
              trigger.current?.focus();
            }}
          />
        </div>
      )}
    </div>
  );
}
