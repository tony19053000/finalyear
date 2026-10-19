// Vitest setup: unmount React trees after every test; fill jsdom gaps.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import { setSigningOut } from "../api/client";

// Route-level code splitting (V0.8 G5 item 0f): the pages are `React.lazy`.
// Load their modules once here, so a page test's first `findBy` (1 s default)
// never includes the time to transform a page and its libraries (Recharts).
// In the browser the chunks are tiny same-origin files; this is test-only.
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
]);

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
