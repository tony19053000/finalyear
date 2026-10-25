import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { applyTheme, storedTheme } from "../app/theme";
import "../styles/index.css";
import { ConsoleApp } from "./ConsoleApp";

// The laptop owner's local console (V0.17 G3, D391/D392): served by the agent
// on http://127.0.0.1:47130 only. Same design system as the dashboard.
applyTheme(storedTheme());

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element");
}
createRoot(root).render(
  <StrictMode>
    <ConsoleApp />
  </StrictMode>,
);
