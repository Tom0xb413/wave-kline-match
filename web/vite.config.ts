import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 41789,
    proxy: {
      "/api": "http://127.0.0.1:18765",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
