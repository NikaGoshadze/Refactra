// Mirrors safe_next in the API: only a plain path inside Refactra is a valid return address,
// so a crafted sign-in link cannot send someone to another site afterwards.
const NEXT = /^\/(?![/\\])[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]{0,511}(?![\s\S])/;

export function safeNext(value: string | null | undefined): string {
  return value && NEXT.test(value) ? value : "/";
}

/** The sign-in page, remembering where to come back to. */
export function signInPath(next: string): string {
  const target = safeNext(next);
  return target === "/" ? "/sign-in" : `/sign-in?next=${encodeURIComponent(target)}`;
}

/** The GitHub sign-in link; the API keeps the return path in its signed attempt cookie. */
export function loginHref(next: string): string {
  const target = safeNext(next);
  return target === "/" ? "/api/v1/auth/github/login" : `/api/v1/auth/github/login?next=${encodeURIComponent(target)}`;
}
