import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 127.0.0.1, not localhost: on this machine another app holds 127.0.0.1:5173, and Node may
    // resolve localhost to ::1 while uvicorn listens on IPv4.
    host: "127.0.0.1",
    port: 5199,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
