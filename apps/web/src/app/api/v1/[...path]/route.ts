import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const AUTH_READ_ROUTES = new Set(["auth/github/login", "auth/github/callback", "auth/session"]);
const RUN_ID_PATTERN = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const SESSION_COOKIES = new Set(["refactra_session", "__Host-refactra_session"]);
const AUTH_COOKIES = new Set([
  "refactra_oauth", "__Host-refactra_oauth", ...SESSION_COOKIES,
]);

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const relative = path.join("/");
  const isAuth = AUTH_READ_ROUTES.has(relative) || relative === "auth/logout";
  const isRepository = /^repositories\/[1-9][0-9]*$/.test(relative);
  const isAnalysisSubmit = relative === "runs";
  const [resource, id, section, action] = path;
  const isRun = resource === "runs" && RUN_ID_PATTERN.test(id ?? "");
  const isPlan = isRun && section === "plan";
  const isRunRead = isRun && (path.length === 2 || (isPlan && path.length === 3));
  const isPlanWrite = isPlan && request.method === "POST"
    && (path.length === 3 || (path.length === 4 && action === "approve"));
  const sessionOnly = isRepository || isAnalysisSubmit || isRunRead || isPlanWrite;
  const readAllowed = isRepository || isRunRead || AUTH_READ_ROUTES.has(relative)
    || relative === "capabilities" || /^mock\/runs\/[0-9a-f-]{36}(\/events)?$/i.test(relative);
  const writeAllowed = isAnalysisSubmit || isPlanWrite || relative === "mock/runs" || relative === "auth/logout";
  if (!(request.method === "GET" ? readAllowed : request.method === "POST" && writeAllowed)) {
    return NextResponse.json({ error: { code: "unavailable_route", message: "This route is not available in the foundation build." } }, { status: 404, headers: { "Cache-Control": "no-store" } });
  }
  try {
    const headers = new Headers({ "Content-Type": "application/json" });
    const key = request.headers.get("Idempotency-Key");
    if (key) headers.set("Idempotency-Key", key);
    if (isAuth || sessionOnly) {
      const allowedCookies = isAuth ? AUTH_COOKIES : SESSION_COOKIES;
      const cookies = (request.headers.get("cookie") ?? "").split(";").map((item) => item.trim())
        .filter((item) => allowedCookies.has(item.slice(0, item.indexOf("=")))).join("; ");
      if (cookies) headers.set("Cookie", cookies);
    }
    if (isAuth || isAnalysisSubmit || isPlanWrite) {
      // Preserve browser-supplied values; inventing a trusted Origin would bypass CSRF checks.
      for (const name of ["Origin", "X-Refactra-CSRF"]) {
        const value = request.headers.get(name);
        if (value !== null) headers.set(name, value);
      }
    }
    // Submission also resolves the branch and downloads the source before returning a receipt.
    const timeoutMs = isAnalysisSubmit ? 90000 : sessionOnly ? 60000
      : relative === "auth/github/callback" ? 25000 : 5000;
    const upstream = await fetch(`${process.env.REFACTRA_API_URL ?? "http://127.0.0.1:8000"}/api/v1/${relative}${request.nextUrl.search}`, {
      method: request.method, headers,
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store", redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
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
    return NextResponse.json({ error: { code: "api_unavailable", message: "The local API is unavailable. Start the Compose stack, then retry." } }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export const GET = proxy;
export const POST = proxy;
