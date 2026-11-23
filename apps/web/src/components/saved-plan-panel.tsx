"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, request } from "@/lib/api";
import { CandidateResultPanel } from "./candidate-result-panel";

type ApprovalReceipt = { revision: number };
type PlanWrite = "approval" | "feedback";

type SavedPlan = {
  artifact: { id: string; revision: number; sha256: string };
  approval: ApprovalReceipt | null;
  feedback_status?: "pending" | "failed" | "unchanged" | null;
  readiness: { unsupported_check_ids: string[]; scope_errors: string[];
    outcome?: "ready" | "needs_clarification" | "unsupported" };
  content: {
    summary: string;
    changes: { path: string; action: string; intent: string }[];
    acceptance_criteria: { id: string; description: string }[];
    validation_checks: { check_id: string; required: boolean; paths: string[]; criteria_ids: string[] }[];
    preserve_behavior: string[];
    assumptions: string[];
    blocking_questions: string[];
  };
};

type PlanState =
  | { status: "idle" | "loading" | "pending" | "missing" }
  | { status: "loaded"; plan: SavedPlan }
  | { status: "error"; message: string };

function PlanNotes({ title, entries }: { title: string; entries: string[] }) {
  return (
    <div>
      <h4>{title}</h4>
      {entries.length ? <ul>{entries.map((entry, index) => <li key={index}>{entry}</li>)}</ul> : <p>None recorded.</p>}
    </div>
  );
}

export function SavedPlanPanel({ runId }: { runId: string }) {
  const [state, setState] = useState<PlanState>({ status: "idle" });
  const [writing, setWriting] = useState<PlanWrite | null>(null);
  const [writeError, setWriteError] = useState<{ kind: PlanWrite; message: string } | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const plan = state.status === "loaded" ? state.plan : null;
  const revising = plan?.feedback_status === "pending";
  const blockers = plan ? [
    ...(plan.content.blocking_questions.length ? ["Resolve the blocking questions."] : []),
    ...(!plan.content.changes.length ? ["Approval requires a plan with proposed changes."] : []),
    ...(plan.readiness.scope_errors ?? []),
  ] : [];
  const canApprove = plan?.approval === null && !revising && blockers.length === 0;

  const loadPlan = useCallback(async (background = false) => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    if (!background) setState({ status: "loading" });
    setWriteError(null);
    try {
      const plan = await request<SavedPlan>(`runs/${runId}/plan`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) setState({ status: "loaded", plan });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.code === "plan_pending") {
        setState({ status: "pending" });
      } else if (error instanceof ApiError && error.code === "plan_not_found") {
        setState({ status: "missing" });
      } else {
        const message = error instanceof DOMException && error.name === "TimeoutError"
          ? "Plan lookup timed out. Try again."
          : error instanceof Error ? error.message : "Could not load the saved plan.";
        setState({ status: "error", message });
      }
    } finally {
      pending.current = null;
    }
  }, [runId]);

  useEffect(() => {
    if (state.status !== "idle" && state.status !== "pending" && !revising) return;
    const timer = window.setTimeout(() => void loadPlan(revising), state.status === "idle" ? 0 : 2500);
    return () => window.clearTimeout(timer);
  }, [loadPlan, revising, state]);

  async function writePlan(kind: PlanWrite, reviewedPlan: SavedPlan, body: object) {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setWriting(kind);
    setWriteError(null);
    try {
      const path = `runs/${runId}/plan`;
      const options = {
        method: "POST",
        headers: { "X-Refactra-CSRF": "1" },
        body: JSON.stringify(body),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      };
      const saved = kind === "approval"
        ? { ...reviewedPlan, approval: await request<ApprovalReceipt>(`${path}/approve`, options) }
        : await request<SavedPlan>(path, options);
      if (!controller.signal.aborted) {
        setState({ status: "loaded", plan: saved });
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = error instanceof ApiError && error.code === "invalid_request"
        ? "The plan request was invalid. Try again."
        : error instanceof Error ? error.message : "Could not record the plan change.";
      setWriteError({ kind, message });
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setWriting(null);
    }
  }

  function sendFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!plan || plan.approval || pending.current || revising) return;
    const fields = new FormData(event.currentTarget);
    const notes = String(fields.get("notes") ?? "").trim();
    const answers = plan.content.blocking_questions.map((_, i) => String(fields.get(`answer-${i}`) ?? "").trim());
    if (!notes && !answers.some(Boolean)) {
      setWriteError({ kind: "feedback", message: "Answer a question or describe a change." });
      return;
    }
    void writePlan("feedback", plan, { expected_revision: plan.artifact.revision, notes, answers });
  }

  function approvePlan() {
    if (!plan || !canApprove) return;
    const { id, revision, sha256 } = plan.artifact;
    void writePlan("approval", plan, { plan_id: id, revision, sha256 });
  }

  return (
    <section className="saved-plan" aria-labelledby="saved-plan-title">
      <h3 id="saved-plan-title">Refactoring plan</h3>
      {(state.status === "missing" || state.status === "error") &&
        <button className="repository-check" type="button" onClick={() => void loadPlan()}>Retry plan lookup</button>}
      {(state.status === "idle" || state.status === "loading" || state.status === "pending") &&
        <p role="status">Preparing plan…</p>}
      {state.status === "missing" && <p role="status">No plan has been saved for this run yet.</p>}
      {state.status === "error" && <p className="error-box" role="alert">{state.message}</p>}
      {plan && <>
        <PlanReview plan={plan} />
        {plan.approval &&
          <p role="status">Approval recorded for revision {plan.approval.revision}.</p>
        }
        {plan.approval === null && <div className="saved-run">
          <form key={plan.artifact.id} onSubmit={sendFeedback}>
            <fieldset disabled={writing !== null || revising}>
              <legend>Request changes</legend>
              {plan.content.blocking_questions.length > 0 && <h4>Blocking questions</h4>}
              {plan.content.blocking_questions.map((question, i) => <label key={i}>
                {question}<textarea name={`answer-${i}`} rows={2} maxLength={1000} />
              </label>)}
              <label>Changes you want<textarea name="notes" rows={3} maxLength={2000} /></label>
              <button className="repository-check" type="submit">Update plan</button>
            </fieldset>
          </form>
          {revising && <p role="status">Updating plan…</p>}
          {plan.feedback_status === "failed" && <p role="alert">The worker could not update the plan. Revise your feedback and try again.</p>}
          {plan.feedback_status === "unchanged" && <p role="status">The worker returned the same plan.</p>}
          {writeError?.kind === "feedback" && <p role="alert">{writeError.message}</p>}
          {plan.content.changes.length > 0 && <>
            <div id="plan-approval-hint">
              {revising ? <p>Wait for the updated plan before approving.</p> : blockers.length > 0
                ? <ul>{blockers.map((reason) => <li key={reason}>{reason}</li>)}</ul>
                : <p>Approval locks this saved revision.</p>}
            </div>
            {writeError?.kind === "approval" && <p className="error-box" role="alert">{writeError.message}</p>}
            <button className="repository-check" type="button" onClick={approvePlan}
              aria-describedby="plan-approval-hint" disabled={!canApprove || writing !== null}>
              {writing === "approval" ? "Recording approval…" : `Approve plan revision ${plan.artifact.revision}`}
            </button>
          </>}
        </div>}
        {plan.approval && <CandidateResultPanel runId={runId} />}
      </>}
    </section>
  );
}

function PlanReview({ plan: { artifact, content, readiness } }: { plan: SavedPlan }) {
  const outcome = { ready: "Ready for review", needs_clarification: "Needs clarification", unsupported: "Unsupported" };
  return (
    <>
      <dl className="repository-details">
        <div><dt>Plan revision</dt><dd aria-live="polite">{artifact.revision}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{artifact.sha256}</code></dd></div>
      </dl>
      <h4>Summary</h4>
      {readiness.outcome && (readiness.outcome !== "unsupported" || content.changes.length > 0) &&
        <p>{outcome[readiness.outcome]}</p>}
      <p>{content.summary}</p>
      <div>
        <h4>Proposed changes</h4>
        {!content.changes.length && <p>No changes proposed.</p>}
        <ul>{content.changes.map((change) => (
          <li key={change.path}><strong>{change.action}</strong> <code>{change.path}</code> · {change.intent}</li>
        ))}</ul>
      </div>
      <PlanNotes title="Behavior to preserve" entries={content.preserve_behavior} />
      <div>
        <h4>Acceptance criteria</h4>
        <ul>{content.acceptance_criteria.map((criterion) => (
          <li key={criterion.id}><code>{criterion.id}</code> · {criterion.description}</li>
        ))}</ul>
      </div>
      <div>
        <h4>Proposed validation checks</h4>
        {readiness.unsupported_check_ids.length > 0 &&
          <p>No runner available for {readiness.unsupported_check_ids.join(", ")}</p>}
        <ul>{content.validation_checks.map((check, index) => (
          <li key={index}>
            <code>{check.check_id}</code> · {check.required ? "Required" : "Optional"}
            <span> · Criteria {check.criteria_ids.join(", ")}</span>
            {check.paths.length > 0 && <span> · Files {check.paths.join(", ")}</span>}
          </li>
        ))}</ul>
      </div>
      <PlanNotes title="Assumptions" entries={content.assumptions} />
    </>
  );
}
