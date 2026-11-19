// Dev server for the hive. dashboard: vite + proxy to the WSL test controller.
import { pathToFileURL } from "node:url";
const root = "C:/Users/HP/UI MeshForge/mf-dev/frontend";
process.chdir(root);
process.env.MESHFORGE_DEV_CONTROLLER ??= "http://127.0.0.1:47111";
const { createServer } = await import(pathToFileURL(`${root}/node_modules/vite/dist/node/index.js`).href);
const server = await createServer({ root, server: { port: 5173, strictPort: true, host: "127.0.0.1" } });
await server.listen();
server.printUrls();
