import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const AUTH_READ_ROUTES = new Set(["auth/github/login", "auth/github/callback", "auth/session"]);
const RUN_ID_PATTERN = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const SESSION_COOKIES = new Set(["refactra_session", "__Host-refactra_session"]);
const AUTH_COOKIES = new Set([
  "refactra_oauth", "__Host-refactra_oauth", ...SESSION_COOKIES,
]);
// The OAuth callback URL carries the authorization code and state; never send it as a referrer.
const NO_REFERRER = { "Referrer-Policy": "no-referrer" };
// Request bodies are read with a byte limit and a deadline before anything is forwarded.
const BODY_LIMIT = 16 * 1024;
const PLAN_BODY_LIMIT = 128 * 1024; // A full plan edit; saved plans are at most 64 KiB.
const BODY_TIMEOUT_MS = Number(process.env.REFACTRA_PROXY_BODY_TIMEOUT_MS) || 10_000;
const NO_STORE = { "Cache-Control": "no-store" };

class BodyRejected extends Error {
  constructor(readonly status: 400 | 408 | 413) { super(); }
}

/** Read at most `limit` bytes within the deadline, whether or not Content-Length is sent. */
async function readBody(request: NextRequest, limit: number): Promise<string> {
  if (Number(request.headers.get("content-length")) > limit) throw new BodyRejected(413);
  if (!request.body) return "";
  const reader = request.body.getReader();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new BodyRejected(408)), BODY_TIMEOUT_MS);
  });
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await Promise.race([reader.read(), expired]);
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new BodyRejected(413);
      chunks.push(value);
    }
  } catch (error) {
    void reader.cancel().catch(() => undefined);
    throw error instanceof BodyRejected ? error : new BodyRejected(400);
  } finally {
    clearTimeout(timer);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(body);
}

const BODY_ERRORS = {
  400: { code: "request_body_unreadable", message: "The request body could not be read." },
  408: { code: "request_timeout", message: "The request body was not received in time." },
  413: { code: "request_too_large", message: "The request body is too large." },
};

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const relative = path.join("/");
  const isAuth = AUTH_READ_ROUTES.has(relative) || relative === "auth/logout";
  const isAccountDeletion = relative === "account/delete" && request.method === "POST";
  // The user's repository list and usage are signed-in reads like the repository checks.
  const isRepository = relative === "user/repositories" || relative === "usage"
    || /^repositories\/[1-9][0-9]*$/.test(relative);
  const isAnalysisSubmit = relative === "runs";
  const [resource, id, section, action] = path;
  const isRun = resource === "runs" && RUN_ID_PATTERN.test(id ?? "");
  const isPlan = isRun && section === "plan";
  const isRunRead = isRun && (path.length === 2
    || (path.length === 3 && (isPlan || section === "result" || section === "timeline")));
  const isPlanWrite = isPlan && request.method === "POST"
    && (path.length === 3 || (path.length === 4 && action === "approve"));
  const isPublicationApproval = isRun && section === "result" && action === "approve"
    && path.length === 4 && request.method === "POST";
  const isCheckWaiver = isRun && section === "result" && action === "waivers"
    && path.length === 4 && request.method === "POST";
  const isResultWrite = isPublicationApproval || isCheckWaiver;
  const sessionOnly = isAccountDeletion || isRepository || isAnalysisSubmit || isRunRead || isPlanWrite || isResultWrite;
  // GET runs is the user's run history; POST runs submits a new analysis.
  const readAllowed = isRepository || isRunRead || isAnalysisSubmit || AUTH_READ_ROUTES.has(relative)
    || relative === "capabilities" || /^mock\/runs\/[0-9a-f-]{36}(\/events)?$/i.test(relative);
  const writeAllowed = isAccountDeletion || isAnalysisSubmit || isPlanWrite || isResultWrite
    || relative === "mock/runs" || relative === "auth/logout";
  if (!(request.method === "GET" ? readAllowed : request.method === "POST" && writeAllowed)) {
    return NextResponse.json({ error: { code: "unavailable_route", message: "This route is not available in the foundation build." } }, { status: 404, headers: { "Cache-Control": "no-store" } });
  }
  let body: string | undefined;
  if (request.method === "POST") {
    try {
      body = await readBody(request, isPlan ? PLAN_BODY_LIMIT : BODY_LIMIT);
    } catch (error) {
      const status = error instanceof BodyRejected ? error.status : 400;
      return NextResponse.json({ error: BODY_ERRORS[status] }, { status, headers: NO_STORE });
    }
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
    if (isAuth || isAccountDeletion || isAnalysisSubmit || isPlanWrite || isResultWrite) {
      // Preserve browser-supplied values; inventing a trusted Origin would bypass CSRF checks.
      for (const name of ["Origin", "X-Refactra-CSRF"]) {
        const value = request.headers.get(name);
        if (value !== null) headers.set(name, value);
      }
    }
    // Protected routes recheck GitHub access; submission also pins the branch head.
    const timeoutMs = sessionOnly ? 60000 : relative === "auth/github/callback" ? 25000 : 5000;
    const upstream = await fetch(`${process.env.REFACTRA_API_URL ?? "http://127.0.0.1:8000"}/api/v1/${relative}${request.nextUrl.search}`, {
      method: request.method, headers,
      body,
      cache: "no-store", redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store", ...(isAuth ? NO_REFERRER : {}) });
    for (const name of ["Content-Type", "Location"]) {
      const value = upstream.headers.get(name);
      if (value !== null) responseHeaders.set(name, value);
    }
    if (isAuth || isAccountDeletion) {
      for (const cookie of upstream.headers.getSetCookie()) responseHeaders.append("Set-Cookie", cookie);
    }
    return new NextResponse(upstream.status === 204 ? null : await upstream.text(), {
      status: upstream.status, headers: responseHeaders,
    });
  } catch {
    return NextResponse.json({ error: { code: "api_unavailable", message: "The local API is unavailable. Start the Compose stack, then retry." } }, { status: 503, headers: { "Cache-Control": "no-store", ...(isAuth ? NO_REFERRER : {}) } });
  }
}

export const GET = proxy;
export const POST = proxy;
