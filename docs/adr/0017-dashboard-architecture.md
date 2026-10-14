# ADR-0017: Dashboard architecture (V0.8)

> **Status:** Accepted (2026-10-15) — team approved the V0.8 brief ("go with your suggestions") · **Deciders:** team + Lead

## Context
V0.8 (roadmap) needs an infrastructure-grade web dashboard and a polished CLI covering every Mode 1 operation, with
live updates ≤ 2 s (brief §24). The controller already has HTTPS REST under `/v1` with an OpenAPI spec, per-user API
tokens and a fabric CA (ADR-0016). Details and rulings: `docs/briefs/V0.8-dashboard-cli.md` D169–D180.

## Decision
1. **SPA served by the controller.** React + TypeScript + Vite in `frontend/`; built files served from
   `controller/web/dist` with SPA fallback; Node is build-time only (D169).
2. **Browser auth = HttpOnly session cookie** (`browser_session` token kind, 12 h idle / 7 d absolute) + double-submit
   CSRF header; API tokens never enter the browser (D170).
3. **Typed client generated from OpenAPI** with a drift test (D171).
4. **One event stream** `/v1/events/ws` from an in-controller event bus, `seq` + resume/resync, bounded queues; the
   client patches TanStack Query caches (D172). Live metrics from an in-memory ring, no DB in V0.8 (D173).
5. **Fabric CA in browsers:** a helper prints per-OS steps to trust `ca.crt`; plain HTTP is allowed only on loopback
   for the controller host's own browser. Clicking through warnings is never the documented path.
6. **e2e with pytest-playwright** on the cluster harness (D179). Browser job submit reuses the CAS upload API (D177).

## Alternatives considered
- Separate frontend server (Next.js/Node): an extra process and runtime for no gain on a LAN tool.
- Bearer token in `localStorage`: simple, but any XSS steals a long-lived credential.
- Per-resource polling: N requests/s per tab and no ≤ 2 s guarantee at scale.
- TypeScript Playwright: would need a second fixture layer beside the Python harness.
- Plain HTTP dashboard on the LAN: undoes V0.7 criterion 3 (no plaintext on the wire).

## Trade-offs
+ One deployable process; credentials stay server-side; backend changes that break the UI fail CI.
− Developers who build the frontend need Node 22; users must trust the fabric CA once per machine; the event bus is
  new in-memory state in the controller (bounded, lost on restart — clients resync).

## Consequences
New `frontend/`, `controller/events/`, `controller/web/`, `tests/e2e/`; THREAT_MODEL rows for cookie/CSRF/session
fixation; V0.9 replaces the metrics source (ring → telemetry store) without changing the UI contract. Revisit if the
controller ever runs behind a public reverse proxy (then CSP, cookies and CA trust need another review).
