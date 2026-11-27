"use client";

import { useEffect, useState } from "react";
import { UsageSummary } from "@/components/usage-summary";
import { request, type SessionStatus } from "@/lib/api";

export function AuthControls({ session, onSessionChange }: {
  session: SessionStatus | null; onSessionChange: (session: SessionStatus) => void;
}) {
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
        <UsageSummary key={session.user.user_id} />
        <button type="button" disabled={busy} onClick={signOut}>{busy ? "Signing out…" : "Sign out"}</button>
      </> : session ? <>
        <span role="status">Not signed in</span>
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- OAuth needs full navigation without prefetching. */}
        <a className="account-signin" href="/api/v1/auth/github/login">Sign in with GitHub</a>
      </> : <button type="button" onClick={retryAccount}>Retry account</button>}
      {error && <p className="account-error" role="alert">{error}</p>}
    </section>
  );
}
