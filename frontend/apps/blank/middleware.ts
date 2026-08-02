import { NextResponse, type NextRequest } from "next/server";

const LOCALIZED_PATH = /^\/(zh-CN|en)(?:\/|$)/;

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const saved = request.cookies.get("NEXT_LOCALE")?.value;
  const pathLocale = pathname.match(LOCALIZED_PATH)?.[1];
  const locale = pathLocale === "en" || pathLocale === "zh-CN" ? pathLocale : saved === "en" ? "en" : "zh-CN";
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-enterprise-locale", locale);
  if (LOCALIZED_PATH.test(pathname) || pathname.startsWith("/api/") || pathname.startsWith("/_next/") || pathname === "/login/oidc-complete") {
    return NextResponse.next({ request: { headers: requestHeaders } });
  }
  const target = request.nextUrl.clone(); target.pathname = `/${locale}${pathname}`;
  return NextResponse.redirect(target);
}

export const config = { matcher: ["/((?!.*\\..*).*)"] };
