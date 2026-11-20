"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, request } from "@/lib/api";

type PublicationApproval = { result_revision: number };
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
  publication_target: PublicationTarget | null;
  publication_approval: PublicationApproval | null;
  publication: { status: "branch_pushed" } | { status: "draft_opened"; number: number; url: string } | null;
  validation: {
    check_id: string;
    paths: string[];
    status: string;
    exit_code: number | null;
    output: string;
    reason: string | null;
  }[];
};

type ResultState =
  | { status: "idle" | "loading" | "missing" }
  | { status: "loaded"; result: CandidateResult }
  | { status: "error"; message: string };

export function CandidateResultPanel({ runId }: { runId: string }) {
  const [state, setState] = useState<ResultState>({ status: "idle" });
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function loadResult() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setApprovalError(null);
    setState({ status: "loading" });
    try {
      const result = await request<CandidateResult>(`runs/${runId}/result`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) setState({ status: "loaded", result });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.code === "candidate_result_not_found") {
        setState({ status: "missing" });
      } else {
        const message = error instanceof DOMException && error.name === "TimeoutError"
          ? "Result lookup timed out. Try again."
          : error instanceof Error ? error.message : "Could not load the saved result.";
        setState({ status: "error", message });
      }
    } finally {
      pending.current = null;
    }
  }

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
        setState({ status: "loaded", result: { ...result, publication_approval: approval } });
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

  const result = state.status === "loaded" ? state.result : null;
  const target = result?.publication_target;
  const publication = result?.publication;
  return (
    <section className="saved-run" aria-labelledby="candidate-result-title">
      <h4 id="candidate-result-title">Candidate result</h4>
      <button className="repository-check" type="button" onClick={loadResult} disabled={state.status === "loading" || approving}>
        {state.status === "idle" ? "Load saved result" : "Refresh saved result"}
      </button>
      {state.status === "loading" && <p role="status">Loading saved result…</p>}
      {state.status === "missing" && <p role="status">No validated candidate has been saved for this run yet.</p>}
      {state.status === "error" && <p className="error-box" role="alert">{state.message}</p>}
      {state.status === "loaded" && <>
        <dl className="repository-details">
          <div><dt>Result revision</dt><dd>{state.result.revision}</dd></div>
          <div><dt>Approved plan revision</dt><dd>{state.result.approved_plan.revision}</dd></div>
          <div><dt>Result SHA-256</dt><dd><code>{state.result.sha256}</code></dd></div>
        </dl>
        <h5>Validation checks</h5>
        <ul>{state.result.validation.map((check, index) => (
          <li key={index}>
            <code>{check.check_id}</code> · {check.status}
            {check.exit_code !== null && <span> · Exit code {check.exit_code}</span>}
            {check.paths.length > 0 && <p>Files {check.paths.join(", ")}</p>}
            {check.reason && <p>{check.reason}</p>}
            {check.output && <pre className="analysis-result">{check.output}</pre>}
          </li>
        ))}</ul>
        <h5>Saved diff</h5>
        <pre className="analysis-result">{state.result.diff}</pre>
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
        {publication?.status === "branch_pushed" && <p role="status">Branch pushed. Waiting for the draft PR.</p>}
        {publication?.status === "draft_opened" && <p role="status">
          Draft PR opened.{" "}
          <a className="publication-link" href={publication.url}>View draft PR #{publication.number} on GitHub</a>
        </p>}
        {!publication && (result.publication_approval ? (
          <p role="status">Publication approved for result revision {result.publication_approval.result_revision}.</p>
        ) : target ? (
          <button className="repository-check" type="button" onClick={() => approvePublication(result)} disabled={approving}>
            {approving ? "Recording approval…" : "Approve publication"}
          </button>
        ) : <p>Waiting for a prepared publication target.</p>)}
        {approvalError && <p className="error-box" role="alert">{approvalError}</p>}
      </div>}
    </section>
  );
}
