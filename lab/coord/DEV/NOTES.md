# DEV notes (branch dev/ui)

DEV = 5th computer for UI, docs, briefing, website. Not a fabric worker. Never pushes to main or lab/L*.

## Log
- 2026-11-17 04:03 DEV online. Repo in "UI MeshForge\mf-dev", branch dev/ui pushed. Sent lab/chat/20261117-040308-DEV-to-LEAD.md.

## UI redesign plan (draft - for review before building)
Today: Overview, Nodes, Jobs (+ SplitProgressPanel, ChunkGrid), Benchmarks, Planner, Settings. React 18 + Tailwind 4 + recharts + react-query, live via event stream.

1. Overview becomes a "Fabric" live view: one card per laptop (name, OS, GPU, CPU/GPU/RAM meters, current chunk), running jobs below, alerts in a side strip.
2. Job detail: plan decision on top in plain words (mode, nodes, why), then split timeline per laptop (Gantt-like bars from chunk records), then speedup vs local estimate.
3. Speedup panel: reusable chart (speedup per run, ideal line) used on Job detail, Benchmarks and the presentation view.
4. Presentation view (/present, read-only, large type, dark): 4 laptop tiles with live progress, current job, running speedup counter; reuses existing hooks/events, no new API.
5. Keep all existing tests green (npm test, check:api); new components get tests; no API changes, so no controller work.
Open: confirm with LEAD the final numbers to show (2.28x, ~2x, 1.97x/3.21x) and whether /present ships in main or only the video build.
- 2026-11-17 Set up project-style tracking: brief docs/briefs/UI-redesign.md (DRAFT), DEV/STATUS.md, DEV/HANDOFF.md. Plan above moved into the brief.
