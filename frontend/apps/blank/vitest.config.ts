import { defineConfig } from "vitest/config";

/**
 * Adapter-level unit tests for the blank host.
 * Run via the monorepo vitest binary (blank does not ship vitest as a dep):
 *
 *   pnpm --dir packages/easy-enterprise exec vitest run --config ../../apps/blank/vitest.config.ts
 *
 * (from frontend/)
 */
export default defineConfig({
  root: import.meta.dirname,
  test: {
    environment: "node",
    globals: false,
    include: ["lib/**/*.test.ts"],
  },
});
