import { BlankShell } from "../../../components/blank-shell";

/**
 * 外壳段固定动态渲染:`useSearchParams` 在服务端就拿到真实查询串,不会退回客户端渲染,
 * 页面因此不必再包 Suspense。页面级 Suspense 边界会在客户端导航加载页面代码块时先提交骨架屏,
 * 再被 React 的揭示节流扣住约 300 ms;没有它时导航挂起直接冒到路由过渡,旧页留着直到新页就绪。
 * 同理外壳内不要加 `loading.tsx`(见 docs/SHELL_PERCEIVED_LOADING.md)。
 */
export const dynamic = "force-dynamic";

export default async function ProtectedLayout({ children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) { const { locale } = await params; return <BlankShell locale={locale}>{children}</BlankShell>; }
