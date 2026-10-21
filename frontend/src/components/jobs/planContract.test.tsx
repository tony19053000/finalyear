/**
 * V0.14 criterion 4 (D300, D320) - the Plan tab half of the explain contract.
 *
 * `src/test/fixtures/explainContract.json` is generated from the controller's
 * own answer code (tests/_support/mf_explain_contract.py). Its `fields` are
 * every leaf of the decisions answer that `compute explain` shows - proven
 * on the Python side by tests/unit/test_v014_g5_explain_contract.py, which
 * also proves `fields` + `not_shown` is the whole answer model. Here: changing
 * any one of those fields changes what the Plan tab renders, so the tab shows
 * the same field set as the CLI.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import contract from "../../test/fixtures/explainContract.json";
import type { DecisionList } from "../../pages/jobs/jobsApi";
import { PlanTable } from "./PlanTable";

interface Field {
  field: string;
  path: (string | number)[];
  to: unknown;
}

const payload = contract.payload as unknown as DecisionList;
const fields = contract.fields as unknown as Field[];

function text(list: DecisionList): string {
  const { container, unmount } = render(<PlanTable decisions={list.decisions} pinned={false} />);
  const shown = container.textContent;
  unmount();
  return shown;
}

function changed(path: Field["path"], to: unknown): DecisionList {
  const copy = structuredClone(payload) as unknown as Record<string | number, unknown>;
  let node: Record<string | number, unknown> = copy;
  for (const token of path.slice(0, -1)) node = node[token] as Record<string | number, unknown>;
  const last = path[path.length - 1];
  if (last === undefined) throw new Error("empty path");
  node[last] = to;
  return copy as unknown as DecisionList;
}

describe("explain contract (criterion 4): the Plan tab shows every field compute explain shows", () => {
  const base = text(payload);

  it("lists the fields the Python side checks (non-empty, decisions-scoped)", () => {
    expect(fields.length).toBeGreaterThan(40);
    for (const f of fields) expect(f.path[0]).toBe("decisions");
    expect(contract.not_shown.map((n) => n.field)).toContain("job_id");
  });

  it.each(fields.map((f) => [f.field, f] as const))("shows %s", (_name, f) => {
    // the job-level fields are outside PlanTable (the page is the job's)
    expect(text(changed(f.path, f.to))).not.toBe(base);
  });

  it("shows the server's sentences verbatim, never a recomputed one", () => {
    const list = structuredClone(payload);
    const split = list.decisions[1];
    const term = list.decisions[0]?.candidates[0]?.estimate?.terms.transfer;
    if (split === undefined || term === undefined) throw new Error("fixture shape changed");
    split.margin_sentence = "SENTENCE-FROM-THE-SERVER";
    term.note = "NOTE-FROM-THE-SERVER";
    const shown = text(list);
    expect(shown).toContain("SENTENCE-FROM-THE-SERVER");
    expect(shown).toContain("NOTE-FROM-THE-SERVER");
    expect(shown).not.toContain("split over 3 nodes");
  });
});
