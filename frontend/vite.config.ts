import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import path from "path"

// In dev, Vite serves the UI and proxies the real API + demo routes to the
// FastAPI process. In production the built output is served by FastAPI itself,
// so the same relative paths work in both.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/demo": "http://127.0.0.1:8000",
    },
  },
})
