import { NextResponse, type NextRequest } from "next/server";
import { signInPath } from "@/lib/navigation";

const SESSION_COOKIES = ["refactra_session", "__Host-refactra_session"];

// Visitors without a session cookie go straight to the sign-in page. A cookie that has expired
// on the API passes here; the page then finds the session signed out and sends them on itself.
export function proxy(request: NextRequest) {
  if (SESSION_COOKIES.some((name) => request.cookies.has(name))) return NextResponse.next();
  const { pathname, search } = request.nextUrl;
  return NextResponse.redirect(new URL(signInPath(pathname + search), request.url));
}

export const config = { matcher: ["/", "/history", "/runs/:path*", "/waitlist"] };
