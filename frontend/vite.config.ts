// MeshForge dashboard build (V0.8 G2, D169 / ADR-0017).
//
// `npm run build` writes the SPA into the controller package
// (src/meshforge/controller/web/dist, git-ignored); the controller serves it
// with the CSP of controller/web/spa.py. Node is a build-time tool only.
//
// `npm run dev` proxies /v1 (HTTP + WebSocket) to a local controller:
//   MESHFORGE_DEV_CONTROLLER=https://127.0.0.1:47110 npm run dev
// Trust the fabric CA for the proxy with NODE_EXTRA_CA_CERTS=<ca.crt>.
// The proxy rewrites Origin to the controller's own, because the controller
// refuses cross-origin logins and cookie WebSockets (D170) - dev only.
//
// V0.17 G3 (D391): `vite build --mode console` builds the agent's local console
// (second entry, src/agentConsole/index.html) into the agent package
// (src/meshforge/agent/console/dist, git-ignored) - same components, same
// tokens, same CSP rules (scripts/check-dist.mjs runs on both outputs).
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** File-name prefix of the qrcode-generator chunk (scripts/check-dist.mjs). */
const QR_CHUNK = "qrcode-generator";
const controller = process.env.MESHFORGE_DEV_CONTROLLER ?? "https://127.0.0.1:47110";
const outDir = fileURLToPath(
  new URL("../src/meshforge/controller/web/dist", import.meta.url),
);
const consoleOutDir = fileURLToPath(
  new URL("../src/meshforge/agent/console/dist", import.meta.url),
);
const consoleRoot = fileURLToPath(new URL("./src/agentConsole", import.meta.url));
const publicDir = fileURLToPath(new URL("./public", import.meta.url));

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  // The console's index.html lives in src/agentConsole, so it is the root there
  // (the output then has index.html at its top, which the agent serves).
  root: mode === "console" ? consoleRoot : undefined,
  publicDir,
  build: {
    outDir: mode === "console" ? consoleOutDir : outDir,
    emptyOutDir: true,
    // Never inline assets as data: URIs - the CSP has `font-src 'self'`, so an
    // inlined font would be refused; one file per asset keeps it honest.
    assetsInlineLimit: 0,
    sourcemap: false,
    target: "es2022",
    rollupOptions: {
      output: {
        // D204 (V0.8 G7): qrcode-generator gets a chunk of its own, so the one
        // CSP exception scripts/check-dist.mjs grants it (dead `<table style=`
        // builders) applies to that file name only, never to app code.
        manualChunks(id) {
          return id.includes("/node_modules/qrcode-generator/") ? QR_CHUNK : undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      // `npm run dev -- --mode console`: the local console of an agent on this
      // laptop (the agent checks Host and Origin, so both are rewritten).
      "/console": {
        target: "http://127.0.0.1:47130",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => {
            req.setHeader("origin", "http://127.0.0.1:47130");
          });
        },
      },
      "/v1": {
        target: controller,
        changeOrigin: true,
        ws: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => {
            req.setHeader("origin", controller);
          });
          proxy.on("proxyReqWs", (req) => {
            req.setHeader("origin", controller);
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
}));
