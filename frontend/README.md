# MeshForge dashboard (`frontend/`)

React 18 + TypeScript (strict) + Vite, served by the controller itself (ADR-0017, D169).
Node 22 is a **build-time** tool only; nothing JavaScript runs on the controller.

```bash
cd frontend
npm ci                 # exact versions from package-lock.json
npm run build          # -> src/meshforge/controller/web/dist/ (git-ignored), then the CSP check
```

Then open the controller's URL (`https://<controller>:47110/`) and sign in with a controller user
(`meshforge-controller user create NAME`). Trust the fabric CA first:
`meshforge-controller ca trust-helper`.

| Script | What it does |
|---|---|
| `npm run dev` | Vite dev server; proxies `/v1` (HTTP + WebSocket) to `MESHFORGE_DEV_CONTROLLER` (default `https://127.0.0.1:47110`; set `NODE_EXTRA_CA_CERTS=<ca.crt>`) |
| `npm run lint` | eslint, zero warnings allowed |
| `npm run typecheck` | `tsc -b` (app, tests, config) |
| `npm test` | vitest (jsdom) |
| `npm run build` | type check, build, then `scripts/check-dist.mjs` (fails on anything the CSP would refuse: inline script/style, `style=`, CDN URLs, inlined fonts) |
| `npm run gen:api` | export `openapi.json` from the controller (no server) and regenerate `src/api/schema.d.ts` |
| `npm run check:api` | fail if either generated file is stale (also enforced by `tests/unit/test_v08_g2_openapi_drift.py`) |

Rules the code keeps (and tests check):

- **CSP `style-src 'self'`, `script-src 'self'`** (D181): no inline `style=` (eslint forbids it), no
  `<style>` injection - hence the native `<dialog>` instead of Radix Dialog.
- **No token in the browser** except the HttpOnly session cookie; `localStorage` holds only the theme
  (`mf-theme`); nothing in `sessionStorage` or URLs.
- **One event stream** (`/v1/events/ws`): events invalidate TanStack Query caches; no second store.
- **Every number has an API source** - placeholder pages say what will be there, never fake data.
- Colours only from `src/styles/tokens.css`; every text token meets WCAG AA (unit-tested), axe-core
  runs in the e2e tests (`tests/e2e/`, `uv run pytest tests/e2e`).
