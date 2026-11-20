"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, request } from "@/lib/api";

type SavedPlan = {
  artifact: { revision: number; sha256: string };
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
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function loadPlan() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setState({ status: "loading" });
    setSaveError(null);
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

  async function saveRevision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current || state.status !== "loaded") return;
    let proposal: unknown;
    try {
      proposal = JSON.parse(String(new FormData(event.currentTarget).get("proposal")));
    } catch {
      setSaveError("Enter valid JSON for the plan draft.");
      return;
    }
    const controller = new AbortController();
    pending.current = controller;
    setSaving(true);
    setSaveError(null);
    try {
      const plan = await request<SavedPlan>(`runs/${runId}/plan`, {
        method: "POST",
        headers: { "X-Refactra-CSRF": "1" },
        body: JSON.stringify({ expected_revision: state.plan.artifact.revision, proposal }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(65000)]),
      });
      if (!controller.signal.aborted) setState({ status: "loaded", plan });
    } catch (error) {
      if (controller.signal.aborted) return;
      setSaveError(error instanceof ApiError && error.code === "invalid_request"
        ? "Check the draft fields and keep the structure of the original plan."
        : error instanceof Error ? error.message : "Could not save the plan revision.");
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  return (
    <section className="saved-plan" aria-labelledby="saved-plan-title">
      <h3 id="saved-plan-title">Refactoring plan</h3>
      <button className="repository-check" type="button" onClick={loadPlan} disabled={state.status === "loading" || saving}>
        {state.status === "idle" ? "Load saved plan" : "Refresh saved plan"}
      </button>
      {state.status === "loading" && <p role="status">Loading saved plan…</p>}
      {state.status === "missing" && <p role="status">No plan has been saved for this run yet.</p>}
      {state.status === "error" && <p className="error-box" role="alert">{state.message}</p>}
      {state.status === "loaded" && <>
        <PlanReview plan={state.plan} />
        <details className="saved-run" key={state.plan.artifact.sha256}>
          <summary>Revise plan draft</summary>
          <form onSubmit={saveRevision}>
            <p id="plan-draft-hint">Record answers in the relevant plan fields, then remove the resolved questions.</p>
            <label htmlFor="plan-draft">Plan draft JSON</label>
            <textarea id="plan-draft" name="proposal" defaultValue={draftJson(state.plan.content)}
              aria-describedby="plan-draft-hint" rows={12} required disabled={saving} />
            {saveError && <p className="error-box" role="alert">{saveError}</p>}
            <button className="repository-check" type="submit" disabled={saving}>
              {saving ? "Saving revision…" : "Save plan revision"}
            </button>
          </form>
        </details>
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
