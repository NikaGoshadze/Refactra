import { defineConfig, devices } from "@playwright/test";

// Live end-to-end run against the real stack, GitHub and OpenAI, started only by
// scripts/run-live-e2e.ps1. The default config (tests/e2e) and CI never include it.
export default defineConfig({
  testDir: "./tests/e2e-live",
  timeout: 45 * 60_000,
  retries: 0,
  workers: 1,
  outputDir: process.env.REFACTRA_E2E_OUTPUT ?? ".runtime/e2e-live/results",
  reporter: [["list"]],
  use: {
    // localhost, not 127.0.0.1, so the session cookie matches the configured callback host.
    baseURL: process.env.REFACTRA_WEB_URL ?? "http://localhost:3000",
    trace: "on",
    screenshot: "on",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
