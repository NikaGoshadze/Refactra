"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AuthControls } from "@/components/auth-controls";
import { STAGE_LABELS, formatDate, type RunSummary, type Stage } from "@/components/run-history";
import { SavedPlanPanel } from "@/components/saved-plan-panel";
import { accessNotice, request, type SessionStatus } from "@/lib/api";
import { nextUnchanged, pollDelay } from "@/lib/polling";

type Detail = {
  summary: RunSummary;
  task_text: string;
  timeline: { at: string; label: string; link: string | null }[];
};

// Stages where the worker is still busy; the page keeps checking for progress.
const MOVING: Stage[] = ["analyzing", "generating", "publishing"];

/** A workspace link that preselects this run's repository and branch and fills in its task. */
export function similarTaskHref(summary: RunSummary, task: string) {
  const params = new URLSearchParams({ repository: String(summary.repository_id), branch: summary.base_branch, task });
  return `/?${params}`;
}

function RunDetailBody({ runId }: { runId: string }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let previous: string | null = null;
    let unchanged = 0;
    async function load() {
      try {
        // Rechecks repository access, so allow for the proxy's 60-second limit.
        const value = await request<Detail>(`runs/${runId}/timeline`, {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
        });
        if (controller.signal.aborted) return;
        setDetail(value);
        const signature = JSON.stringify(value);
        unchanged = nextUnchanged(previous, signature, unchanged);
        previous = signature;
        if (MOVING.includes(value.summary.stage)) timer = setTimeout(load, pollDelay(5000, unchanged));
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load this run.");
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, retry]);

  if (error) return <div className="error-box">
    <p role="alert">{error}</p>
    <button type="button" onClick={() => { setError(null); setRetry((value) => value + 1); }}>Retry</button>
  </div>;
  if (!detail) return <p role="status">Loading this run…</p>;
  const { summary } = detail;
  // The plan panel continues through approval, the candidate and publication.
  const hasPlan = summary.plan_revisions > 0;

  return <>
    <section className="repository-card" aria-labelledby="run-title">
      <p className="history-repository">{summary.repository_full_name ?? `Repository ${summary.repository_id}`}</p>
      <h2 id="run-title">Run</h2>
      <p className="run-task">{detail.task_text}</p>
      <p><strong className={`history-stage stage-${summary.stage}`}>{STAGE_LABELS[summary.stage]}</strong>
        {summary.stop_reason && <> — {summary.stop_reason}</>}
        {summary.pull_request && <> — <a href={summary.pull_request.url}>Draft PR #{summary.pull_request.number}</a></>}</p>
      <dl className="repository-details">
        <div><dt>Branch</dt><dd>{summary.base_branch} @ <code>{summary.base_sha.slice(0, 7)}</code></dd></div>
        <div><dt>Plan revisions</dt><dd>{summary.plan_revisions}</dd></div>
        <div><dt>Model cost</dt><dd>${summary.cost_usd}</dd></div>
        <div><dt>Started</dt><dd>{formatDate(summary.created_at)}</dd></div>
        <div><dt>Last activity</dt><dd>{formatDate(summary.last_activity_at)}</dd></div>
        <div><dt>Run ID</dt><dd><code>{summary.run_id}</code></dd></div>
      </dl>
      <p className="run-actions">
        <Link href={similarTaskHref(summary, detail.task_text)}>Start a similar task</Link>
        <Link href="/history">Back to history</Link>
      </p>
    </section>
    <section className="repository-card" aria-labelledby="timeline-title">
      <h2 id="timeline-title">Timeline</h2>
      <ol className="run-timeline">
        {detail.timeline.map((entry, index) => <li key={index}>
          <time dateTime={entry.at}>{formatDate(entry.at)}</time>{" "}
          {entry.link ? <a href={entry.link}>{entry.label}</a> : entry.label}
        </li>)}
      </ol>
    </section>
    {hasPlan && <section className="repository-card analysis-card" aria-label="Continue this run">
      <SavedPlanPanel runId={runId} />
    </section>}
  </>;
}

export function RunDetail({ runId }: { runId: string }) {
  const [session, setSession] = useState<SessionStatus | null>(null);
  const notice = accessNotice(session);
  return <>
    <AuthControls session={session} onSessionChange={setSession} gate />
    {notice ? <p className="repository-state" role="status">{notice}</p>
      : session?.authenticated ? <RunDetailBody key={session.user.user_id} runId={runId} />
      : session ? <p className="repository-state" role="status">Sign in with GitHub to see this run.</p> : null}
  </>;
}
