import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles/index.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("index.html has no #root element");
}
createRoot(root).render(
  <StrictMode>
    <main>MeshForge</main>
  </StrictMode>,
);
