import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a local backend (DEMO_MODE=true PORT=18090 python -m app.serve).
const backend = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env.AGENT_HUB_BACKEND ?? "http://127.0.0.1:18090";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": { target: backend, ws: true } },
  },
});
