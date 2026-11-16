"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { request, type MockRun, type RunEvent } from "@/lib/api";

const savedRunKey = "refactra:last-mock-run";

export function RunConsole() {
  const [task, setTask] = useState("Extract duplicated calculation logic into a shared helper while preserving behavior.");
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<MockRun | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [retry, setRetry] = useState(0);
  const submissionKey = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    request<{ mock_enabled: boolean }>("capabilities", { signal: controller.signal })
      .then((capabilities) => {
        setEnabled(capabilities.mock_enabled);
        if (!capabilities.mock_enabled) setError("Mock mode is disabled on the local API.");
        const saved = localStorage.getItem(savedRunKey);
        if (saved && /^[0-9a-f-]{36}$/i.test(saved)) setRunId(saved);
      })
      .catch((err: Error) => { if (!controller.signal.aborted) setError(err.message); });
    return () => controller.abort();
  }, [retry]);

  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    async function poll() {
      try {
        const [current, timeline] = await Promise.all([
          request<MockRun>(`mock/runs/${runId}`, { signal: controller.signal }),
          request<{ items: RunEvent[] }>(`mock/runs/${runId}/events?limit=100`, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setRun(current);
        setEvents(timeline.items);
        setError(null);
        failures = 0;
        if (current.status === "CREATED" || current.status === "ANALYZING") timer = setTimeout(poll, 2000);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Could not load this run.");
        if (++failures < 4) timer = setTimeout(poll, 2000 * failures);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, retry]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    submissionKey.current ??= crypto.randomUUID();
    try {
      const created = await request<MockRun>("mock/runs", {
        method: "POST",
        headers: { "Idempotency-Key": submissionKey.current },
        body: JSON.stringify({ task_text: task }),
      });
      setRun(created);
      setEvents([]);
      setRunId(created.run_id);
      localStorage.setItem(savedRunKey, created.run_id);
      submissionKey.current = null;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not submit the task.");
    } finally { setBusy(false); }
  }

  return (
    <section className="console-grid" aria-label="Mock run workspace">
      <div className="task-card">
        <div className="card-heading"><h2>Start with a task</h2><span className="badge">MOCK MODE</span></div>
        <p className="card-description">Try the local queue. No repository is accessed and no model is called.</p>
        <form onSubmit={submit}>
          <label htmlFor="task">What would you like to refactor?</label>
          <textarea id="task" value={task} maxLength={4000} rows={4} required disabled={busy}
            onChange={(event) => { setTask(event.target.value); submissionKey.current = null; }} />
          <div className="form-bottom"><span>Controlled fixture · Python example</span><span>{task.length}/4000</span></div>
          <button className="primary-button" disabled={busy || !enabled || !task.trim()} type="submit">
            {busy ? "Saving task…" : "Start mock run"}<span aria-hidden="true">↗</span>
          </button>
        </form>
        <div className="boundary-note"><span aria-hidden="true">ⓘ</span><p>This run demonstrates persistence and worker processing. It stops before planning, approvals or code changes.</p></div>
        {error && <div className="error-box" role="alert"><p>{error}</p><button onClick={() => setRetry((value) => value + 1)} type="button">Retry connection</button></div>}
      </div>
      <div className="activity-card" aria-live="polite" aria-atomic="false">
        <div className="card-heading"><h2>Run activity</h2><span className="activity-label">{run ? "PERSISTED" : "READY WHEN YOU ARE"}</span></div>
        {!run ? <div className="empty-state"><span className="empty-icon" aria-hidden="true">≋</span><h3>Your task starts here.</h3><p>Submit a mock run to follow its journey from API to database to worker.</p></div> : <>
          <div className="run-summary"><span className="run-state">{run.status.replaceAll("_", " ").toLowerCase()}</span><code title={run.run_id}>{run.run_id.slice(0, 8)}</code></div>
          <ol className="event-list">{events.map((event) => <li key={event.id}><span className="event-marker" aria-hidden="true" /><div><strong>{event.event_type.replaceAll("_", " ").toLowerCase()}</strong><p>{event.payload.message}</p></div></li>)}</ol>
          {run.status === "CREATED" && <p className="waiting-note">Waiting for the worker. The task stays in PostgreSQL if the worker is stopped.</p>}
          {run.stop_reason && <p className="stop-note">{run.stop_reason.message}</p>}
        </>}
      </div>
    </section>
  );
}
