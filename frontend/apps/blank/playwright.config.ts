import { defineConfig } from "@playwright/test";

/**
 * Default e2e port is 3110 (not 3100) so a running docker-compose blank-frontend
 * on BLANK_FRONTEND_PORT=3100 is never reused by accident. That reuse was the
 * root cause of the local-accounts v2 suite failing against a stale image while
 * the local workspace (and mocks) were already correct.
 *
 * Override with BLANK_E2E_FRONTEND_URL (e.g. make blank-check against a fresh
 * compose stack) to skip the local webServer entirely.
 */
const e2ePort = Number(process.env.BLANK_E2E_PORT ?? 3110);
const baseURL = process.env.BLANK_E2E_FRONTEND_URL ?? `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: { baseURL },
  webServer: process.env.BLANK_E2E_FRONTEND_URL
    ? undefined
    : {
        // Requires a prior `pnpm --dir apps/blank build` (or `pnpm blank:build`).
        command: `pnpm exec next start -p ${e2ePort}`,
        url: `${baseURL}/zh-CN/login`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
