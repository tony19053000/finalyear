/**
 * The Job page's **Chunks** tab for a split job (V0.10 G7, D270; roadmap
 * 0.10.8): overall progress (items, chunks by state, ETA, elapsed vs the
 * planner's prediction, warnings), the chunk grid (every chunk coloured by
 * state; click or arrow keys select one, its details below), and one row per
 * lane (node): chunks and items it committed, its measured items/s, what it
 * runs now.
 *
 * Data: `GET /v1/jobs/{id}/split?map=true`, read from the controller's rows
 * (shown numbers = rows, checklist 37). Live: `split.progress` events patch it
 * (`applySplitEvent`, stale events ignored); a slow poll covers a stream that
 * is down. The elapsed time ticks every second on the controller's clock.
 */
import { useQuery } from "@tanstack/react-query";
import { useId, useState, type ReactNode } from "react";

import { Card } from "../Card";
import { Meter } from "../Meter";
import { ErrorState } from "../States";
import { Badge, StatusDot } from "../Status";
import { jobTone } from "../../lib/status";
import { useServerNow } from "../../lib/serverClock";
import { jobKeys } from "../../pages/jobs/jobKeys";
import {
  CHUNK_STATE_LABEL,
  getChunk,
  getSplit,
  type ChunkMapChar,
  type SplitLane,
  type SplitProgress,
} from "../../pages/jobs/splitApi";
import { ChunkGrid } from "./ChunkGrid";
import { CELL_CLASS, liveElapsed, mapCounts } from "./splitView";
import { formatDuration, formatTime, shortDigest } from "./format";

/** Poll while the job runs, in case the event stream is down (events are the fast path). */
const SPLIT_POLL_MS = 10_000;

const LANE_TONES: Record<string, "ok" | "busy" | "degraded" | "failed" | "unknown"> = {
  assigned: "unknown",
  staging: "busy",
  env_ready: "busy",
  running: "busy",
  uploading: "busy",
  done: "ok",
  failed: "failed",
  lost: "degraded",
  fenced: "degraded",
};

function seconds(value: number | null | undefined): string {
  return value == null ? "—" : formatDuration(value * 1000);
}

function rate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value >= 100 ? `${value.toFixed(0)} items/s` : `${value.toPrecision(3)} items/s`;
}

function Stat({ label, children, field }: { label: string; children: ReactNode; field: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">{label}</dt>
      <dd className="text-sm tabular-nums" data-field={field}>
        {children}
      </dd>
    </div>
  );
}

function Overview({ progress }: { progress: SplitProgress }) {
  const now = useServerNow();
  const { chunks } = progress;
  const percent = progress.items_total > 0 ? (progress.items_done * 100) / progress.items_total : null;
  const elapsed = liveElapsed(progress, now);
  const combining = progress.job_state === "collecting" && progress.queue_state === "done";
  return (
    <Card title="Progress" actions={<StatusDot tone={jobTone(progress.job_state)} label={progress.job_state} />}>
      <div className="mb-3 flex items-center gap-3">
        <Meter percent={percent} className="h-2" />
        <span className="shrink-0 text-sm tabular-nums" data-field="percent">
          {percent == null ? "—" : `${Math.floor(percent).toString()}%`}
        </span>
      </div>
      <dl className="grid grid-cols-[repeat(auto-fill,minmax(10rem,1fr))] gap-x-6 gap-y-3">
        <Stat label="Items done" field="items">
          {progress.items_done} / {progress.items_total}
        </Stat>
        <Stat label="Chunks done" field="chunks-done">
          {chunks.done} / {chunks.total}
        </Stat>
        <Stat label="Running" field="chunks-leased">
          {chunks.leased}
        </Stat>
        <Stat label="Pending" field="chunks-pending">
          {chunks.pending}
        </Stat>
        <Stat label="Failed chunks" field="chunks-failed">
          {chunks.failed}
        </Stat>
        <Stat label="Speculative copies" field="speculative">
          {progress.speculative_live} live · {progress.speculated} total
        </Stat>
        <Stat label="Rate" field="rate">
          {rate(progress.rate_items_s)}
        </Stat>
        <Stat label="ETA" field="eta">
          {progress.ended_at != null ? "—" : progress.eta_s == null ? "not measured yet" : seconds(progress.eta_s)}
        </Stat>
        <Stat label="Elapsed / predicted" field="makespan">
          {seconds(elapsed)} / {seconds(progress.predicted_s)}
        </Stat>
        {progress.combine_ms != null && (
          <Stat label="Combine" field="combine">
            {formatDuration(progress.combine_ms)}
          </Stat>
        )}
      </dl>
      {combining && (
        <p role="status" className="mt-3 text-xs text-fg-muted">
          Every chunk is done; the chunk results are being combined into the job&apos;s output.
        </p>
      )}
      {progress.queue_state == null && (
        <p role="status" className="mt-3 text-xs text-fg-muted">
          Not placed yet: the chunks are created when the planner places the job.
        </p>
      )}
      {progress.warnings.length > 0 && (
        <ul aria-label="Split warnings" className="mt-3">
          {progress.warnings.map((w) => (
            <li key={w.code} className="text-xs">
              <span className="font-mono text-status-degraded">{w.code}</span> ×{w.count}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Legend({ map }: { map: string }) {
  const counts = mapCounts(map);
  return (
    <ul aria-label="Chunk states" className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
      {(Object.keys(counts) as ChunkMapChar[]).map((ch) => (
        <li key={ch} className="inline-flex items-center gap-1.5" data-legend={ch}>
          <svg aria-hidden="true" width="10" height="10">
            <rect width="10" height="10" rx="2" className={CELL_CLASS[ch]} />
          </svg>
          {CHUNK_STATE_LABEL[ch]} <span className="tabular-nums">{counts[ch]}</span>
        </li>
      ))}
    </ul>
  );
}

function ChunkDetail({ jobId, index }: { jobId: string; index: number }) {
  const chunk = useQuery({
    queryKey: jobKeys.chunk(jobId, index),
    queryFn: ({ signal }) => getChunk(jobId, index, signal),
  });
  if (chunk.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading chunk {index}…
      </p>
    );
  }
  if (chunk.isError) {
    return (
      <ErrorState
        title={`Could not load chunk ${String(index)}`}
        error={chunk.error}
        onRetry={() => {
          void chunk.refetch();
        }}
      />
    );
  }
  const c = chunk.data;
  if (c === null) return <p className="text-sm text-fg-muted">No chunk {index}.</p>;
  const lastItem = c.first_item == null ? null : c.first_item + c.item_count - 1;
  return (
    <dl aria-label={`Chunk ${String(index)}`} className="grid grid-cols-[repeat(auto-fill,minmax(12rem,1fr))] gap-x-6 gap-y-2 text-sm">
      <Stat label="Chunk" field="chunk-index">
        #{c.chunk_index}
      </Stat>
      <Stat label="State" field="chunk-state">
        {c.state}
      </Stat>
      <Stat label="Items" field="chunk-items">
        {c.first_item == null ? `${String(c.item_count)} items` : `${String(c.first_item)}–${String(lastItem)} (${String(c.item_count)})`}
      </Stat>
      <Stat label="Node" field="chunk-node">
        <span className="font-mono text-xs">{c.node_id ?? "—"}</span>
      </Stat>
      <Stat label="Tries" field="chunk-tries">
        {c.attempts}
        {c.speculations > 0 ? ` (+${String(c.speculations)} speculative)` : ""}
      </Stat>
      <Stat label="Done at" field="chunk-done-at">
        <span className="font-mono text-xs">{formatTime(c.done_at)}</span>
      </Stat>
      <Stat label="Result" field="chunk-result">
        <span className="font-mono text-xs">{shortDigest(c.result_digest)}</span>
      </Stat>
      {c.failed_kind != null && (
        <Stat label="Failed as" field="chunk-failed">
          <span className="font-mono text-xs text-status-failed">{c.failed_kind}</span>
        </Stat>
      )}
      {c.failed_nodes.length > 0 && (
        <Stat label="Failed on" field="chunk-failed-nodes">
          <span className="font-mono text-xs">{c.failed_nodes.join(", ")}</span>
        </Stat>
      )}
      {c.leases.length > 0 && (
        <Stat label="Held by" field="chunk-leases">
          {c.leases.map((l) => (
            <span key={l.lease_seq} className="block font-mono text-xs">
              {l.node_id} (lease {l.lease_seq}
              {l.speculative ? ", speculative" : ""})
            </span>
          ))}
        </Stat>
      )}
    </dl>
  );
}

function Chunks({ jobId, progress }: { jobId: string; progress: SplitProgress }) {
  const [selected, setSelected] = useState<number | null>(null);
  const inputId = useId();
  const map = progress.chunk_map ?? "";
  return (
    <Card title={`Chunks (${String(map.length)})`}>
      {map.length === 0 ? (
        <p className="text-sm text-fg-muted">No chunks yet.</p>
      ) : (
        <>
          <Legend map={map} />
          <ChunkGrid map={map} selected={selected} onSelect={setSelected} />
          <div className="mt-3 flex items-center gap-2 text-sm">
            <label htmlFor={inputId} className="text-fg-muted">
              Chunk #
            </label>
            <input
              id={inputId}
              type="number"
              min={0}
              max={map.length - 1}
              value={selected ?? ""}
              onChange={(event) => {
                const n = Number.parseInt(event.target.value, 10);
                setSelected(Number.isInteger(n) && n >= 0 && n < map.length ? n : null);
              }}
              className="h-8 w-24 rounded-md border border-border bg-bg px-2 tabular-nums"
            />
            {selected == null && <span className="text-xs text-fg-muted">click a cell or type a number</span>}
          </div>
          {selected != null && (
            <div className="mt-3 border-t border-border pt-3">
              <ChunkDetail key={`${String(selected)}:${String(progress.chunks.done)}`} jobId={jobId} index={selected} />
            </div>
          )}
        </>
      )}
    </Card>
  );
}

function Lanes({ progress }: { progress: SplitProgress }) {
  const total = progress.items_total;
  const header = ["Node", "Lane", "Chunks", "Items", "Share", "Rate", "Running now", "Failed tries", "Last chunk"];
  return (
    <Card title={`Nodes (${String(progress.lanes_total)} lanes)`} flush>
      {progress.lanes.length === 0 ? (
        <p className="p-4 text-sm text-fg-muted">No lane yet.</p>
      ) : (
        <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Lanes table (scrolls sideways)">
          <table className="w-full border-collapse text-sm">
            <caption className="sr-only">One row per lane: the node, what it committed and runs now</caption>
            <thead>
              <tr className="border-b border-border">
                {header.map((h) => (
                  <th key={h} scope="col" className="h-8 px-3 text-left text-2xs font-medium tracking-wide whitespace-nowrap text-fg-subtle uppercase">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {progress.lanes.map((lane: SplitLane) => (
                <tr key={lane.attempt_id} className="border-b border-border last:border-b-0" data-lane={lane.node_id}>
                  <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{lane.node_id}</td>
                  <td className="px-3 py-2">
                    <Badge tone={LANE_TONES[lane.state] ?? "unknown"}>{lane.state}</Badge>
                  </td>
                  <td className="px-3 py-2 tabular-nums" data-field="lane-chunks">
                    {lane.chunks_done}
                  </td>
                  <td className="px-3 py-2 tabular-nums" data-field="lane-items">
                    {lane.items_done}
                  </td>
                  <td className="px-3 py-2 tabular-nums">{total > 0 ? `${Math.round((lane.items_done * 100) / total).toString()}%` : "—"}</td>
                  <td className="px-3 py-2 whitespace-nowrap tabular-nums">{rate(lane.rate_items_s)}</td>
                  <td className="px-3 py-2 font-mono text-xs" data-field="lane-current">
                    {lane.current.length === 0
                      ? "—"
                      : lane.current.map((c) => `#${String(c.chunk_index)}${c.speculative ? "*" : ""}`).join(" ")}
                  </td>
                  <td className="px-3 py-2 tabular-nums">{lane.failed_tries}</td>
                  <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">{formatTime(lane.last_commit_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {progress.lanes_total > progress.lanes.length && (
            <p className="px-4 py-2 text-xs text-fg-muted">
              Showing the {progress.lanes.length} newest lanes of {progress.lanes_total}.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

export interface SplitProgressPanelProps {
  jobId: string;
  /** The job ended: no more polling. */
  terminal: boolean;
}

export function SplitProgressPanel({ jobId, terminal }: SplitProgressPanelProps) {
  const split = useQuery({
    queryKey: jobKeys.split(jobId),
    queryFn: ({ signal }) => getSplit(jobId, signal),
    refetchInterval: terminal ? false : SPLIT_POLL_MS,
  });
  if (split.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the split progress…
      </p>
    );
  }
  if (split.isError) {
    return (
      <ErrorState
        title="Could not load the split progress"
        error={split.error}
        onRetry={() => {
          void split.refetch();
        }}
      />
    );
  }
  const progress = split.data;
  return (
    <div className="flex flex-col gap-4">
      <Overview progress={progress} />
      <Chunks jobId={jobId} progress={progress} />
      <Lanes progress={progress} />
    </div>
  );
}
