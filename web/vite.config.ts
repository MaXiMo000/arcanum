import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// In development the API runs separately (python -m arcanum.server); the dev server forwards to it,
// so the page and the API share an origin exactly as they do in production. ARCANUM_API (in the
// environment or web/.env) points it at an API elsewhere.
export default defineConfig(({ mode }) => {
  const api = loadEnv(mode, ".", "ARCANUM_").ARCANUM_API || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/sessions": { target: api, ws: true },
        "/health": api,
      },
    },
    build: {
      sourcemap: false,
      assetsInlineLimit: 0, // keep every asset a separate file, so the strict CSP needs no data: scripts
    },
  };
});
