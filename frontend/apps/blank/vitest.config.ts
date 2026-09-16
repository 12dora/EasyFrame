import { defineConfig } from "vitest/config";

/**
 * Adapter- and hook-level unit tests for the blank host.
 * Run via the monorepo vitest binary (blank does not ship vitest as a dep):
 *
 *   pnpm --dir packages/easy-enterprise exec vitest run --config ../../apps/blank/vitest.config.ts
 *
 * (from frontend/)
 */
export default defineConfig({
  root: import.meta.dirname,
  // `lib/table-query.test.tsx` 挂 React 组件跑 hook,需要 automatic JSX。
  esbuild: { jsx: "automatic" },
  // EasyUI 自带一份 react 作为 devDependency;不去重的话 kit 里的 hook 会跑在第二个
  // react 实例上("Cannot read properties of null (reading 'useState')")。
  resolve: { dedupe: ["react", "react-dom"] },
  test: {
    // happy-dom (not node): the shell identity store lives in sessionStorage and
    // `components/use-shell-identity.test.tsx` mounts a real React root.
    environment: "happy-dom",
    globals: false,
    include: ["lib/**/*.test.{ts,tsx}", "components/**/*.test.{ts,tsx}"],
  },
});
