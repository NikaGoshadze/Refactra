"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, request } from "@/lib/api";

type CandidateResult = {
  revision: number;
  sha256: string;
  approved_plan: { revision: number };
  diff: string;
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
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function loadResult() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
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

  return (
    <section className="saved-run" aria-labelledby="candidate-result-title">
      <h4 id="candidate-result-title">Candidate result</h4>
      <button className="repository-check" type="button" onClick={loadResult} disabled={state.status === "loading"}>
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
    </section>
  );
}
