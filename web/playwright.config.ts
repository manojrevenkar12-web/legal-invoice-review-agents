import { defineConfig, devices } from "@playwright/test";

// The API needs a seeded database: `lira demo --seed-only` (CI does this) with LIRA_DATABASE_URL set.
const db = process.env.LIRA_DATABASE_URL ?? "postgresql://lira:lira@localhost:5432/lira";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: { baseURL: "http://127.0.0.1:5173", trace: "retain-on-failure" },
  projects: [{ name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    {
      command: "../.venv/bin/lira serve --host 127.0.0.1 --port 8080",
      url: "http://127.0.0.1:8080/readyz",
      env: { LIRA_DATABASE_URL: db, LIRA_WORKER: "inline" },
      reuseExistingServer: true,
      timeout: 120_000,
    },
    { command: "npm run build && npm run preview", url: "http://127.0.0.1:5173", reuseExistingServer: true, timeout: 120_000 },
  ],
});
