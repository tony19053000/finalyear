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
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const controller = process.env.MESHFORGE_DEV_CONTROLLER ?? "https://127.0.0.1:47110";
const outDir = fileURLToPath(
  new URL("../src/meshforge/controller/web/dist", import.meta.url),
);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir,
    emptyOutDir: true,
    // Never inline assets as data: URIs - the CSP has `font-src 'self'`, so an
    // inlined font would be refused; one file per asset keeps it honest.
    assetsInlineLimit: 0,
    sourcemap: false,
    target: "es2022",
  },
  server: {
    proxy: {
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
});
