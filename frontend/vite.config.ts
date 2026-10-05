import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  server: {
    proxy: { "/jobs": api, "/metrics": api, "/webhooks": api, "/health": api },
  },
});
