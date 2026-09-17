import { createRequire } from "node:module";
import { dirname } from "node:path";
import { defineConfig } from "vitest/config";

/**
 * Adapter- and hook-level unit tests for the blank host.
 * Run via the monorepo vitest binary (blank does not ship vitest as a dep):
 *
 *   pnpm --dir packages/easy-enterprise exec vitest run --config ../../apps/blank/vitest.config.ts
 *
 * (from frontend/)
 */

// 宿主这一侧看到的那份 react(pnpm store 里的实路径)。
const require = createRequire(import.meta.url);
const reactDir = dirname(require.resolve("react/package.json"));
const reactDomDir = dirname(require.resolve("react-dom/package.json"));
const antdDir = dirname(require.resolve("antd/package.json"));

export default defineConfig({
  root: import.meta.dirname,
  // `lib/table-query.test.tsx` 挂 React 组件跑 hook,需要 automatic JSX。
  esbuild: { jsx: "automatic" },
  // EasyUI 自带一份 react 作为 devDependency;不去重的话 kit 里的 hook 会跑在第二个
  // react 实例上("Cannot read properties of null (reading 'useState')")。
  //
  // `dedupe` 只管到宿主自己解析的那几个 bare import:EasyUI 的 peer 依赖(motion /
  // framer-motion 住在 `packages/easy-enterprise/node_modules/.pnpm` 下)从它自己那层
  // 解析 react,照样会拿到第二份。所以再按实路径钉一次 —— 这样 `MobileNav` 这类带动效的
  // kit 组件在宿主的用例里能用真的,不必换替身。
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: [
      { find: /^react$/, replacement: `${reactDir}/index.js` },
      { find: /^react\//, replacement: `${reactDir}/` },
      { find: /^react-dom$/, replacement: `${reactDomDir}/index.js` },
      { find: /^react-dom\//, replacement: `${reactDomDir}/` },
      // antd 同理:EasyUI 那层的 antd 是跟 react 19.2.8 配对安装的,`Pagination` /
      // `Checkbox` 里的 hook 会跑在第二份 react 上。钉到宿主这一份(它配的就是上面那个 react)。
      { find: /^antd$/, replacement: `${antdDir}/es/index.js` },
      { find: /^antd\//, replacement: `${antdDir}/` },
    ],
  },
  test: {
    // happy-dom (not node): the shell identity store lives in sessionStorage and
    // `components/use-shell-identity.test.tsx` mounts a real React root.
    environment: "happy-dom",
    globals: false,
    // node_modules 里的依赖默认走 node 解析(不经 Vite),上面的别名对它们不生效;
    // motion / framer-motion 内联进来才会跟宿主共用同一份 react。
    server: { deps: { inline: [/framer-motion/, /[\\/]motion[\\/]/] } },
    include: ["lib/**/*.test.{ts,tsx}", "components/**/*.test.{ts,tsx}"],
  },
});
