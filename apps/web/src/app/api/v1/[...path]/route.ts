import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const relative = path.join("/");
  const readAllowed = relative === "capabilities" || /^mock\/runs\/[0-9a-f-]{36}(\/events)?$/i.test(relative);
  const writeAllowed = relative === "mock/runs";
  if (!(request.method === "GET" ? readAllowed : writeAllowed)) {
    return NextResponse.json({ error: { code: "unavailable_route", message: "This route is not available in the foundation build." } }, { status: 404 });
  }
  try {
    const headers = new Headers({ "Content-Type": "application/json" });
    const key = request.headers.get("Idempotency-Key");
    if (key) headers.set("Idempotency-Key", key);
    const upstream = await fetch(`${process.env.REFACTRA_API_URL ?? "http://127.0.0.1:8000"}/api/v1/${relative}${request.nextUrl.search}`, {
      method: request.method, headers,
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000),
    });
    return new NextResponse(await upstream.text(), { status: upstream.status, headers: {
      "Content-Type": "application/json", "Cache-Control": "no-store",
      ...(upstream.headers.get("Location") ? { Location: upstream.headers.get("Location")! } : {}),
    } });
  } catch {
    return NextResponse.json({ error: { code: "api_unavailable", message: "The local API is unavailable. Start the Compose stack, then retry." } }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
