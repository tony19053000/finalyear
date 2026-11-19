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

import { ErrorState } from "../States";
import { cn } from "../../lib/cn";
import { useServerNow } from "../../lib/serverClock";
import { jobKeys } from "../../pages/jobs/jobKeys";
import { stateLight } from "../../pages/jobs/jobView";
import { FrostTable } from "../../pages/nodes/FrostTable";
import {
  CHUNK_STATE_LABEL,
  getChunk,
  getChunkTries,
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

/** Lane (attempt) states -> the flat light's colour. */
const LANE_LIGHT: Record<string, string> = {
  assigned: "bg-status-unknown",
  staging: "bg-status-busy",
  env_ready: "bg-status-busy",
  running: "bg-status-busy",
  uploading: "bg-status-busy",
  done: "bg-status-ok",
  failed: "bg-status-failed",
  lost: "bg-status-degraded",
  fenced: "bg-status-degraded",
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
    <div className="flex min-w-0 flex-col gap-2">
      <dt className="nd-label">{label}</dt>
      <dd className="text-[0.9375rem] text-fg tabular-nums" data-field={field}>
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
    <section aria-label="Progress" className="flex flex-col gap-6">
      <div className="flex items-end justify-between gap-6 px-2">
        <p className="ov-figure text-[3.5rem] text-fg">
          <span data-field="percent">{percent == null ? "—" : `${Math.floor(percent).toString()}%`}</span>
        </p>
        <span className="mb-2 inline-flex items-center gap-2.5 font-mono text-[0.6875rem] tracking-[0.14em] text-fg-muted uppercase">
          <span aria-hidden="true" className={`nd-light ${stateLight(progress.job_state)}`} />
          {progress.job_state}
        </span>
      </div>
      <dl className="nd-hair-y grid grid-cols-5 gap-x-10 gap-y-8 px-2 py-8">
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
        <p role="status" className="px-2 text-sm text-fg-muted">
          Every chunk is done; the chunk results are being combined into the job&apos;s output.
        </p>
      )}
      {progress.queue_state == null && (
        <p role="status" className="px-2 text-sm text-fg-muted">
          Not placed yet: the chunks are created when the planner places the job.
        </p>
      )}
      {progress.warnings.length > 0 && (
        <ul aria-label="Split warnings" className="flex flex-col gap-1 px-2">
          {progress.warnings.map((w) => (
            <li key={w.code} className="text-sm">
              <span className="font-mono text-status-degraded">{w.code}</span> ×{w.count}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Legend({ map }: { map: string }) {
  const counts = mapCounts(map);
  return (
    <ul aria-label="Chunk states" className="flex flex-wrap gap-x-8 gap-y-2 font-mono text-[0.6875rem] tracking-[0.06em] text-fg-muted">
      {(Object.keys(counts) as ChunkMapChar[]).map((ch) => (
        <li key={ch} className="inline-flex items-center gap-2" data-legend={ch}>
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
    <dl aria-label={`Chunk ${String(index)}`} className="grid grid-cols-5 gap-x-10 gap-y-6">
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
        <span className="font-mono text-[0.8125rem]">{c.node_id ?? "—"}</span>
      </Stat>
      <Stat label="Tries" field="chunk-tries">
        {c.attempts}
        {c.speculations > 0 ? ` (+${String(c.speculations)} speculative)` : ""}
      </Stat>
      <Stat label="Done at" field="chunk-done-at">
        <span className="font-mono text-[0.8125rem]">{formatTime(c.done_at)}</span>
      </Stat>
      <Stat label="Result" field="chunk-result">
        <span className="font-mono text-[0.8125rem]">{shortDigest(c.result_digest)}</span>
      </Stat>
      {c.failed_kind != null && (
        <Stat label="Failed as" field="chunk-failed">
          <span className="font-mono text-[0.8125rem] text-status-failed">{c.failed_kind}</span>
        </Stat>
      )}
      {c.failed_nodes.length > 0 && (
        <Stat label="Failed on" field="chunk-failed-nodes">
          <span className="font-mono text-[0.8125rem]">{c.failed_nodes.join(", ")}</span>
        </Stat>
      )}
      {c.leases.length > 0 && (
        <Stat label="Held by" field="chunk-leases">
          {c.leases.map((l) => (
            <span key={l.lease_seq} className="block font-mono text-[0.8125rem]">
              {l.node_id} (lease {l.lease_seq}
              {l.speculative ? ", speculative" : ""})
            </span>
          ))}
        </Stat>
      )}
    </dl>
  );
}

function ms(value: number | null | undefined): string {
  return value == null ? "—" : formatDuration(value);
}

/**
 * V0.10 G8 (D261/D271): the chunk's recorded tries - which node ran it, how
 * it ended, its measured phases and the end of its stderr (redacted on the
 * agent; shown as plain text, never as markup).
 */
function ChunkTriesList({ jobId, index }: { jobId: string; index: number }) {
  const tries = useQuery({
    queryKey: jobKeys.chunkTries(jobId, index),
    queryFn: ({ signal }) => getChunkTries(jobId, index, signal),
  });
  if (tries.isPending) {
    return (
      <p role="status" className="mt-6 text-sm text-fg-muted">
        Loading tries…
      </p>
    );
  }
  if (tries.isError) {
    return (
      <ErrorState
        className="mt-6"
        title={`Could not load the tries of chunk ${String(index)}`}
        error={tries.error}
        onRetry={() => {
          void tries.refetch();
        }}
      />
    );
  }
  if (tries.data.tries.length === 0) {
    return <p className="mt-6 text-sm text-fg-muted">No recorded try yet.</p>;
  }
  return (
    <ol aria-label={`Tries of chunk ${String(index)}`} className="mt-7 flex flex-col divide-y divide-[#ffffff0f] text-sm">
      {tries.data.tries.map((t) => (
        <li key={t.lease_seq} data-field="chunk-try" className="py-4">
          <p className="font-mono text-[0.8125rem] text-fg">
            try {t.lease_seq} · {t.node_id} · {t.outcome}
            {t.won ? " (won)" : ""}
            {t.speculative ? " · speculative" : ""}
            {t.failure_type != null ? ` · ${t.failure_type}` : ""}
            {t.exit_code != null ? ` · exit ${String(t.exit_code)}` : ""}
          </p>
          <p className="mt-1 font-mono text-2xs text-fg-muted">
            stage {ms(t.stage_ms)} · run {ms(t.run_ms)} · upload {ms(t.upload_ms)}
          </p>
          {t.stderr_tail != null && (
            <pre data-field="chunk-stderr" className="jb-log mt-3 max-h-48 break-all whitespace-pre-wrap text-status-degraded">
              {t.stderr_tail}
            </pre>
          )}
        </li>
      ))}
    </ol>
  );
}

function Chunks({ jobId, progress }: { jobId: string; progress: SplitProgress }) {
  const [selected, setSelected] = useState<number | null>(null);
  const inputId = useId();
  const map = progress.chunk_map ?? "";
  return (
    <section aria-label={`Chunks (${String(map.length)})`} className="ov-card ov-frost flex flex-col gap-6 px-9 pt-7 pb-8">
      <header className="flex flex-wrap items-center justify-between gap-x-8 gap-y-3">
        <h2 className="ov-card-title">
          Chunks <span className="ml-1 font-mono text-[0.75rem] font-normal text-fg-subtle tabular-nums">{map.length}</span>
        </h2>
        {map.length > 0 && <Legend map={map} />}
      </header>
      {map.length === 0 ? (
        <p className="text-sm text-fg-muted">No chunks yet.</p>
      ) : (
        <>
          <ChunkGrid map={map} selected={selected} onSelect={setSelected} />
          <div className="flex items-end gap-4 text-sm">
            <label htmlFor={inputId} className="nd-label pb-2.5">
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
              className="jb-field w-24 tabular-nums"
            />
            {selected == null && <span className="pb-2.5 text-xs text-fg-muted">click a cell or type a number</span>}
          </div>
          {selected != null && (
            <div className="nd-hair-t nd-in pt-7">
              <ChunkDetail key={`${String(selected)}:${String(progress.chunks.done)}`} jobId={jobId} index={selected} />
              <ChunkTriesList key={`tries:${String(selected)}:${String(progress.chunks.done)}:${String(progress.chunks.failed)}`} jobId={jobId} index={selected} />
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Lanes({ progress, names }: { progress: SplitProgress; names?: ReadonlyMap<string, string> }) {
  const total = progress.items_total;
  const header = ["Node", "Lane", "Chunks", "Items", "Share", "Rate", "Running now", "Failed tries", "Last chunk"];
  return (
    <FrostTable
      title="Nodes"
      className="jb-lanes"
      testId="lanes-card"
      meta={
        <span className="font-mono text-[0.6875rem] tracking-[0.12em] text-fg-subtle uppercase">
          {progress.lanes_total} lane{progress.lanes_total === 1 ? "" : "s"}
        </span>
      }
    >
      {progress.lanes.length === 0 ? (
        <p className="px-9 pb-4 text-sm text-fg-muted">No lane yet.</p>
      ) : (
        <>
          <table className="w-full border-collapse">
            <caption className="sr-only">One row per lane: the node, what it committed and runs now</caption>
            <thead>
              <tr>
                {header.map((h) => (
                  <th key={h} scope="col" className="text-left font-mono font-medium tracking-[0.14em] whitespace-nowrap text-fg-subtle uppercase">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {progress.lanes.map((lane: SplitLane) => {
                const name = names?.get(lane.node_id);
                return (
                  <tr key={lane.attempt_id} data-lane={lane.node_id}>
                    <td>
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate">{name ?? lane.node_id}</span>
                        {name !== undefined && <span className="truncate font-mono text-2xs text-fg-muted">{lane.node_id}</span>}
                      </span>
                    </td>
                    <td>
                      <span className="inline-flex items-center gap-2.5">
                        <span aria-hidden="true" className={cn("nd-light", LANE_LIGHT[lane.state] ?? "bg-status-unknown")} />
                        {lane.state}
                      </span>
                    </td>
                    <td className="tabular-nums" data-field="lane-chunks">
                      {lane.chunks_done}
                    </td>
                    <td className="tabular-nums" data-field="lane-items">
                      {lane.items_done}
                    </td>
                    <td className="tabular-nums">{total > 0 ? `${Math.round((lane.items_done * 100) / total).toString()}%` : "—"}</td>
                    <td className="whitespace-nowrap tabular-nums">{rate(lane.rate_items_s)}</td>
                    <td className="font-mono text-[0.8125rem]" data-field="lane-current">
                      {lane.current.length === 0
                        ? "—"
                        : lane.current.map((c) => `#${String(c.chunk_index)}${c.speculative ? "*" : ""}`).join(" ")}
                    </td>
                    <td className="tabular-nums">{lane.failed_tries}</td>
                    <td className="font-mono text-[0.8125rem] whitespace-nowrap">{formatTime(lane.last_commit_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {progress.lanes_total > progress.lanes.length && (
            <p className="px-9 pt-2 text-xs text-fg-muted">
              Showing the {progress.lanes.length} newest lanes of {progress.lanes_total}.
            </p>
          )}
        </>
      )}
    </FrostTable>
  );
}

export interface SplitProgressPanelProps {
  jobId: string;
  /** The job ended: no more polling. */
  terminal: boolean;
  /** node_id -> name (the Nodes table shows names, the ids beneath). */
  names?: ReadonlyMap<string, string>;
}

export function SplitProgressPanel({ jobId, terminal, names }: SplitProgressPanelProps) {
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
    <div className="flex flex-col gap-12">
      <Overview progress={progress} />
      <Chunks jobId={jobId} progress={progress} />
      <Lanes progress={progress} names={names} />
    </div>
  );
}
