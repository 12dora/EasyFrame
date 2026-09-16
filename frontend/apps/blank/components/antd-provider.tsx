"use client";

import dynamic from "next/dynamic";
import type { ReactNode } from "react";
import type { Locale } from "../lib/messages";

// 只把当前语言的 antd locale pack 带进受保护壳的 bundle(EasyUI 的 `./antd` 桶会同时拉入两份)。
// `ssr: true` 让服务端 HTML 与 hydrate 用同一份 locale,否则首帧文案会闪一下。
const ProviderZh = dynamic(() => import("@easy-enterprise/ui/antd/provider-zh").then((m) => m.EasyAntdProviderZh), { ssr: true });
const ProviderEn = dynamic(() => import("@easy-enterprise/ui/antd/provider-en").then((m) => m.EasyAntdProviderEn), { ssr: true });

/**
 * antd 6 环境:主题 token 与 locale 全部来自 EasyUI(`@easy-enterprise/ui/antd`)。
 *
 * 由 `BlankShell` 在应用内容外**只包一层**;页面里不要再嵌套带 `cssVar` 的
 * `ConfigProvider`,否则 antd 会注入两份 `--ant-*` 自定义属性互相打架。
 * 页面级主题走 `token` / `components` 两个属性(见 EasyUI src/README.md「表格」一节)。
 */
export function BlankAntdProvider({ locale, children }: { locale: Locale; children: ReactNode }) {
  return locale === "en" ? <ProviderEn>{children}</ProviderEn> : <ProviderZh>{children}</ProviderZh>;
}
