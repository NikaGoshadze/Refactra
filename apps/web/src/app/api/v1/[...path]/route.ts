import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const AUTH_READ_ROUTES = new Set(["auth/github/login", "auth/github/callback", "auth/session"]);
const AUTH_COOKIES = new Set([
  "refactra_oauth", "refactra_session", "__Host-refactra_oauth", "__Host-refactra_session",
]);

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const relative = path.join("/");
  const isAuth = AUTH_READ_ROUTES.has(relative) || relative === "auth/logout";
  const readAllowed = AUTH_READ_ROUTES.has(relative) || relative === "capabilities" || /^mock\/runs\/[0-9a-f-]{36}(\/events)?$/i.test(relative);
  const writeAllowed = relative === "mock/runs" || relative === "auth/logout";
  if (!(request.method === "GET" ? readAllowed : request.method === "POST" && writeAllowed)) {
    return NextResponse.json({ error: { code: "unavailable_route", message: "This route is not available in the foundation build." } }, { status: 404 });
  }
  try {
    const headers = new Headers({ "Content-Type": "application/json" });
    const key = request.headers.get("Idempotency-Key");
    if (key) headers.set("Idempotency-Key", key);
    if (isAuth) {
      const cookies = (request.headers.get("cookie") ?? "").split(";").map((item) => item.trim())
        .filter((item) => AUTH_COOKIES.has(item.slice(0, item.indexOf("=")))).join("; ");
      if (cookies) headers.set("Cookie", cookies);
      // Preserve browser-supplied values; inventing a trusted Origin would bypass CSRF checks.
      for (const name of ["Origin", "X-Refactra-CSRF"]) {
        const value = request.headers.get(name);
        if (value !== null) headers.set(name, value);
      }
    }
    const upstream = await fetch(`${process.env.REFACTRA_API_URL ?? "http://127.0.0.1:8000"}/api/v1/${relative}${request.nextUrl.search}`, {
      method: request.method, headers,
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store", redirect: "manual",
      signal: AbortSignal.timeout(relative === "auth/github/callback" ? 25000 : 5000),
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    for (const name of ["Content-Type", "Location"]) {
      const value = upstream.headers.get(name);
      if (value !== null) responseHeaders.set(name, value);
    }
    if (isAuth) {
      for (const cookie of upstream.headers.getSetCookie()) responseHeaders.append("Set-Cookie", cookie);
    }
    return new NextResponse(upstream.status === 204 ? null : await upstream.text(), {
      status: upstream.status, headers: responseHeaders,
    });
  } catch {
    return NextResponse.json({ error: { code: "api_unavailable", message: "The local API is unavailable. Start the Compose stack, then retry." } }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
