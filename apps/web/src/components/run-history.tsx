"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { AuthControls } from "@/components/auth-controls";
import { accessNotice, request, type SessionStatus } from "@/lib/api";

export type Stage = "analyzing" | "plan_ready" | "generating" | "ready_to_publish" | "publishing" | "published" | "failed";
export type RunSummary = {
  run_id: string; repository_id: number; repository_full_name: string | null; base_branch: string; base_sha: string;
  task_summary: string; stage: Stage; stop_reason: string | null; plan_revisions: number;
  pull_request: { number: number; url: string } | null; cost_usd: string; created_at: string; last_activity_at: string;
};
type Page = { runs: RunSummary[]; next_cursor: string | null };

export const STAGE_LABELS: Record<Stage, string> = {
  analyzing: "Analyzing",
  plan_ready: "Plan ready for review",
  generating: "Generating and validating",
  ready_to_publish: "Ready to publish",
  publishing: "Publishing",
  published: "Published",
  failed: "Failed",
};
export const formatDate = (value: string) => new Date(value).toLocaleString();

function HistoryList() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: "20" });
    if (search) params.set("q", search);
    if (more && cursor) params.set("cursor", cursor);
    request<Page>(`runs?${params}`, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) })
      .then((page) => {
        if (controller.signal.aborted) return;
        setRuns((current) => (more ? [...current, ...page.runs] : page.runs));
        setCursor(page.next_cursor);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Could not load your runs.");
        setLoading(false);
      });
    return () => controller.abort();
    // `cursor` is read only when loading more; a new search starts from the first page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, more]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    setMore(0);
    setSearch(query.trim());
  }

  function loadMore() {
    setLoading(true);
    setError(null);
    setMore((value) => value + 1);
  }

  return (
    <section className="repository-card" aria-labelledby="history-title">
      <h2 id="history-title">Your runs</h2>
      <form className="history-search" role="search" onSubmit={submit}>
        <label htmlFor="history-query">Search tasks and repositories</label>
        <input id="history-query" type="search" value={query} maxLength={100}
          onChange={(event) => setQuery(event.target.value)} />
        <button className="repository-check" type="submit" disabled={loading}>Search</button>
      </form>
      {error && <p className="account-error" role="alert">{error}</p>}
      {loading && runs.length === 0 && <p role="status">Loading your runs…</p>}
      {!loading && !error && runs.length === 0 && <p role="status">
        {search ? "No runs match this search." : <>No runs yet. <Link href="/">Start one in the workspace.</Link></>}
      </p>}
      {runs.length > 0 && <ol className="history-list">
        {runs.map((run) => <li key={run.run_id}>
          <article aria-label={`Run ${run.run_id}`}>
            <p className="history-repository">{run.repository_full_name ?? `Repository ${run.repository_id}`}</p>
            <p className="history-task"><Link href={`/runs/${run.run_id}`}>{run.task_summary}</Link></p>
            <p><strong className={`history-stage stage-${run.stage}`}>{STAGE_LABELS[run.stage]}</strong>
              {run.stop_reason && <> — {run.stop_reason}</>}
              {run.pull_request && <> — <a href={run.pull_request.url}>Draft PR #{run.pull_request.number}</a></>}</p>
            <dl className="repository-details">
              <div><dt>Branch</dt><dd>{run.base_branch} @ <code>{run.base_sha.slice(0, 7)}</code></dd></div>
              <div><dt>Plan revisions</dt><dd>{run.plan_revisions}</dd></div>
              <div><dt>Model cost</dt><dd>${run.cost_usd}</dd></div>
              <div><dt>Started</dt><dd>{formatDate(run.created_at)}</dd></div>
              <div><dt>Last activity</dt><dd>{formatDate(run.last_activity_at)}</dd></div>
              <div><dt>Run ID</dt><dd><code>{run.run_id}</code></dd></div>
            </dl>
          </article>
        </li>)}
      </ol>}
      {cursor && <button className="repository-check" type="button" disabled={loading} onClick={loadMore}>
        {loading ? "Loading…" : "Load more"}
      </button>}
    </section>
  );
}

export function RunHistory() {
  const [session, setSession] = useState<SessionStatus | null>(null);
  const notice = accessNotice(session);
  return <>
    <AuthControls session={session} onSessionChange={setSession} gate />
    {notice ? <p className="repository-state" role="status">{notice}</p>
      : session?.authenticated ? <HistoryList key={session.user.user_id} />
      : session ? <p className="repository-state" role="status">Sign in with GitHub to see your runs.</p> : null}
  </>;
}
