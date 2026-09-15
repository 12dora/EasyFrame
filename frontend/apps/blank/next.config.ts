import type { NextConfig } from "next";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(fileURLToPath(import.meta.url));
const internalBackendOrigin = process.env.INTERNAL_BACKEND_ORIGIN || "http://blank-backend:8000";
const isDev = process.env.NODE_ENV !== "production";
const scriptSrc = isDev ? "'self' 'unsafe-inline' 'unsafe-eval'" : "'self' 'unsafe-inline'";

function apiConnectOrigin(): string {
  const raw = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8100";
  try {
    return new URL(raw).origin;
  } catch {
    return "";
  }
}

/**
 * Authentik 源(如 https://auth.jiefakj.com),来自构建期 NEXT_PUBLIC_OIDC_PROVIDER_ORIGIN。
 * 登出要把 end-session 表单 POST 给它(见 EasyUI docs/LOGOUT.md);form-action 少这一源时
 * 浏览器会静默拦掉表单 —— 不抛异常、页面不动,而本地会话已经清了。未配置时不放行任何外部源,
 * 登出回落到 endSessionUrl 顶层 GET。
 */
function oidcProviderOrigin(): string {
  const raw = process.env.NEXT_PUBLIC_OIDC_PROVIDER_ORIGIN?.trim();
  if (!raw) return "";
  try {
    return new URL(raw).origin;
  } catch {
    return "";
  }
}

function sourceList(...values: string[]): string {
  return [...new Set(values.filter(Boolean))].join(" ");
}

const connectSrc = sourceList("'self'", apiConnectOrigin());
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src ${scriptSrc}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src ${connectSrc}`,
  "frame-src 'self' blob:",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "object-src 'none'",
  sourceList("form-action 'self'", oidcProviderOrigin()),
].join("; ");

const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
];

const nextConfig: NextConfig = {
  output: "standalone",
  transpilePackages: ["@easy-enterprise/ui"],
  turbopack: { root: resolve(projectRoot, "../..") },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${internalBackendOrigin}/api/:path*` },
      { source: "/.well-known/easyauth-app.json", destination: `${internalBackendOrigin}/.well-known/easyauth-app.json` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
