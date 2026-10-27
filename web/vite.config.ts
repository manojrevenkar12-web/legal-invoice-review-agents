/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = process.env.LIRA_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/v1": api, "/healthz": api } },
  preview: { port: 5173, proxy: { "/v1": api, "/healthz": api } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"], include: ["src/**/*.test.tsx", "src/**/*.test.ts"] },
});
