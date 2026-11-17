"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { AuthControls } from "@/components/auth-controls";
import { request, type SessionStatus } from "@/lib/api";

// The first UI slice targets the controlled fixture selected for local testing.
const REPOSITORY_ID = 1001132861;
type Repository = { full_name: string; default_branch: string; access: "read-only" };
type Connection = { status: "idle" | "loading" } | { status: "verified"; repository: Repository }
  | { status: "error"; message: string };

function RepositoryPanel({ authenticated }: { authenticated: boolean }) {
  const [connection, setConnection] = useState<Connection>({ status: "idle" });
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function checkAccess() {
    if (!authenticated || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setConnection({ status: "loading" });
    try {
      const repository = await request<Repository>(`repositories/${REPOSITORY_ID}`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) setConnection({ status: "verified", repository });
    } catch (err) {
      if (!controller.signal.aborted) setConnection({
        status: "error", message: err instanceof DOMException && err.name === "TimeoutError"
          ? "Repository check timed out. Try again."
          : err instanceof Error ? err.message : "Could not check repository access.",
      });
    } finally { if (pending.current === controller) pending.current = null; }
  }

  return (
    <section className="repository-card" aria-labelledby="repository-title">
      <div className="repository-heading">
        <div><p className="eyebrow">GITHUB CONNECTION</p><h2 id="repository-title">Repository connection</h2>
          <p className="repository-target">NikaGoshadze/AI-Hub</p></div>
        <button className="repository-check" type="button" onClick={checkAccess}
          disabled={!authenticated || connection.status === "loading"}>
          {connection.status === "loading" ? "Checking access…" : connection.status === "error" ? "Retry connection" : "Check repository access"}
        </button>
      </div>
      {connection.status === "error" ? <p className="account-error repository-state" role="alert">{connection.message}</p>
        : <p className="repository-state" role="status">{!authenticated ? "Confirm your GitHub account above to check access."
          : connection.status === "verified" ? "Read-only connection verified."
          : connection.status === "loading" ? "Checking your repository permissions…" : "Access has not been checked."}</p>}
      {connection.status === "verified" && <dl className="repository-details">
        <div><dt>Repository</dt><dd>{connection.repository.full_name}</dd></div>
        <div><dt>Default branch</dt><dd>{connection.repository.default_branch}</dd></div>
        <div><dt>Access</dt><dd>Read-only</dd></div>
      </dl>}
      <p className="repository-note">This check reads repository details. Tasks below still use the mock queue.</p>
    </section>
  );
}

export function RepositoryConnection({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionStatus | null>(null);
  return <>
    <AuthControls session={session} onSessionChange={setSession} />
    {children}
    <RepositoryPanel key={session?.authenticated ? session.user.user_id : "anonymous"}
      authenticated={session?.authenticated === true} />
  </>;
}
