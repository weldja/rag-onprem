import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` (needs Node on the host) proxies the API to the running stack.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
  server: {
    proxy: {
      "/api": { target: "https://127.0.0.1:8443", changeOrigin: true, secure: false },
    },
  },
});
