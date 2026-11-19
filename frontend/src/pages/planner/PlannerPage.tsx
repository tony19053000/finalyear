/**
 * Planner (V0.14 G5, D300): how the planner corrects itself and how good it is.
 *
 * Layout: the restyled pages' language - the big title with the page's text
 * tabs centred on its row and, right, a round action only where the open tab
 * has one (admins: Run evaluation, Train now); one open row of headline
 * figures (planner/PlannerHeadline.tsx); then the open tab:
 *
 * * **Calibration** (`GET /v1/planner/calibration`, CalibrationTab.tsx): the
 *   shown version's factors and the version history with the held-out gate.
 * * **Evaluation** (`GET /v1/planner/eval-reports`, D318, EvaluationTab.tsx):
 *   the newest reports and the selected one's verdicts, regret per policy and
 *   numbers; `POST` starts one (admins, D378).
 * * **Models** (V0.15 G5b, D374, ModelsTab.tsx): the ML model sets, their
 *   gate numbers, predicted vs actual and pin / unpin / train.
 *
 * Every number is the API's. Selection lives in the URL (`?tab=evaluation`,
 * `?tab=models`, `?calibration=N`, `?report=ID` - which opens the Evaluation
 * tab by itself -, `?set=V`), so a view can be linked. Inactive tabs are not
 * mounted.
 */
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { useSession } from "../../api/hooks";
import { useDocumentTitle } from "../../app/useDocumentTitle";
import { TextTabs, type TabItem } from "../benchmarks/benchUi";
import { CalibrationTab } from "./CalibrationTab";
import { EvaluationTab, RunEvaluation } from "./EvaluationTab";
import { ModelsTab, TrainButton } from "./ModelsTab";
import { PlannerHeadline } from "./PlannerHeadline";

type PlannerTab = "calibration" | "evaluation" | "models";

const TABS: readonly TabItem[] = [
  { value: "calibration", label: "Calibration" },
  { value: "evaluation", label: "Evaluation" },
  { value: "models", label: "Models" },
];

function positiveInt(raw: string | null): number | null {
  if (raw === null || !/^[1-9][0-9]{0,15}$/.test(raw)) return null;
  return Number(raw);
}

function tabOf(params: URLSearchParams): PlannerTab {
  const tab = params.get("tab");
  if (tab === "models" || tab === "evaluation") return tab;
  // a report link (`?report=ID`) is a link to the evaluation
  if (tab === null && params.has("report")) return "evaluation";
  return "calibration";
}

export function PlannerPage() {
  useDocumentTitle("Planner");
  const session = useSession();
  const admin = session.data?.user.role === "admin";
  const [params, setParams] = useSearchParams();
  const calibration = positiveInt(params.get("calibration"));
  const report = positiveInt(params.get("report"));
  const tab = tabOf(params);
  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace: true });
  };
  const showReport = (id: number) => {
    update({ tab: "evaluation", report: String(id) });
  };

  let panel: ReactNode;
  let action: ReactNode = null;
  if (tab === "evaluation") {
    panel = <EvaluationTab selected={report} onSelect={showReport} />;
    if (admin) action = <RunEvaluation onStarted={showReport} />;
  } else if (tab === "models") {
    panel = (
      <ModelsTab
        selected={params.get("set")}
        onSelect={(v) => {
          update({ set: v });
        }}
      />
    );
    if (admin) action = <TrainButton />;
  } else {
    panel = (
      <CalibrationTab
        version={calibration}
        onSelect={(v) => {
          update({ calibration: v === null ? null : String(v) });
        }}
      />
    );
  }

  return (
    <>
      <div className="mt-3 mb-8 grid grid-cols-[1fr_auto_1fr] items-end gap-x-8">
        <h1 className="font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">Planner</h1>
        <div className="flex justify-center pb-1.5">
          <TextTabs
            label="Planner sections"
            idPrefix="planner"
            items={TABS}
            value={tab}
            onChange={(value) => {
              // with a report link open, Calibration is named (`?report=` alone opens Evaluation)
              update({ tab: value === "calibration" && !params.has("report") ? null : value });
            }}
          />
        </div>
        <div className="flex min-w-0 items-center justify-end gap-4 pb-1">{action}</div>
      </div>
      <PlannerHeadline />
      <div role="tabpanel" id={`planner-panel-${tab}`} aria-labelledby={`planner-tab-${tab}`} key={tab} className="nd-in">
        {panel}
      </div>
    </>
  );
}
