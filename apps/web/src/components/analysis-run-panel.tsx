"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { SavedPlanPanel } from "@/components/saved-plan-panel";
import { request } from "@/lib/api";

type Receipt = { run_id: string; base_branch: string; base_sha: string };
const RUN_ID_PATTERN = "[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}";
type RunStatus = "CREATED" | "ANALYZING" | "NEEDS_HUMAN_REVIEW" | "FAILED";
type AnalysisRun = Receipt & {
  repository_id: number;
  status: RunStatus;
  stop_reason: { message: string } | null;
};

const statusLabel: Record<RunStatus, string> = {
  CREATED: "Preparing plan…",
  ANALYZING: "Preparing plan…",
  NEEDS_HUMAN_REVIEW: "Preparing plan…",
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
  const [workerAvailable, setWorkerAvailable] = useState(true);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [branch, setBranch] = useState(defaultBranch);
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [target, setTarget] = useState<(Pick<Receipt, "run_id"> & Partial<Receipt>) | null>(null);
  const [run, setRun] = useState<AnalysisRun | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const pending = useRef<AbortController | null>(null);
  const submission = useRef<{ body: string; key: string } | null>(null);
  const runId = target?.run_id;
  const revision = run ?? target;
  const tracking = Boolean(runId && !statusError && (!run || isActive(run.status)));

  useEffect(() => {
    const controller = new AbortController();
    request<{ repository_analysis_enabled: boolean; analysis_worker_available?: boolean }>("capabilities", {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(7000)]),
    }).then((capabilities) => {
      if (controller.signal.aborted) return;
      setAvailable(capabilities.repository_analysis_enabled === true);
      setWorkerAvailable(capabilities.analysis_worker_available !== false);
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
        if (current.repository_id !== repositoryId) {
          setStatusError("This run belongs to a different repository.");
          return;
        }
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
  }, [repositoryId, runId, retry]);

  function track(next: Pick<Receipt, "run_id"> & Partial<Receipt>) {
    setRun(null);
    setStatusError(null);
    setTarget(next);
    setRetry((value) => value + 1);
  }

  function openSavedRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const id = new FormData(event.currentTarget).get("run-id");
    if (typeof id === "string" && new RegExp(`^${RUN_ID_PATTERN}$`).test(id)) {
      track({ run_id: id.toLowerCase() });
    }
  }

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
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (controller.signal.aborted) return;
      submission.current = null;
      track(accepted);
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
          {!workerAvailable && <p role="status">
            No analysis worker is running. Submitted runs wait in the queue until one starts.
          </p>}
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
      <details className="saved-run">
        <summary>Open a saved run</summary>
        <form onSubmit={openSavedRun}>
          <label htmlFor="saved-run-id">Run ID</label>
          <input id="saved-run-id" name="run-id" pattern={RUN_ID_PATTERN} required disabled={busy}
            title="Enter the run UUID" />
          <button className="repository-check" type="submit" disabled={busy}>Open run</button>
        </form>
      </details>
      {runId && <>
        <dl className="repository-details">
          <div><dt>Run ID</dt><dd><code>{runId}</code></dd></div>
          {revision?.base_branch && <div><dt>Branch</dt><dd>{revision.base_branch}</dd></div>}
          {revision?.base_sha && <div><dt>Pinned commit</dt><dd><code>{revision.base_sha}</code></dd></div>}
        </dl>
        {!statusError && run?.status !== "NEEDS_HUMAN_REVIEW" &&
          <p role="status">{run ? statusLabel[run.status] : "Checking run status…"}</p>}
        {statusError && <div className="error-box">
          <p role="alert">{statusError}</p>
          <button type="button" onClick={() => { setStatusError(null); setRetry((value) => value + 1); }}>
            Retry status
          </button>
        </div>}
        {run?.stop_reason && <p className="error-box" role="alert">{run.stop_reason.message}</p>}
        {run?.status === "NEEDS_HUMAN_REVIEW" && <SavedPlanPanel key={runId} runId={runId} />}
      </>}
    </section>
  );
}
