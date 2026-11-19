// Dev server for the hive. dashboard: vite + proxy to the WSL test controller.
import { fileURLToPath, pathToFileURL } from "node:url";
// mf-dev/frontend, found from this file (lab/coord/DEV/dash-dev.mjs), so it works on any laptop
const root = fileURLToPath(new URL("../../../frontend/", import.meta.url)).replace(/[\/]$/, "");
process.chdir(root);
process.env.MESHFORGE_DEV_CONTROLLER ??= "http://127.0.0.1:47111";
const { createServer } = await import(pathToFileURL(`${root}/node_modules/vite/dist/node/index.js`).href);
const server = await createServer({ root, server: { port: 5173, strictPort: true, host: "127.0.0.1" } });
await server.listen();
server.printUrls();
