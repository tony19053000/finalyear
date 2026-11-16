# UI — Dashboard redesign, presentation view, docs, briefing, website: session plan

> **Status:** **DRAFT** — not approved. Nothing is built until the team approves §2 (U-decisions) and §7.
> Owner: DEV workstation, branch `dev/ui`. LEAD reviews; LEAD merges to main after the lab runs.
> Decision numbers use the prefix **U** (U1, U2 …) so they never clash with LEAD's D-numbers.
> Loop per group: implementer → tester → (fix rounds) → reviewer → `npm test` + `npm run check:api` once at group end → close.

## 1. What already exists (do not rebuild)

| Have | Where | UI change |
|---|---|---|
| React 18 + TS + Vite, Tailwind 4 tokens, Radix, recharts, TanStack Query | `frontend/` (D169, D175) | reuse |
| One event stream with resume/resync | `frontend/src/events/` (D172) | reuse for all live views |
| Pages: Overview, Nodes, Jobs (+ SplitProgressPanel, ChunkGrid, PlanTable), Benchmarks, Planner, Settings | `frontend/src/pages/` | redesign layout, not data layer |
| Generated API client + drift gate | `frontend/src/api/`, `npm run check:api` | no API changes planned |

## 2. Design decisions (DEV proposals — team approves)

| # | Decision | Why |
|---|---|---|
| U1 | Overview becomes a live "Fabric" view: one card per laptop (name, OS, GPU, CPU/GPU/RAM meters, current chunk), running jobs below, alerts in a side strip. | The 4-laptop fabric is the story; show it first. |
| U2 | Job detail leads with the planner's decision in plain words (mode, laptops, why), then a per-laptop split timeline, then speedup vs local. | Evaluators must see *why* the planner chose. |
| U3 | One reusable speedup chart (runs vs ideal line) for Job detail, Benchmarks and the presentation view. | One component, consistent numbers. |
| U4 | `/present`: read-only, large type, dark; 4 laptop tiles with live progress, current job, speedup counter. Reuses existing hooks/events. | For the evaluation video. |
| U5 | No API/controller changes; all existing tests stay green; new components get tests. | DEV must not touch the backend during lab runs. |

## 3. Groups

| Group | Content | Progress file |
|---|---|---|
| UI-G1 | U1 Fabric overview | `progress/UI-G1.md` |
| UI-G2 | U2 + U3 job detail + speedup chart | `progress/UI-G2.md` |
| UI-G3 | U4 presentation view | `progress/UI-G3.md` |
| UI-G4 | Docs polish (docs/user/, PROJECT_SUMMARY.md) | `progress/UI-G4.md` |
| UI-G5 | Team briefing notes | `progress/UI-G5.md` |
| UI-G6 | Static project website | `progress/UI-G6.md` |

## 7. Questions for the team
1. Final numbers to show (3-GPU split 2.28x, 2B ~2x, CPU 1.97x / 3.21x) — confirmed by LEAD?
2. Does `/present` ship in main, or only in a video build?
3. Order of groups OK?
