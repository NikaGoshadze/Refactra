"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, request } from "@/lib/api";

type ApprovalReceipt = { revision: number };
type PlanWrite = "revision" | "approval";

type SavedPlan = {
  artifact: { id: string; revision: number; sha256: string };
  approval: ApprovalReceipt | null;
  readiness: { unsupported_check_ids: string[]; has_required_supported_check: boolean };
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
  | { status: "idle" | "loading" | "missing" }
  | { status: "loaded"; plan: SavedPlan }
  | { status: "error"; message: string };

function PlanNotes({ title, entries, empty }: { title: string; entries: string[]; empty?: string }) {
  if (!entries.length && !empty) return null;
  return (
    <div>
      <h4>{title}</h4>
      {entries.length ? <ul>{entries.map((entry, index) => <li key={index}>{entry}</li>)}</ul> : <p>{empty}</p>}
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
  const canApprove = plan?.approval === null && !draftEdited
    && plan.content.blocking_questions.length === 0
    && plan.readiness.has_required_supported_check && plan.readiness.unsupported_check_ids.length === 0;

  async function loadPlan() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setState({ status: "loading" });
    setWriteError(null);
    setDraftEdited(false);
    try {
      const plan = await request<SavedPlan>(`runs/${runId}/plan`, {
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) setState({ status: "loaded", plan });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof ApiError && error.code === "plan_not_found") {
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
  }

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
      const message = kind === "revision" && error instanceof ApiError && error.code === "invalid_request"
        ? "Check the draft fields and keep the structure of the original plan."
        : error instanceof Error ? error.message : "Could not record the plan change.";
      setWriteError({ kind, message });
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setWriting(null);
    }
  }

  function saveRevision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!plan || plan.approval || pending.current) return;
    let proposal: unknown;
    try {
      proposal = JSON.parse(String(new FormData(event.currentTarget).get("proposal")));
    } catch {
      setWriteError({ kind: "revision", message: "Enter valid JSON for the plan draft." });
      return;
    }
    void writePlan("revision", plan, { expected_revision: plan.artifact.revision, proposal });
  }

  function approvePlan() {
    if (!plan || !canApprove) return;
    const { id, revision, sha256 } = plan.artifact;
    void writePlan("approval", plan, { plan_id: id, revision, sha256 });
  }

  return (
    <section className="saved-plan" aria-labelledby="saved-plan-title">
      <h3 id="saved-plan-title">Refactoring plan</h3>
      <button className="repository-check" type="button" onClick={loadPlan} disabled={state.status === "loading" || writing !== null}>
        {state.status === "idle" ? "Load saved plan" : "Refresh saved plan"}
      </button>
      {state.status === "loading" && <p role="status">Loading saved plan…</p>}
      {state.status === "missing" && <p role="status">No plan has been saved for this run yet.</p>}
      {state.status === "error" && <p className="error-box" role="alert">{state.message}</p>}
      {plan && <>
        <PlanReview plan={plan} />
        {plan.approval && <p role="status">Approval recorded for revision {plan.approval.revision}.</p>}
        {!plan.approval && <details className="saved-run" key={plan.artifact.sha256}>
          <summary>Revise plan draft</summary>
          <form onSubmit={saveRevision}>
            <p id="plan-draft-hint">Record answers in the relevant plan fields, then remove the resolved questions.</p>
            <label htmlFor="plan-draft">Plan draft JSON</label>
            <textarea id="plan-draft" name="proposal" defaultValue={draftJson(plan.content)}
              aria-describedby="plan-draft-hint" rows={12} required disabled={writing !== null}
              onChange={(event) => setDraftEdited(event.target.value !== event.target.defaultValue)} />
            {writeError?.kind === "revision" && <p className="error-box" role="alert">{writeError.message}</p>}
            <button className="repository-check" type="submit" disabled={writing !== null}>
              {writing === "revision" ? "Saving revision…" : "Save plan revision"}
            </button>
          </form>
        </details>}
        {plan.approval === null && <div className="saved-run">
          <p id="plan-approval-hint">{draftEdited ? "Save your edits or refresh the plan before approving." : "Approval locks this saved revision."}</p>
          {writeError?.kind === "approval" && <p className="error-box" role="alert">{writeError.message}</p>}
          <button className="repository-check" type="button" onClick={approvePlan}
            aria-describedby="plan-approval-hint" disabled={!canApprove || writing !== null}>
            {writing === "approval" ? "Recording approval…" : `Approve plan revision ${plan.artifact.revision}`}
          </button>
        </div>}
      </>}
    </section>
  );
}

function draftJson(content: SavedPlan["content"]) {
  const { summary, changes, acceptance_criteria, validation_checks, preserve_behavior, assumptions, blocking_questions } = content;
  return JSON.stringify({ summary, changes, acceptance_criteria, validation_checks, preserve_behavior, assumptions, blocking_questions }, null, 2);
}

function PlanReview({ plan: { artifact, content, readiness } }: { plan: SavedPlan }) {
  return (
    <>
      <dl className="repository-details">
        <div><dt>Plan revision</dt><dd aria-live="polite">{artifact.revision}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{artifact.sha256}</code></dd></div>
      </dl>
      <p>{content.summary}</p>
      <PlanNotes title="Unrecognized validation IDs" entries={readiness.unsupported_check_ids} />
      {!readiness.has_required_supported_check && <p>A registered required validation check is missing.</p>}
      <PlanNotes title="Blocking questions" entries={content.blocking_questions} empty="None recorded." />
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
