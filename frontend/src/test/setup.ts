// Vitest setup: unmount React trees after every test; fill jsdom gaps.
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

import { setSigningOut } from "../api/client";

// Route-level code splitting (V0.8 G5 item 0f): the pages are `React.lazy`.
// Load their modules once here, so a page test's first `findBy` (1 s default)
// never includes the time to transform a page and its libraries (Recharts).
// In the browser the chunks are tiny same-origin files; this is test-only.
//
// react-router-dom is CommonJS that `require()`s the ESM react-router
// (Node's require(esm), "module-sync" export). When the pages below are imported
// in parallel, several of them reach react-router-dom at once and Node's loader
// can race between that synchronous require and an async import of the same
// module ("Cannot read properties of undefined (reading 'getStatus')" from
// node:internal/modules/esm/loader): a random test file then fails to load with
// 0 tests. Loading it once, alone, first removes the race.
await import("react-router-dom");
await Promise.all([
  import("../pages/overview/OverviewPage"),
  import("../pages/nodes/NodesPage"),
  import("../pages/nodes/NodeDetailPage"),
  import("../pages/jobs/JobsListPage"),
  import("../pages/jobs/JobDetailPage"),
  import("../pages/jobs/submit/SubmitJobPage"),
  import("../pages/settings/SettingsRoutes"),
  import("../pages/benchmarks/BenchmarksPage"),
  import("../pages/benchmarks/CampaignDetailPage"),
  import("../pages/planner/PlannerPage"),
  import("../pages/guide/GuidePage"),
  import("../pages/setup/SetupPage"),
]);

// findBy*/waitFor default to 1 s; with ~60 files in parallel a page's first render
// can take longer on a busy machine (flaky "Unable to find"). 5 s is still far
// below the per-test limit (vite.config testTimeout), so real hangs still fail.
configure({ asyncUtilTimeout: 5_000 });

afterEach(() => {
  cleanup();
  setSigningOut(false); // module state: one test's sign-out must not leak into the next
  document.cookie.split(";").forEach((part) => {
    const name = part.split("=")[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });
});

// jsdom has HTMLDialogElement but not its modal methods (Dialog.tsx uses
// them). Minimal stand-ins: the open attribute and the close event - the
// real top-layer/inert behaviour is covered by the Playwright e2e tests.
const proto = HTMLDialogElement.prototype;
if (typeof proto.showModal !== "function") {
  proto.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
}
if (typeof proto.close !== "function") {
  proto.close = function close(this: HTMLDialogElement) {
    if (!this.hasAttribute("open")) return;
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}
