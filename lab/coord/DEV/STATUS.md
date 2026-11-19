# DEV STATUS — 2026-11-19 (evening)

- **Current:** dashboard restyle pushed as wip on `dev/ui`; DEV moves to a new laptop. See HANDOFF.md → NEWEST.
- **LEAD orders (chat 20261119-185955):** merge to main LATER. Before merge on dev/ui: (a) remove demo fake admin session (keep ?demo=1 localhost-only, no auth bypass), (b) fix g2_tester theme-toggle test + review the 6 changed g5_settings tests, (c) npm test, npm run check:api, tsc, eslint green, (d) DEV note listing every changed page. Then SCREENSHOTS.md (36 controller screenshots), then website with LEAD's content draft (real numbers), video-ready pages (Overview speed-up, a split job, Planner evaluation).
- **Not in the commit:** test creds file, frontend-src-backup-* / website-backup-* folders, Blender _work/renders, website buzz-preview-*.png render previews (~8 MB; regenerate with website/blender/buzz3d.py).
- **New laptop:** clone, `git checkout dev/ui`, `cd frontend && npm ci`, run tsc/eslint/vitest; copy creds by hand.
