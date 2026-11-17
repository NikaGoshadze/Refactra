export type MockRun = {
  run_id: string;
  mode: "mock";
  status: "CREATED" | "ANALYZING" | "NEEDS_HUMAN_REVIEW" | "FAILED";
  task_text: string;
  version: number;
  workflow_version: string;
  created_at: string;
  updated_at: string;
  stop_reason: { code: string; message: string } | null;
  validation: null;
  pull_request: null;
};

export type RunEvent = {
  id: number;
  event_type: string;
  payload: { message?: string; mode?: string; attempt?: number };
  occurred_at: string;
};

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, {
    ...init,
    cache: "no-store",
    signal: init?.signal ?? AbortSignal.timeout(7000),
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message ?? "The local API could not handle the request.");
  return body as T;
}
