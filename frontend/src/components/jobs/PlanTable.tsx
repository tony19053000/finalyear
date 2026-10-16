import { Badge } from "../Status";
import { EmptyState } from "../States";
import type { CandidateOut, DecisionOut } from "../../pages/jobs/jobsApi";
import { cn } from "../../lib/cn";
import { formatDuration, formatTime } from "./format";

/** The estimator's terms, in the order `compute explain` prints them. */
const TERMS = ["transfer", "env", "compute", "output"] as const;
/** The five measured phases of an attempt (ActualOut.phases_ms). */
const PHASES = ["queue", "stage", "env", "run", "collect"] as const;

const SOURCE_TONE = {
  measured: "ok",
  history: "accent",
  scaled: "neutral",
  assumed: "degraded",
} as const;

function seconds(s: number | undefined): string {
  return s === undefined ? "—" : formatDuration(s * 1000);
}

function TermCell({ candidate, term }: { candidate: CandidateOut; term: (typeof TERMS)[number] }) {
  const value = candidate.estimate?.terms[term];
  if (value === undefined) return <span className="text-fg-subtle">—</span>;
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap">
      <span className="font-mono tabular-nums">{seconds(value.seconds)}</span>
      {/* Never show an assumption as a measurement (checklist 18). */}
      <Badge tone={SOURCE_TONE[value.source]}>{value.source}</Badge>
    </span>
  );
}

function CandidateRows({ decision }: { decision: DecisionOut }) {
  const ordered = [...decision.candidates].sort((a, b) => {
    if (a.rank != null && b.rank != null) return a.rank - b.rank;
    if (a.rank != null) return -1;
    if (b.rank != null) return 1;
    return a.node_id.localeCompare(b.node_id);
  });
  return (
    <div className="overflow-x-auto" tabIndex={0} role="region" aria-label={`Candidates of attempt ${String(decision.attempt_no)} (scrolls sideways)`}>
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">
          Candidates of decision {decision.id} (attempt {decision.attempt_no})
        </caption>
        <thead>
          <tr className="border-b border-border">
            {["Rank", "Node", "Mode", "Slot", "Feasible", ...TERMS, "Total", "Score", "Why not"].map((h) => (
              <th
                key={h}
                scope="col"
                className="h-8 px-3 text-left text-2xs font-medium tracking-wide whitespace-nowrap text-fg-subtle uppercase"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ordered.map((c) => {
            const chosen = c.node_id === decision.chosen_node_id && c.rank === 1;
            return (
              <tr
                key={`${c.node_id}-${c.mode}`}
                className={cn("border-b border-border last:border-b-0", chosen && "font-medium")}
              >
                <td className="h-9 px-3 font-mono text-xs tabular-nums">{c.rank ?? "—"}</td>
                <td className="h-9 px-3 font-mono text-xs whitespace-nowrap">
                  {c.node_id}
                  {chosen && (
                    <Badge tone="accent" className="ml-2">
                      chosen
                    </Badge>
                  )}
                </td>
                <td className="h-9 px-3 text-xs">{c.mode}</td>
                <td className="h-9 px-3 text-xs">{c.slot ?? "—"}</td>
                <td className="h-9 px-3 text-xs">{c.feasible ? "yes" : "no"}</td>
                {TERMS.map((t) => (
                  <td key={t} className="h-9 px-3 text-xs">
                    <TermCell candidate={c} term={t} />
                  </td>
                ))}
                <td className="h-9 px-3 font-mono text-xs tabular-nums">{seconds(c.estimate?.total_s)}</td>
                <td className="h-9 px-3 font-mono text-xs tabular-nums">
                  {c.score != null ? c.score.toPrecision(4) : "—"}
                </td>
                <td className="h-9 px-3 text-xs text-fg-muted">
                  {c.reject_code != null ? (
                    <>
                      <span className="font-mono">{c.reject_code}</span>
                      {c.reject_text != null && <span>: {c.reject_text}</span>}
                    </>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The plan explanation (D176; GET /v1/jobs/{id}/decisions, what `compute
 * explain` prints): every decision, every candidate - chosen, ranked,
 * rejected - with its estimate terms and their source, and for a placed
 * attempt what actually happened.
 */
export function PlanTable({ decisions, pinned }: { decisions: readonly DecisionOut[]; pinned: boolean }) {
  if (decisions.length === 0) {
    return (
      <EmptyState title="No placement decisions">
        {pinned
          ? "This job was pinned to a node at submit, so the planner did not choose one."
          : "The planner has not placed this job yet. Decisions appear here as it does."}
      </EmptyState>
    );
  }
  return (
    <div className="flex flex-col gap-6">
      {decisions.map((d) => (
        <section key={d.id} aria-label={`Decision ${d.id}`} className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="font-semibold">Attempt {d.attempt_no}</span>
            <Badge tone={d.outcome === "dispatched" ? "ok" : d.outcome === "failed" ? "failed" : "unknown"}>
              {d.outcome}
            </Badge>
            <span className="text-fg-muted">
              policy <span className="font-mono text-fg">{d.policy}</span>
            </span>
            <span className="text-fg-muted">
              chosen <span className="font-mono text-fg">{d.chosen_node_id ?? "none"}</span>
            </span>
            <span className="font-mono text-xs text-fg-muted">{formatTime(d.created_at)}</span>
          </div>
          <CandidateRows decision={d} />
          {d.actual != null && (
            <p className="text-xs text-fg-muted">
              Actual: <span className="font-mono text-fg">{d.actual.node_id}</span> {d.actual.state}
              {d.actual.failure_type != null ? ` (${d.actual.failure_type})` : ""} ·{" "}
              {PHASES.map((p) => {
                const ms = d.actual?.phases_ms?.[p];
                return `${p} ${ms == null ? "did not run" : formatDuration(ms)}`;
              }).join(" · ")}
            </p>
          )}
        </section>
      ))}
    </div>
  );
}
