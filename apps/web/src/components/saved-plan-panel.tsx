"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, request } from "@/lib/api";
import { CandidateResultPanel } from "./candidate-result-panel";

type ApprovalReceipt = { revision: number };
type PlanWrite = "revision" | "approval" | "feedback";

type SavedPlan = {
  artifact: { id: string; revision: number; sha256: string };
  approval: ApprovalReceipt | null;
  feedback_status?: "pending" | "failed" | "unchanged" | null;
  readiness: { unsupported_check_ids: string[]; has_required_supported_check: boolean; scope_errors: string[] };
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
  const [draftEdited, setDraftEdited] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const plan = state.status === "loaded" ? state.plan : null;
  const revising = plan?.feedback_status === "pending";
  const blockers = plan ? [
    ...(plan.content.blocking_questions.length ? ["Resolve the blocking questions."] : []),
    ...(!plan.readiness.has_required_supported_check ? ["Add a required supported validation check."] : []),
    ...(plan.readiness.unsupported_check_ids.length ? ["Replace unsupported validation checks."] : []),
    ...(plan.readiness.scope_errors ?? []),
  ] : [];
  const canApprove = plan?.approval === null && !draftEdited && !revising && blockers.length === 0;

  const loadPlan = useCallback(async (background = false) => {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    if (!background) setState({ status: "loading" });
    setWriteError(null);
    setDraftEdited(false);
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
        setDraftEdited(false);
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = error instanceof ApiError && error.code === "invalid_request"
        ? "Check the plan fields and try again."
        : error instanceof Error ? error.message : "Could not record the plan change.";
      setWriteError({ kind, message });
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setWriting(null);
    }
  }

  function saveRevision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!plan || plan.approval || pending.current || revising) return;
    const { content } = plan;
    const fields = new FormData(event.currentTarget);
    const value = (name: string) => String(fields.get(name) ?? "").trim();
    const proposal = {
      summary: value("summary"),
      changes: content.changes.map((_, i) => ({
        path: value(`path-${i}`), action: value(`action-${i}`), intent: value(`intent-${i}`),
      })),
      acceptance_criteria: content.acceptance_criteria.map((item, i) => ({
        ...item, description: value(`criterion-${i}`),
      })),
      validation_checks: content.validation_checks.map((check, i) => ({
        ...check, check_id: value(`check-${i}`),
        paths: String(fields.get(`targets-${i}`) ?? "").split(/\r?\n/).filter(Boolean),
      })),
      assumptions: content.assumptions,
      preserve_behavior: content.preserve_behavior,
      blocking_questions: content.blocking_questions,
    };
    void writePlan("revision", plan, { expected_revision: plan.artifact.revision, proposal });
  }

  function sendFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!plan || plan.approval || draftEdited || pending.current || revising) return;
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
        {plan.approval && <>
          <PlanNotes title="Blocking questions" entries={plan.content.blocking_questions} />
          <p role="status">Approval recorded for revision {plan.approval.revision}.</p>
        </>}
        {!plan.approval && <details className="saved-run" key={plan.artifact.sha256}>
          <summary>Edit plan</summary>
          <form onSubmit={saveRevision} onChange={() => setDraftEdited(true)} onReset={() => setDraftEdited(false)}>
            <fieldset disabled={writing !== null || revising}>
              <label htmlFor="edit-summary">Summary</label>
              <textarea id="edit-summary" name="summary" defaultValue={plan.content.summary} required />
              {plan.content.changes.map((edit, i) => <fieldset key={edit.path}>
                <legend>Change {i + 1}</legend>
                <label>File path<input name={`path-${i}`} defaultValue={edit.path} required /></label>
                <label>Action<select name={`action-${i}`} defaultValue={edit.action}>
                  <option>CREATE</option><option>MODIFY</option><option>DELETE</option>
                </select></label>
                <label htmlFor={`edit-intent-${i}`}>Intent</label>
                <textarea id={`edit-intent-${i}`} name={`intent-${i}`} defaultValue={edit.intent} required />
              </fieldset>)}
              {plan.content.acceptance_criteria.map((item, i) => <label key={item.id}>
                Criterion {item.id}<input name={`criterion-${i}`} defaultValue={item.description} required />
              </label>)}
              {plan.content.validation_checks.map((check, i) => <fieldset key={i}>
                <legend>Check {i + 1}</legend>
                <label>Check ID<input name={`check-${i}`} defaultValue={check.check_id} required /></label>
                <label htmlFor={`edit-targets-${i}`}>Test files, one per line</label>
                <textarea id={`edit-targets-${i}`} name={`targets-${i}`} defaultValue={check.paths.join("\n")} />
              </fieldset>)}
              <button className="repository-check" type="reset">Discard edits</button>
            </fieldset>
            {writeError?.kind === "revision" && <p className="error-box" role="alert">{writeError.message}</p>}
            <button className="repository-check" type="submit" disabled={writing !== null || revising || !draftEdited}>
              {writing === "revision" ? "Saving revision…" : "Save plan revision"}
            </button>
          </form>
        </details>}
        {plan.approval === null && <div className="saved-run">
          <form key={plan.artifact.id} onSubmit={sendFeedback}>
            <fieldset disabled={writing !== null || revising || draftEdited}>
              <legend>Request changes</legend>
              <h4>Blocking questions</h4>
              {!plan.content.blocking_questions.length && <p>None recorded.</p>}
              {plan.content.blocking_questions.map((question, i) => <label key={i}>
                {question}<textarea name={`answer-${i}`} rows={2} maxLength={1000} />
              </label>)}
              <label>Changes you want<textarea name="notes" rows={3} maxLength={2000} /></label>
              <button className="repository-check" type="submit">Update plan</button>
            </fieldset>
          </form>
          {revising && <p role="status">Updating plan…</p>}
          {plan.feedback_status === "failed" && <p role="alert">The worker could not update the plan. Revise your feedback or edit the draft.</p>}
          {plan.feedback_status === "unchanged" && <p role="status">The worker returned the same plan.</p>}
          {writeError?.kind === "feedback" && <p role="alert">{writeError.message}</p>}
          <div id="plan-approval-hint">
            {draftEdited && <p>Save your edits before approving.</p>}
            {blockers.length > 0
              ? <ul>{blockers.map((reason) => <li key={reason}>{reason}</li>)}</ul>
              : !draftEdited && <p>Approval locks this saved revision.</p>}
          </div>
          {writeError?.kind === "approval" && <p className="error-box" role="alert">{writeError.message}</p>}
          <button className="repository-check" type="button" onClick={approvePlan}
            aria-describedby="plan-approval-hint" disabled={!canApprove || writing !== null}>
            {writing === "approval" ? "Recording approval…" : `Approve plan revision ${plan.artifact.revision}`}
          </button>
        </div>}
        {plan.approval && <CandidateResultPanel runId={runId} />}
      </>}
    </section>
  );
}

function PlanReview({ plan: { artifact, content, readiness } }: { plan: SavedPlan }) {
  return (
    <>
      <dl className="repository-details">
        <div><dt>Plan revision</dt><dd aria-live="polite">{artifact.revision}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{artifact.sha256}</code></dd></div>
      </dl>
      <h4>Summary</h4>
      <p>{content.summary}</p>
      <div>
        <h4>Proposed changes</h4>
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
          <p>Unsupported checks {readiness.unsupported_check_ids.join(", ")}</p>}
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
