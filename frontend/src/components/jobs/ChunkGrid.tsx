/**
 * A split job's chunks as a grid of cells coloured by state (V0.10 G7, D270):
 * up to 10 000 chunks, virtualised by rows - only the rows in (and near) the
 * scroll viewport are drawn. One SVG sized with `width`/`height` attributes and
 * `fill-*` classes: the controller's CSP (`style-src 'self'`) forbids style
 * attributes. Clicking a cell selects that chunk; keyboard users pick one with
 * the "Chunk #" field of the panel (the grid is a picture with a summary).
 */
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { cn } from "../../lib/cn";
import { CHUNK_STATE_LABEL } from "../../pages/jobs/splitApi";
import { CELL, CELL_CLASS, SQUARE, charAt, columnsFor } from "./splitView";
import { useWindowedRows } from "./useWindowedRows";

export interface ChunkGridProps {
  /** One state character per chunk, index order (`GET .../split?map=true`). */
  map: string;
  selected: number | null;
  onSelect: (index: number) => void;
  className?: string;
}

export function ChunkGrid({ map, selected, onSelect, className }: ChunkGridProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = scrollRef.current;
    if (element === null) return undefined;
    const update = () => {
      setWidth(element.clientWidth);
    };
    update();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(element);
    return () => {
      observer?.disconnect();
    };
  }, []);

  const count = map.length;
  const columns = columnsFor(width);
  const rows = Math.ceil(count / columns);
  const view = useWindowedRows(scrollRef, rows, { rowHeight: CELL, overscan: 4, threshold: 0 });
  const first = view.start * columns;
  const last = Math.min(count, view.end * columns);
  const cells = Array.from({ length: Math.max(0, last - first) }, (_, offset) => {
    const index = first + offset;
    const ch = charAt(map, index);
    const x = (index % columns) * CELL;
    const y = Math.floor(index / columns) * CELL;
    return (
      <rect
        key={index}
        x={x}
        y={y}
        width={SQUARE}
        height={SQUARE}
        rx={2}
        data-chunk={index}
        className={cn(CELL_CLASS[ch], "cursor-pointer", selected === index && "stroke-accent stroke-2")}
        onClick={() => {
          onSelect(index);
        }}
      >
        <title>{`chunk ${String(index)}: ${CHUNK_STATE_LABEL[ch]}`}</title>
      </rect>
    );
  });
  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    // arrow keys move the selection (the scroll region has focus)
    const step: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: columns, ArrowUp: -columns };
    const delta = step[event.key];
    if (delta === undefined || count === 0) return;
    event.preventDefault();
    const next = Math.min(count - 1, Math.max(0, (selected ?? -delta) + delta));
    onSelect(next);
  };
  return (
    <div
      ref={scrollRef}
      tabIndex={0}
      role="region"
      aria-label={`Chunk grid: ${String(count)} chunks; arrow keys select a chunk`}
      onKeyDown={onKey}
      className={cn("max-h-72 overflow-y-auto rounded-md border border-border bg-bg p-1", className)}
    >
      <svg
        role="img"
        aria-label={`${String(count)} chunks coloured by state`}
        width={columns * CELL}
        height={Math.max(rows, 1) * CELL}
        data-rows={rows}
        data-columns={columns}
        data-drawn={last - first}
      >
        {cells}
      </svg>
    </div>
  );
}
