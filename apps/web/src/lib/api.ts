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

export type Access = "allowed" | "waitlisted" | "denied";
export type SessionStatus =
  | { authenticated: false; user: null; access?: null }
  | { authenticated: true; user: { user_id: number; login: string }; access?: Access };

/** What a signed-in user without access sees instead of the workspace, or null if allowed. */
export function accessNotice(session: SessionStatus | null): string | null {
  if (!session?.authenticated || !session.access || session.access === "allowed") return null;
  return session.access === "waitlisted"
    ? "You're on the Refactra waitlist. You'll get access once the owner invites you."
    : "Your account does not have access to Refactra.";
}

export class ApiError extends Error {
  constructor(message: string, readonly code: string | undefined) {
    super(message);
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, {
    ...init,
    cache: "no-store",
    signal: init?.signal ?? AbortSignal.timeout(7000),
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (response.status === 204) return undefined as T;
  const body = await response.json();
  if (!response.ok) {
    throw new ApiError(body.error?.message ?? "The local API could not handle the request.", body.error?.code);
  }
  return body as T;
}
