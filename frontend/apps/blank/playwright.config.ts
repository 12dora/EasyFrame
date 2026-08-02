import { defineConfig } from "@playwright/test";

const baseURL = process.env.BLANK_E2E_FRONTEND_URL ?? "http://localhost:3100";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: { baseURL },
  webServer: process.env.BLANK_E2E_FRONTEND_URL
    ? undefined
    : {
        command: "pnpm exec next start -p 3100",
        url: `${baseURL}/zh-CN/login`,
        reuseExistingServer: true,
        timeout: 120_000,
      },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
