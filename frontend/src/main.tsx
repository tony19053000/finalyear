import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createQueryClient } from "./api/queryClient";
import { App } from "./app/App";
import { applyTheme } from "./app/theme";
import "./styles/index.css";

// The dashboard is dark only (no theme toggle in the top bar): a "light"
// choice stored by an older build is ignored. index.html already says
// data-theme="dark"; this pins it before the first render (no inline script:
// the CSP forbids it). theme.ts keeps its storage helpers for the agent console.
applyTheme("dark");

const queryClient = createQueryClient();
// DEMO MODE (vite dev server + localhost ?demo=1 only; delete src/demo/ and these
// lines to remove). `import.meta.env.DEV` is the constant false in `npm run build`,
// so this block and the demo modules are not in the production bundle at all.
if (import.meta.env.DEV) {
  const demo = await import("./demo/install");
  if (demo.demoRequested()) demo.installDemo(queryClient);
}

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element");
}
createRoot(root).render(
  <StrictMode>
    <App queryClient={queryClient} />
  </StrictMode>,
);
