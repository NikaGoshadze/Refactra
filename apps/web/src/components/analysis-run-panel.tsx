"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { request } from "@/lib/api";

type Receipt = { run_id: string; base_branch: string; base_sha: string };
type RunStatus = "CREATED" | "ANALYZING" | "NEEDS_HUMAN_REVIEW" | "FAILED";
type AnalysisRun = Receipt & {
  status: RunStatus;
  analysis_text: string | null;
  stop_reason: { message: string } | null;
};

const statusLabel: Record<RunStatus, string> = {
  CREATED: "Queued",
  ANALYZING: "Analyzing",
  NEEDS_HUMAN_REVIEW: "Ready for review",
  FAILED: "Analysis failed",
};
const isActive = (status: RunStatus) => status === "CREATED" || status === "ANALYZING";

function errorMessage(error: unknown, timeoutMessage: string, fallback: string) {
  if (error instanceof DOMException && error.name === "TimeoutError") return timeoutMessage;
  return error instanceof Error ? error.message : fallback;
}

export function AnalysisRunPanel({ repositoryId, defaultBranch }: {
  repositoryId: number; defaultBranch: string;
}) {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [branch, setBranch] = useState(defaultBranch);
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [run, setRun] = useState<AnalysisRun | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const pending = useRef<AbortController | null>(null);
  const submission = useRef<{ body: string; key: string } | null>(null);
  const runId = receipt?.run_id;
  const tracking = Boolean(receipt && (!run || isActive(run.status)));

  useEffect(() => {
    const controller = new AbortController();
    request<{ repository_analysis_enabled: boolean }>("capabilities", {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(7000)]),
    }).then((capabilities) => {
      if (!controller.signal.aborted) setAvailable(capabilities.repository_analysis_enabled === true);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setCapabilityError(
        errorMessage(error, "Analysis availability check timed out.", "Could not check analysis availability."),
      );
    });
    return () => controller.abort();
  }, []);

  useEffect(() => () => pending.current?.abort(), []);
  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const current = await request<AnalysisRun>(`runs/${runId}`, {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
        });
        if (controller.signal.aborted) return;
        setRun(current);
        if (isActive(current.status)) timer = setTimeout(poll, 5000);
      } catch (error) {
        if (!controller.signal.aborted) setStatusError(
          errorMessage(error, "Run status check timed out.", "Could not check run status."),
        );
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, retry]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!available || pending.current || tracking || !branch.trim() || !task.trim()) return;
    const body = JSON.stringify({ repository_id: repositoryId, base_branch: branch.trim(), task_text: task.trim() });
    if (submission.current?.body !== body) submission.current = { body, key: crypto.randomUUID() };
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setSubmissionError(null);
    try {
      const accepted = await request<Receipt>("runs", {
        method: "POST",
        body,
        headers: { "X-Refactra-CSRF": "1", "Idempotency-Key": submission.current.key },
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(95000)]),
      });
      if (controller.signal.aborted) return;
      submission.current = null;
      setRun(null);
      setStatusError(null);
      setReceipt(accepted);
    } catch (error) {
      if (!controller.signal.aborted) setSubmissionError(
        errorMessage(error, "Submission timed out. Retry the same task to check whether it was accepted.", "Could not start analysis."),
      );
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <section className="repository-card analysis-card" aria-labelledby="analysis-title">
      <h2 id="analysis-title">Repository analysis</h2>
      {capabilityError ? <p role="alert">{capabilityError}</p>
        : available === null ? <p role="status">Checking analysis availability…</p>
        : !available ? <p>Repository analysis is disabled.</p>
        : <form onSubmit={submit}>
          <label htmlFor="analysis-branch">Base branch</label>
          <input id="analysis-branch" value={branch} maxLength={255} required disabled={busy || tracking}
            onChange={(event) => setBranch(event.target.value)} />
          <label htmlFor="analysis-task">What would you like to refactor?</label>
          <textarea id="analysis-task" value={task} maxLength={4000} rows={4} required disabled={busy || tracking}
            onChange={(event) => setTask(event.target.value)} />
          <button className="primary-button" type="submit" disabled={busy || tracking || !branch.trim() || !task.trim()}>
            {busy ? "Preparing analysis…" : "Start analysis"}
          </button>
        </form>}
      {submissionError && <p className="error-box" role="alert">{submissionError}</p>}
      {receipt && <>
        <dl className="repository-details">
          <div><dt>Branch</dt><dd>{receipt.base_branch}</dd></div>
          <div><dt>Pinned commit</dt><dd><code>{receipt.base_sha}</code></dd></div>
        </dl>
        <p role="status">{statusLabel[run?.status ?? "CREATED"]}</p>
        {statusError && <div className="error-box">
          <p role="alert">{statusError}</p>
          <button type="button" onClick={() => { setStatusError(null); setRetry((value) => value + 1); }}>
            Retry status
          </button>
        </div>}
        {run?.stop_reason && <p className="error-box" role="alert">{run.stop_reason.message}</p>}
        {run?.analysis_text && <pre className="analysis-result">{run.analysis_text}</pre>}
      </>}
    </section>
  );
}
