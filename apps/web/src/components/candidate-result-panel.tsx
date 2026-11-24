"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "@/lib/api";

type PublicationApproval = { result_revision: number };
type PublicationWaiver = { check_index: number; check_id: string; reason: string };
type PublicationTarget = {
  sha256: string;
  commit_sha: string;
  destination: { repository_id: number; base_branch: string; head_branch: string };
};

type CandidateResult = {
  id: string;
  revision: number;
  sha256: string;
  approved_plan: { revision: number };
  diff: string;
  review?: {
    files: { path: string; action: string; diff_line: number | null }[];
    criteria: { id: string; description: string; passing_checks: string[] }[];
    routes_to_review: { path: string; route: string; diff_line: number }[];
  } | null;
  publication_target: PublicationTarget | null;
  publication_approval: PublicationApproval | null;
  publication_waivers?: PublicationWaiver[];
  publication: { status: "branch_pushed" } | { status: "draft_opened"; number: number; url: string } | null;
  validation: {
    check_id: string;
    paths: string[];
    required: boolean;
    status: string;
    exit_code: number | null;
    output: string;
    reason: string | null;
  }[];
};

const activeMessages = {
  queued: "Waiting for the worker to verify the plan approval.",
  generating: "Generating the candidate patch.",
  validating: "Running validation checks.",
  preparing: "Preparing the commit for review.",
  publishing: "Publishing the draft PR.",
};

type CandidateProgress = {
  status: keyof typeof activeMessages | "idle" | "failed";
  message: string | null;
  result: CandidateResult | null;
};

export function CandidateResultPanel({ runId }: { runId: string }) {
  const [progress, setProgress] = useState<CandidateProgress | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [waiverReasons, setWaiverReasons] = useState<Record<number, string>>({});
  const [waiving, setWaiving] = useState<number | null>(null);
  const pending = useRef<AbortController | null>(null);
  const loadResult = useCallback(async () => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setLoadError(null);
    setApprovalError(null);
    try {
      const saved = await request<CandidateProgress>(`runs/${runId}/result?progress=true`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) setProgress(saved);
    } catch (error) {
      if (controller.signal.aborted) return;
      setLoadError(error instanceof DOMException && error.name === "TimeoutError"
        ? "Result lookup timed out. Refresh to try again."
        : error instanceof Error ? error.message : "Could not load the saved result.");
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    const initial = window.setTimeout(loadResult, 0);
    return () => {
      window.clearTimeout(initial);
      pending.current?.abort();
    };
  }, [loadResult]);

  const status = progress?.status;
  useEffect(() => {
    if (!status || status === "idle" || status === "failed" || loadError || approving) return;
    const started = Date.now();
    const timer = window.setInterval(() => {
      if (pending.current) return;
      if (Date.now() - started >= 180_000) {
        setLoadError("Still waiting for the worker. Refresh to check again.");
      } else {
        void loadResult();
      }
    }, 4000);
    return () => window.clearInterval(timer);
  }, [status, loadError, approving, loadResult]);

  async function approvePublication(result: CandidateResult) {
    if (pending.current || !result.publication_target || result.publication_approval) return;
    const controller = new AbortController();
    pending.current = controller;
    setApproving(true);
    setApprovalError(null);
    try {
      const approval = await request<PublicationApproval>(`runs/${runId}/result/approve`, {
        method: "POST",
        headers: { "X-Refactra-CSRF": "1" },
        body: JSON.stringify({ result_id: result.id, target_sha256: result.publication_target.sha256 }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) {
        setProgress({ status: "publishing", message: null,
          result: { ...result, publication_approval: approval } });
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setApprovalError(error instanceof Error ? error.message : "Could not record publication approval.");
      }
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setApproving(false);
    }
  }

  async function waiveCheck(result: CandidateResult, index: number) {
    const reason = (waiverReasons[index] ?? "").trim();
    if (pending.current || !reason) return;
    const controller = new AbortController();
    pending.current = controller;
    setWaiving(index);
    setApprovalError(null);
    try {
      const waiver = await request<PublicationWaiver>(`runs/${runId}/result/waivers`, {
        method: "POST",
        headers: { "X-Refactra-CSRF": "1" },
        body: JSON.stringify({ result_id: result.id, result_sha256: result.sha256, check_index: index, reason }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) {
        setProgress((current) => current?.result?.id === result.id ? { ...current, result: {
          ...current.result,
          publication_waivers: [...(current.result.publication_waivers ?? []), waiver],
        } } : current);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setApprovalError(error instanceof Error ? error.message : "Could not record the waiver.");
      }
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setWaiving(null);
    }
  }

  const result = progress?.result;
  const target = result?.publication_target;
  const publication = result?.publication;
  const waivers = result?.publication_waivers ?? [];
  const waiverFor = (index: number, checkId: string) =>
    waivers.find((waiver) => waiver.check_index === index && waiver.check_id === checkId);
  // Required checks that could not run block publication until the owner waives each one.
  const unwaived = result?.validation.filter((check, index) =>
    check.required && check.status !== "PASSED" && !waiverFor(index, check.check_id)).length ?? 0;
  const canWaive = status === "idle" && Boolean(target) && !result?.publication_approval && !publication;
  const activity = status && status !== "failed" && status !== "idle" ? activeMessages[status] : null;
  return (
    <section className="saved-run" aria-labelledby="candidate-result-title">
      <h4 id="candidate-result-title">Candidate result</h4>
      <button className="repository-check" type="button" onClick={loadResult} disabled={loading || approving}>
        Refresh saved result
      </button>
      {!progress && !loadError && <p role="status">Loading saved result…</p>}
      {status === "idle" && !result && <p role="status">No validated candidate has been saved for this run yet.</p>}
      {activity && !loadError && publication?.status !== "branch_pushed" && <p role="status">{activity}</p>}
      {loadError && <p className="error-box" role="alert">{loadError}</p>}
      {status === "failed" && <p className="error-box" role="alert">{progress?.message}</p>}
      {result && <>
        <dl className="repository-details">
          <div><dt>Result revision</dt><dd>{result.revision}</dd></div>
          <div><dt>Approved plan revision</dt><dd>{result.approved_plan.revision}</dd></div>
          <div><dt>Result SHA-256</dt><dd><code>{result.sha256}</code></dd></div>
        </dl>
        <h5>Validation checks</h5>
        <ul>{result.validation.map((check, index) => {
          const waiver = waiverFor(index, check.check_id);
          const reasonId = `waiver-reason-${index}`;
          return <li key={index}>
            <code>{check.check_id}</code> · {check.status} · {check.required ? "Required" : "Optional"}
            {check.exit_code !== null && <span> · Exit code {check.exit_code}</span>}
            {check.paths.length > 0 && <p>Files {check.paths.join(", ")}</p>}
            {check.reason && <p>{check.reason}</p>}
            {check.output && <pre className="analysis-result">{check.output}</pre>}
            {waiver && <p role="status">Waived by the owner: {waiver.reason}</p>}
            {!waiver && check.required && check.status === "UNAVAILABLE" && canWaive && <div>
              <label htmlFor={reasonId}>Reason to publish without check {index + 1}</label>
              <textarea id={reasonId} maxLength={500} rows={2} value={waiverReasons[index] ?? ""}
                onChange={(event) => setWaiverReasons((reasons) => ({ ...reasons, [index]: event.target.value }))} />
              <button className="repository-check" type="button" onClick={() => waiveCheck(result, index)}
                disabled={!(waiverReasons[index] ?? "").trim() || waiving !== null || loading || approving}>
                {waiving === index ? "Recording waiver…" : `Waive check ${index + 1}`}
              </button>
            </div>}
          </li>;
        })}</ul>
        {result.review && <>
          <h5>Plan coverage in the diff</h5>
          <ul>{result.review.files.map((file) => <li key={file.path}>
            {file.action} <code>{file.path}</code> · {file.diff_line === null ? "No change in diff" : `Diff line ${file.diff_line}`}
          </li>)}</ul>
          <ul>{result.review.criteria.map((criterion) => <li key={criterion.id}>
            <code>{criterion.id}</code> · {criterion.description} · {criterion.passing_checks.length
              ? `Linked checks passed (${criterion.passing_checks.join(", ")})` : "Behavior unverified"}
          </li>)}</ul>
          {result.review.routes_to_review.length > 0 && <><h5>New routes to review</h5>
            <ul>{result.review.routes_to_review.map((route) => <li key={`${route.path}:${route.diff_line}`}>
              <code>{route.route}</code> in <code>{route.path}</code> · Diff line {route.diff_line}
            </li>)}</ul>
          </>}
        </>}
        <h5>Saved diff</h5>
        <pre className="analysis-result">{result.diff}</pre>
      </>}
      {result && <div className="saved-run">
        {target && <>
          <h5>Publication target</h5>
          <dl className="repository-details">
            <div><dt>Commit</dt><dd><code>{target.commit_sha}</code></dd></div>
            <div><dt>Repository ID</dt><dd>{target.destination.repository_id}</dd></div>
            <div><dt>Base branch</dt><dd><code>{target.destination.base_branch}</code></dd></div>
            <div><dt>Head branch</dt><dd><code>{target.destination.head_branch}</code></dd></div>
          </dl>
        </>}
        {publication?.status === "branch_pushed" && <p role="status">
          {status === "publishing" && !loadError ? "Branch pushed. Waiting for the draft PR." : "Branch pushed."}
        </p>}
        {publication?.status === "draft_opened" && <p role="status">
          Draft PR opened.{" "}
          <a className="publication-link" href={publication.url}>View draft PR #{publication.number} on GitHub</a>
        </p>}
        {status === "idle" && !publication && (result.publication_approval ? (
          <p role="status">Publication approved for result revision {result.publication_approval.result_revision}.</p>
        ) : target ? <>
          <p role="status">Candidate ready for review.</p>
          {unwaived > 0 && <p>Waive each required check that could not run before approving publication.</p>}
          <button className="repository-check" type="button" onClick={() => approvePublication(result)} disabled={loading || approving || waiving !== null || unwaived > 0 || Boolean(loadError)}>
            {approving ? "Recording approval…" : "Approve publication"}
          </button>
        </> : <p>Publication target has not been prepared.</p>)}
        {approvalError && <p className="error-box" role="alert">{approvalError}</p>}
      </div>}
    </section>
  );
}
