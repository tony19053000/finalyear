import { Badge } from "../Status";
import { EmptyState } from "../States";
import type { CandidateOut, DecisionOut } from "../../pages/jobs/jobsApi";
import { cn } from "../../lib/cn";
import { formatDuration, formatTime, shortDigest } from "./format";
import { mlDecisionText, mlLine } from "./mlText";
import {
  PHASES,
  calibrationText,
  chosenCandidate,
  chunksText,
  isSplitDecision,
  marginText,
  orderedCandidates,
  rateText,
  ratioText,
  rejectedOnly,
  seconds,
  termNames,
  type TermOut,
} from "./planView";

const SOURCE_TONE: Record<string, "ok" | "accent" | "neutral" | "degraded"> = {
  measured: "ok",
  history: "accent",
  scaled: "neutral",
  assumed: "degraded",
};

/** common/states.py ATTEMPT_TERMINAL_STATES. */
const ATTEMPT_ENDED: ReadonlySet<string> = new Set(["done", "failed", "lost", "fenced"]);

const TH = "h-8 px-3 text-left text-2xs font-medium tracking-wide whitespace-nowrap text-fg-subtle uppercase";
const TD = "h-9 px-3 text-xs";

function TermCell({ term }: { term: TermOut | undefined }) {
  if (term === undefined) return <span className="text-fg-subtle">—</span>;
  const calibrated = calibrationText(term);
  const ml = mlLine(term.ml);
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="inline-flex items-center gap-1 whitespace-nowrap">
        <span className="font-mono tabular-nums">{seconds(term.seconds)}</span>
        {/* Never show an assumption as a measurement (checklist 18). */}
        <Badge tone={SOURCE_TONE[term.source] ?? "neutral"}>{term.source}</Badge>
        {calibrated != null && (
          <Badge tone="accent" className="font-mono">
            {calibrated}
          </Badge>
        )}
      </span>
      {term.note != null && <span className="max-w-56 text-2xs text-fg-muted">{term.note}</span>}
      {ml != null && (
        // D373: the line `compute explain` prints for this term (mlText.ts).
        <span className={cn("max-w-72 font-mono text-2xs", term.ml?.used === true ? "text-accent" : "text-fg-muted")}>{ml}</span>
      )}
    </span>
  );
}

function nodeSetText(c: CandidateOut): string {
  const idle = new Set(c.inactive ?? []);
  return (c.node_set ?? []).map((n) => (idle.has(n) ? `${n} (idle)` : n)).join(", ") || "—";
}

function CandidateRows({ decision }: { decision: DecisionOut }) {
  const split = isSplitDecision(decision);
  const terms = termNames(decision);
  const heads = [
    "Rank",
    "Node",
    "Mode",
    ...(split ? ["Nodes", "Chunks", "Rate"] : []),
    "Slot",
    "Feasible",
    ...terms,
    "Total",
    "Score",
    ...(split ? ["Margin test"] : []),
    "Why not",
  ];
  return (
    <div
      className="overflow-x-auto"
      tabIndex={0}
      role="region"
      aria-label={`Candidates of attempt ${String(decision.attempt_no)} (scrolls sideways)`}
    >
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">
          Candidates of decision {decision.id} (attempt {decision.attempt_no})
        </caption>
        <thead>
          <tr className="border-b border-border">
            {heads.map((h) => (
              <th key={h} scope="col" className={TH}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {orderedCandidates(decision).map((c) => {
            const chosen = c.node_id === decision.chosen_node_id && c.rank === 1;
            return (
              <tr
                key={`${c.node_id}-${c.mode}-${(c.node_set ?? []).join("+")}`}
                className={cn("border-b border-border align-top last:border-b-0", chosen && "font-medium shadow-[inset_3px_0_0_var(--color-accent)]")}
                aria-current={chosen ? "true" : undefined}
              >
                <td className={cn(TD, "font-mono tabular-nums")}>{c.rank ?? "—"}</td>
                <td className={cn(TD, "font-mono whitespace-nowrap")}>
                  {c.node_id}
                  {chosen && (
                    <Badge tone="accent" className="ml-2">
                      chosen
                    </Badge>
                  )}
                </td>
                <td className={TD}>{c.mode}</td>
                {split && (
                  <>
                    <td className={cn(TD, "font-mono")}>{nodeSetText(c)}</td>
                    <td className={cn(TD, "whitespace-nowrap")}>{chunksText(c)}</td>
                    <td className={cn(TD, "whitespace-nowrap")}>{rateText(c)}</td>
                  </>
                )}
                <td className={TD}>{c.slot ?? "—"}</td>
                <td className={TD}>{c.feasible ? "yes" : "no"}</td>
                {terms.map((t) => (
                  <td key={t} className={TD}>
                    <TermCell term={c.estimate?.terms[t]} />
                  </td>
                ))}
                <td className={cn(TD, "font-mono tabular-nums")}>{seconds(c.estimate?.total_s)}</td>
                <td className={cn(TD, "font-mono tabular-nums")}>{c.score != null ? c.score.toPrecision(4) : "—"}</td>
                {split && <td className={cn(TD, "font-mono")}>{marginText(c)}</td>}
                <td className={cn(TD, "text-fg-muted")}>
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

/** Predicted vs actual per term of the chosen candidate (D320: the API's `actual_s` / `ratio`). */
function ActualRows({ decision }: { decision: DecisionOut }) {
  const actual = decision.actual;
  if (actual == null) return null;
  const chosen = chosenCandidate(decision);
  const terms = Object.entries(chosen?.estimate?.terms ?? {});
  const ended = ATTEMPT_ENDED.has(actual.state);
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-fg-muted">
        Actual: attempt <span className="font-mono text-fg">{actual.attempt_id}</span> on{" "}
        <span className="font-mono text-fg">{actual.node_id}</span> {actual.state}
        {actual.failure_type != null ? ` (${actual.failure_type})` : ""} ·{" "}
        {PHASES.map((p) => {
          const ms = actual.phases_ms?.[p];
          return `${p} ${ms == null ? "did not run" : formatDuration(ms)}`;
        }).join(" · ")}
      </p>
      {terms.length > 0 && (
        <div className="overflow-x-auto">
          <table className="border-collapse text-sm">
            <caption className="sr-only">Predicted vs actual of decision {decision.id}</caption>
            <thead>
              <tr className="border-b border-border">
                {["Term", "Predicted", "Actual", "Actual / predicted"].map((h) => (
                  <th key={h} scope="col" className={TH}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {terms.map(([name, term]) => (
                <tr key={name} className="border-b border-border last:border-b-0">
                  <td className={cn(TD, "font-mono")}>{name}</td>
                  <td className={TD}>
                    <TermCell term={term} />
                  </td>
                  <td className={cn(TD, "font-mono tabular-nums")}>{seconds(term.actual_s)}</td>
                  <td className={cn(TD, "font-mono tabular-nums")}>{ratioText(term.ratio)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-2xs text-fg-subtle">
            {ended
              ? "A dash: no measured phase is honestly the same work (a Mode 3 set, a skipped phase, an attempt that did not finish)."
              : "Actuals appear when the attempt ends."}
          </p>
        </div>
      )}
    </div>
  );
}

function DecisionHeader({ decision }: { decision: DecisionOut }) {
  const d = decision;
  const ml = mlDecisionText(d.ml_model_version, d.ml_fallback);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <span className="font-semibold">Attempt {d.attempt_no}</span>
        <span className="font-mono text-xs text-fg-muted">decision #{d.id}</span>
        <Badge tone={d.outcome === "dispatched" ? "ok" : d.outcome === "failed" ? "failed" : "unknown"}>{d.outcome}</Badge>
        <span className="text-fg-muted">
          policy <span className="font-mono text-fg">{d.policy}</span>
        </span>
        <span className="text-fg-muted">
          objective{" "}
          {d.objective != null ? (
            <span className="font-mono text-fg">{d.objective}</span>
          ) : (
            <span title="Recorded before V0.14: no objective was stored">—</span>
          )}
        </span>
        {d.calibration_version != null ? (
          <Badge tone="accent">calibration v{d.calibration_version}</Badge>
        ) : (
          <Badge>uncalibrated</Badge>
        )}
        {ml != null && (
          <Badge tone={d.ml_fallback != null ? "degraded" : "accent"} className="font-mono">
            {ml}
          </Badge>
        )}
        {d.replay_version != null ? (
          <Badge tone="ok">replay v{d.replay_version}</Badge>
        ) : (
          <Badge tone="degraded">no replay</Badge>
        )}
        <span className="text-fg-muted">
          chosen <span className="font-mono text-fg">{d.chosen_node_id ?? "none"}</span>
        </span>
        {d.decision_ms != null && (
          <span className="text-fg-muted" title="How long the dispatcher took to make this decision (criterion 3)">
            decided in <span className="font-mono text-fg">{d.decision_ms.toFixed(2)} ms</span>
          </span>
        )}
        <span className="font-mono text-xs text-fg-muted">{formatTime(d.created_at)}</span>
        <span className="font-mono text-xs text-fg-muted" title={d.snapshot_digest}>
          snapshot {shortDigest(d.snapshot_digest)}
        </span>
      </div>
      {d.shadow_chosen_node_id != null && (
        <p className="text-xs text-fg-muted">
          Unpinned, the planner would have picked <span className="font-mono text-fg">{d.shadow_chosen_node_id}</span> (
          {seconds(d.shadow_estimate_s)} predicted).
        </p>
      )}
      {d.replay_version == null && (
        <p className="text-2xs text-fg-subtle">
          This decision has no replay snapshot (recorded before V0.14, or over the size cap), so the offline evaluator
          cannot re-run it.
        </p>
      )}
      {d.margin_sentence != null && (
        <p className="text-sm">
          <span className="font-medium">Margin test:</span> {d.margin_sentence}
        </p>
      )}
      {rejectedOnly(d) && (
        <p role="note" className="text-sm text-status-degraded">
          Every candidate was rejected: no node can take this job {d.outcome === "failed" ? "at all" : "right now"}.
        </p>
      )}
    </div>
  );
}

/**
 * The plan explanation (D176, V0.14 G5 D300; GET /v1/jobs/{id}/decisions -
 * exactly what `compute explain` prints, criterion 4): every decision, its
 * objective, policy, calibration version and replay; every candidate -
 * chosen, ranked, rejected - with each term's prediction, source, calibration
 * factor and note, the risk term, the total, rank and rejection reason; a
 * split job's node sets, chunking, rates and margin test with the server's
 * margin sentence; and once the attempt ended, predicted vs actual per term.
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
      {pinned && (
        <p className="text-xs text-fg-muted">
          This job is pinned to a node: the planner only checked that node, and records what it would have picked
          unpinned when it can.
        </p>
      )}
      {decisions.map((d) => (
        <section key={d.id} aria-label={`Decision ${String(d.id)}`} className="flex flex-col gap-2">
          <DecisionHeader decision={d} />
          <CandidateRows decision={d} />
          <ActualRows decision={d} />
        </section>
      ))}
    </div>
  );
}
