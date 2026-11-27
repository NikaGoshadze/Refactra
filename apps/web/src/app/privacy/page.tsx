"use client";

import { useState, type FormEvent } from "react";
import { AuthControls } from "@/components/auth-controls";
import { TopBar } from "@/components/top-bar";
import { request, type SessionStatus } from "@/lib/api";

export default function PrivacyPage() {
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleted, setDeleted] = useState(false);
  const [error, setError] = useState("");
  const confirmed = session?.authenticated && confirmation.trim().toLowerCase() === session.user.login.toLowerCase();

  async function deleteAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!confirmed || busy) return;
    setBusy(true);
    setError("");
    try {
      await request("account/delete", {
        method: "POST", headers: { "X-Refactra-CSRF": "1" },
        body: JSON.stringify({ confirm_login: confirmation }),
        signal: AbortSignal.timeout(60000),
      });
      setSession({ authenticated: false, user: null });
      setConfirmation("");
      setDeleted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete your account. Please try again.");
    } finally { setBusy(false); }
  }

  return (
    <div className="workspace">
      <TopBar />
      <AuthControls session={session} onSessionChange={setSession} />
      <main>
        <header className="intro"><h1>Privacy and account data</h1></header>
        <section className="repository-card">
          <h2>What Refactra stores and shares</h2>
          <p className="card-description">Refactra stores your GitHub identity, access status and sessions. When configured, it encrypts your GitHub access and refresh tokens. For each run it saves the repository and branch, task, selected source excerpts, plan, approvals, diff and validation results.</p>
          <p>Refactra uses GitHub to check access and publish approved branches and draft pull requests. It sends task instructions and selected repository content to OpenAI to generate plans and code changes.</p>
        </section>
        <section className="repository-card analysis-card" aria-labelledby="deletion">
          <h2 id="deletion">Delete your Refactra account</h2>
          <p className="card-description">Deletion permanently clears your saved run content and invite or waitlist entry and signs you out on every device. The worker removes prepared commits and staged assets from its artifact directory on its next cleanup. Refactra keeps run IDs and model usage records for its shared budget, with the account and repository fields cleared.</p>
          <p>GitHub keeps published branches, pull requests and your App installation. You can manage them on GitHub. Account deletion covers Refactra’s active storage. External provider records, operational logs and backups have separate retention.</p>
          {deleted ? <p role="status">Your Refactra account has been deleted.</p> : session?.authenticated ? (
            <form onSubmit={deleteAccount}>
              <label htmlFor="confirm-login">Type your GitHub login ({session.user.login}) to confirm deletion</label>
              <input id="confirm-login" autoComplete="off" spellCheck={false} value={confirmation}
                disabled={busy} onChange={(event) => setConfirmation(event.target.value)} />
              <button type="submit" className="primary-button" disabled={!confirmed || busy}>
                {busy ? "Deleting account…" : "Permanently delete my account"}
              </button>
              {error && <p role="alert">{error}</p>}
            </form>
          ) : session && <p className="repository-state">Sign in with GitHub above to delete your account.</p>}
        </section>
      </main>
    </div>
  );
}
