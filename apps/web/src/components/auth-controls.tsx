"use client";

import { MarkGithubIcon } from "@primer/octicons-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { UsageSummary } from "@/components/usage-summary";
import { accessNotice, request, type SessionStatus } from "@/lib/api";
import { loginHref, signInPath } from "@/lib/navigation";

/** The account bar. On app pages (`gate`), signed-out visitors go to the sign-in page and
 * users without access go to the waitlist page. */
export function AuthControls({ session, onSessionChange, gate = false }: {
  session: SessionStatus | null; onSessionChange: (session: SessionStatus) => void; gate?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    request<SessionStatus>("auth/session", { signal: controller.signal })
      .then((current) => { if (!controller.signal.aborted) onSessionChange(current); })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Could not load your account.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [retry, onSessionChange]);

  useEffect(() => {
    if (!gate || !session) return;
    if (!session.authenticated) router.replace(signInPath(pathname + window.location.search));
    else if (accessNotice(session)) router.replace("/waitlist");
  }, [gate, session, router, pathname]);

  function retryAccount() {
    setError(null);
    setLoading(true);
    setRetry((value) => value + 1);
  }

  async function signOut() {
    setBusy(true);
    setError(null);
    try {
      await request<void>("auth/logout", { method: "POST", headers: { "X-Refactra-CSRF": "1" } });
      onSessionChange({ authenticated: false, user: null });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign out failed. Please try again.");
    } finally { setBusy(false); }
  }

  return (
    <section className="account-bar" aria-label="GitHub account">
      {loading ? <span role="status">Checking account…</span> : session?.authenticated ? <>
        <span role="status">Signed in as <strong>{session.user.login}</strong></span>
        {!accessNotice(session) && <UsageSummary key={session.user.user_id} />}
        <button type="button" disabled={busy} onClick={signOut}>{busy ? "Signing out…" : "Sign out"}</button>
      </> : session ? <>
        <span role="status">Not signed in</span>
        {/* A plain link: OAuth needs full navigation without prefetching. */}
        <a className="account-signin" href={loginHref(pathname)}>
          <MarkGithubIcon size={16} aria-hidden="true" />Sign in with GitHub
        </a>
      </> : <button type="button" onClick={retryAccount}>Retry account</button>}
      {error && <p className="account-error" role="alert">{error}</p>}
    </section>
  );
}
