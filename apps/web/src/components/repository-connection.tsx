"use client";

import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from "react";
import { AnalysisRunPanel } from "@/components/analysis-run-panel";
import { AuthControls } from "@/components/auth-controls";
import { accessNotice, request, type SessionStatus } from "@/lib/api";

type UserRepository = {
  id: number; full_name: string; private: boolean; default_branch: string; installation_id: number; writable: boolean;
};
type Installation = { id: number; account: string; settings_url: string };
type Listing = {
  install_url: string | null; installations: Installation[]; repositories: UserRepository[]; truncated: boolean;
};
type Repository = {
  repository_id: number; full_name: string; default_branch: string; branches: string[]; branches_truncated: boolean;
};
type Preset = { repositoryId: number; branch: string | null; task: string };
type Load<T> = { status: "idle" | "loading" } | { status: "ready"; value: T } | { status: "error"; message: string };

const TIMEOUT_MS = 65000; // The proxy allows 60 seconds for repository reads.

function failure(error: unknown, timeout: string, fallback: string) {
  if (error instanceof DOMException && error.name === "TimeoutError") return timeout;
  return error instanceof Error ? error.message : fallback;
}

function RepositoryPanel({ authenticated, notice }: { authenticated: boolean; notice: string | null }) {
  const [listing, setListing] = useState<Load<Listing>>({ status: authenticated ? "loading" : "idle" });
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState("");
  const [repository, setRepository] = useState<Load<Repository>>({ status: "idle" });
  const [reload, setReload] = useState(0);
  const [preset, setPreset] = useState<Preset | null>(null);
  const verifying = useRef<AbortController | null>(null);
  const presetRead = useRef(false);

  // "Start a similar task" links carry ?repository=&branch=&task=; apply them once, after the
  // list shows the repository is still available.
  const applyPreset = useEffectEvent((value: Listing) => {
    if (presetRead.current) return;
    presetRead.current = true;
    const params = new URLSearchParams(window.location.search);
    const id = params.get("repository");
    const match = value.repositories.find((item) => String(item.id) === id && item.writable);
    if (!match) return;
    setPreset({ repositoryId: match.id, branch: params.get("branch"), task: params.get("task") ?? "" });
    void choose(String(match.id));
  });

  useEffect(() => {
    if (!authenticated) return;
    const controller = new AbortController();
    request<Listing>("user/repositories", { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(TIMEOUT_MS)]) })
      .then((value) => {
        if (controller.signal.aborted) return;
        setListing({ status: "ready", value });
        applyPreset(value);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setListing({
          status: "error", message: failure(error, "Loading your repositories timed out. Try again.", "Could not load your repositories."),
        });
      });
    return () => controller.abort();
  }, [authenticated, reload]);
  useEffect(() => () => verifying.current?.abort(), []);

  async function choose(id: string) {
    verifying.current?.abort();
    setSelected(id);
    setRepository({ status: id ? "loading" : "idle" });
    if (!id) return;
    const controller = new AbortController();
    verifying.current = controller;
    try {
      // Rechecks the App installation and the user's write access, and lists branches.
      const value = await request<Repository>(`repositories/${id}`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(TIMEOUT_MS)]),
      });
      if (!controller.signal.aborted) setRepository({ status: "ready", value });
    } catch (error) {
      if (!controller.signal.aborted) setRepository({
        status: "error", message: failure(error, "The access check timed out. Try again.", "Could not check repository access."),
      });
    }
  }

  const repositories = listing.status === "ready" ? listing.value.repositories : [];
  const shown = repositories.filter((item) => item.full_name.toLowerCase().includes(filter.trim().toLowerCase()));
  const usable = repositories.some((item) => item.writable);
  const status = !authenticated ? notice ?? "Sign in with GitHub to choose a repository."
    : listing.status === "loading" || listing.status === "idle" ? "Loading your repositories…"
    : listing.status === "error" ? null
    : !usable ? "No repositories are available yet. Install Refactra on the repositories you want to refactor."
    : repository.status === "loading" ? "Checking your access…"
    : repository.status === "ready" ? "Write access verified."
    : repository.status === "error" ? null
    : "Choose a repository to refactor.";

  return (
    <>
    <section className="repository-card" aria-labelledby="repository-title">
      <div className="repository-heading">
        <div><p className="eyebrow">GITHUB CONNECTION</p><h2 id="repository-title">Repository connection</h2>
          <p className="repository-target">{repository.status === "ready" ? repository.value.full_name : "No repository chosen"}</p></div>
        {authenticated && <button className="repository-check" type="button" disabled={listing.status === "loading"}
          onClick={() => {
            void choose("");
            setListing({ status: "loading" });
            setReload((value) => value + 1);
          }}>Refresh list</button>}
      </div>
      {status && <p className="repository-state" role="status">{status}</p>}
      {listing.status === "error" && <p className="account-error repository-state" role="alert">{listing.message}</p>}
      {repository.status === "error" && <p className="account-error repository-state" role="alert">{repository.message}</p>}
      {listing.status === "ready" && <div className="repository-picker">
        {repositories.length > 0 && <>
          <label htmlFor="repository-filter">Filter repositories</label>
          <input id="repository-filter" type="search" value={filter} onChange={(event) => setFilter(event.target.value)} />
          <label htmlFor="repository-select">Repository</label>
          <select id="repository-select" value={selected} disabled={repository.status === "loading"}
            onChange={(event) => void choose(event.target.value)}>
            <option value="">Choose a repository…</option>
            {shown.map((item) => <option key={item.id} value={item.id} disabled={!item.writable}>
              {item.full_name}{item.writable ? "" : " (read-only)"}
            </option>)}
          </select>
        </>}
        {listing.value.truncated && <p>Only some of your repositories are listed.</p>}
        <ul className="repository-links">
          {listing.value.install_url && <li><a href={listing.value.install_url}>Install Refactra on more repositories</a></li>}
          {listing.value.installations.map((item) => <li key={item.id}>
            <a href={item.settings_url}>Choose which {item.account} repositories Refactra can access</a>
          </li>)}
        </ul>
      </div>}
      {repository.status === "ready" && <dl className="repository-details">
        <div><dt>Repository</dt><dd>{repository.value.full_name}</dd></div>
        <div><dt>Default branch</dt><dd>{repository.value.default_branch}</dd></div>
      </dl>}
    </section>
    {repository.status === "ready" && (
      <AnalysisRunPanel key={repository.value.repository_id} repositoryId={repository.value.repository_id}
        defaultBranch={repository.value.default_branch} branches={repository.value.branches}
        branchesTruncated={repository.value.branches_truncated}
        {...(preset?.repositoryId === repository.value.repository_id
          ? { preferredBranch: preset.branch, initialTask: preset.task } : {})} />
    )}
    </>
  );
}

export function RepositoryConnection({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionStatus | null>(null);
  // Waitlisted users are signed in but get the waitlist notice instead of their repositories.
  const notice = accessNotice(session);
  return <>
    <AuthControls session={session} onSessionChange={setSession} />
    {children}
    <RepositoryPanel key={session?.authenticated ? `${session.user.user_id}-${notice ? "notice" : "ok"}` : "anonymous"}
      authenticated={session?.authenticated === true && !notice} notice={notice} />
  </>;
}
