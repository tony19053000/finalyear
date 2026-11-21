import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { applyTheme } from "../app/theme";
import "../styles/index.css";
import { consoleDemoRequested, installConsoleDemo } from "../demo/consoleDemo";
import { ConsoleApp } from "./ConsoleApp";

// The laptop owner's local console (V0.17 G3, D391/D392): served by the agent
// on http://127.0.0.1:47130 only. Same design system as the dashboard.
// The console is dark only (no theme switch), like the hive skin it shares.
applyTheme("dark");
// DEMO MODE (localhost ?demo=1 only; delete src/demo/consoleDemo.ts and these lines to remove).
if (consoleDemoRequested()) installConsoleDemo();

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element");
}
createRoot(root).render(
  <StrictMode>
    <ConsoleApp />
  </StrictMode>,
);
