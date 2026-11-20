"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, request } from "@/lib/api";

type SavedPlan = {
  artifact: { revision: number; sha256: string };
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
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function loadPlan() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setState({ status: "loading" });
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

  return (
    <section className="saved-plan" aria-labelledby="saved-plan-title">
      <h3 id="saved-plan-title">Refactoring plan</h3>
      <button className="repository-check" type="button" onClick={loadPlan} disabled={state.status === "loading"}>
        {state.status === "idle" ? "Load saved plan" : "Refresh saved plan"}
      </button>
      {state.status === "loading" && <p role="status">Loading saved plan…</p>}
      {state.status === "missing" && <p role="status">No plan has been saved for this run yet.</p>}
      {state.status === "error" && <p className="error-box" role="alert">{state.message}</p>}
      {state.status === "loaded" && <PlanReview plan={state.plan} />}
    </section>
  );
}

function PlanReview({ plan: { artifact, content } }: { plan: SavedPlan }) {
  return (
    <>
      <dl className="repository-details">
        <div><dt>Plan revision</dt><dd>{artifact.revision}</dd></div>
        <div><dt>SHA-256</dt><dd><code>{artifact.sha256}</code></dd></div>
      </dl>
      <p>{content.summary}</p>
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
