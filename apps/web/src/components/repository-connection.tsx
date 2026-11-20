"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { AnalysisRunPanel } from "@/components/analysis-run-panel";
import { AuthControls } from "@/components/auth-controls";
import { request, type SessionStatus } from "@/lib/api";

// The first UI slice targets the controlled fixture selected for local testing.
const REPOSITORY_ID = 1001132861;
type Repository = { repository_id: number; full_name: string; default_branch: string; access: "read-only" };
type Connection = { status: "idle" } | { status: "verified"; repository: Repository }
  | { status: "error"; message: string };

function RepositoryPanel({ authenticated }: { authenticated: boolean }) {
  const [connection, setConnection] = useState<Connection>({ status: "idle" });
  const [checking, setChecking] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function checkAccess() {
    if (!authenticated || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setChecking(true);
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
    } finally {
      if (pending.current === controller) pending.current = null;
      if (!controller.signal.aborted) setChecking(false);
    }
  }

  return (
    <>
    <section className="repository-card" aria-labelledby="repository-title">
      <div className="repository-heading">
        <div><p className="eyebrow">GITHUB CONNECTION</p><h2 id="repository-title">Repository connection</h2>
          <p className="repository-target">NikaGoshadze/AI-Hub</p></div>
        <button className="repository-check" type="button" onClick={checkAccess}
          disabled={!authenticated || checking}>
          {checking ? "Checking access…" : connection.status === "error" ? "Retry connection" : "Check repository access"}
        </button>
      </div>
      {connection.status === "error" && !checking ? <p className="account-error repository-state" role="alert">{connection.message}</p>
        : <p className="repository-state" role="status">{!authenticated ? "Confirm your GitHub account above to check access."
          : checking ? "Checking your repository permissions…"
          : connection.status === "verified" ? "Read-only connection verified."
          : "Access has not been checked."}</p>}
      {connection.status === "verified" && <dl className="repository-details">
        <div><dt>Repository</dt><dd>{connection.repository.full_name}</dd></div>
        <div><dt>Default branch</dt><dd>{connection.repository.default_branch}</dd></div>
        <div><dt>Access</dt><dd>Read-only</dd></div>
      </dl>}
    </section>
    {connection.status === "verified" && (
      <AnalysisRunPanel repositoryId={connection.repository.repository_id}
        defaultBranch={connection.repository.default_branch} />
    )}
    </>
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
