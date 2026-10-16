import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import { applyTheme, storedTheme } from "./app/theme";
import "./styles/index.css";

// Before the first render (no inline script: the CSP forbids it). index.html
// already says data-theme="dark", so a dark-mode user sees no flash.
applyTheme(storedTheme());

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element");
}
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
