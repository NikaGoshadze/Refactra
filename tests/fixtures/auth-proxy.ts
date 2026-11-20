import { test as base, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import path from "node:path";

type Call = { url: string; method: string; headers: IncomingHttpHeaders; body?: string };
type Proxy = { origin: string; calls: Call[] };
export const OAUTH_COOKIE = "refactra_oauth=fixture-attempt; Path=/; HttpOnly; SameSite=Lax";
export const SESSION_COOKIE = "refactra_session=fixture-session; Path=/; HttpOnly; SameSite=Lax";
export const CLEAR_OAUTH = "refactra_oauth=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax";
export const CLEAR_SESSION = "refactra_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax";
export const RUN_ID = "367dfc44-100b-4af2-9ed7-ddf97ae24d52";
export const RUN_PATH = `/api/v1/runs/${RUN_ID}`;
export const PLAN_PATH = `${RUN_PATH}/plan`;
export const PLAN_APPROVAL_PATH = `${PLAN_PATH}/approve`;
export const REVISED_ARTIFACT = {
  id: "98c1fd19-7102-4250-a458-b9aa083db613", revision: 2, sha256: "c".repeat(64),
};
export const PLAN_PROPOSAL = {
  summary: "<strong>Extract the cart calculation</strong>",
  changes: [{ path: "src/cart.py", action: "MODIFY", intent: "Extract calculation" }],
  acceptance_criteria: [{ id: "AC-1", description: "Preserve totals" }],
  validation_checks: [{ check_id: "VC1", required: true, paths: ["tests/test_cart.py"], criteria_ids: ["AC-1"] }],
  preserve_behavior: ["Return the same total for empty carts."],
  assumptions: ["Keep the public function signature."],
  blocking_questions: ["Which rounding rule should be preserved?"],
};
export const PLAN = {
  artifact: { id: "0dee55db-3437-44ca-bf7b-afa8977a06d3", revision: 1, sha256: "b".repeat(64) },
  approval: null,
  readiness: { blocking_question_count: 1, unsupported_check_ids: ["VC1"], has_required_supported_check: false },
  content: {
    ...PLAN_PROPOSAL, schema_version: "1", run_id: RUN_ID,
    repository: { repository_id: 1001132861, base_branch: "main", base_sha: "a".repeat(40) },
  },
};
export const SAVED_RUN = {
  run_id: RUN_ID, repository_id: 1001132861, base_branch: "main", base_sha: "a".repeat(40),
  status: "NEEDS_HUMAN_REVIEW", analysis_text: "Fixture analysis", stop_reason: null,
};

async function listen(server: Server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return (server.address() as { port: number }).port;
}

async function close(server: Server) {
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

export const test = base.extend<{}, { authProxy: Proxy }>({
  authProxy: [async ({}, use) => {
    const calls: Call[] = [];
    let origin = "";
    const api = createServer((request, response) => {
      const call: Call = { url: request.url!, method: request.method!, headers: request.headers };
      calls.push(call);
      const route = request.url!.split("?")[0];
      response.setHeader("Content-Type", "application/json");
      if (route === "/api/v1/auth/github/login") {
        response.writeHead(302, { Location: "https://github.com/login/oauth/authorize?client_id=fixture", "Set-Cookie": OAUTH_COOKIE });
      } else if (route === "/api/v1/auth/github/callback") {
        response.writeHead(302, { Location: `${origin}/`, "Set-Cookie": [SESSION_COOKIE, CLEAR_OAUTH] });
      } else if (route === "/api/v1/auth/session") {
        const authenticated = request.headers.cookie?.includes("refactra_session=fixture-session") ?? false;
        response.end(JSON.stringify({ authenticated, user: authenticated ? { user_id: 12345, login: "fixture-user" } : null }));
        return;
      } else if (route === "/api/v1/auth/logout") {
        if (request.headers.origin !== origin || request.headers["x-refactra-csrf"] !== "1") {
          response.writeHead(403).end(JSON.stringify({ error: { code: "csrf_rejected" } }));
          return;
        }
        response.writeHead(204, { "Set-Cookie": [CLEAR_SESSION, CLEAR_OAUTH] });
      } else if (/^\/api\/v1\/repositories\/[1-9][0-9]*$/.test(route)) {
        const id = Number(route.split("/").at(-1));
        if (id === 47) { request.socket.destroy(); return; }
        const authenticated = request.headers.cookie?.includes("refactra_session=fixture-session") ?? false;
        const statuses: Record<number, number> = { 43: 403, 44: 502, 45: 503 };
        const status = !authenticated ? 401 : (statuses[id] ?? 200);
        const codes: Record<number, string> = { 401: "authentication_required", 403: "repository_access_denied", 502: "github_unavailable", 503: "repository_access_unconfigured" };
        const body = status === 200
          ? { repository_id: id, full_name: "fixture/example", default_branch: "main", access: "read-only" }
          : { error: { code: codes[status], message: "Fixture repository access failed." } };
        response.writeHead(status, {
          "Cache-Control": "public, max-age=3600", "Set-Cookie": "unrelated=must-not-pass; Path=/",
        });
        if (id === 46) setTimeout(() => response.end(JSON.stringify(body)), 5500);
        else response.end(JSON.stringify(body));
        return;
      } else if (route === "/api/v1/runs") {
        request.setEncoding("utf8");
        call.body = "";
        request.on("data", (chunk) => { call.body += chunk; });
        request.on("end", () => {
          if (request.headers.origin !== origin || request.headers["x-refactra-csrf"] !== "1") {
            response.writeHead(403).end(JSON.stringify({ error: { code: "csrf_rejected" } }));
            return;
          }
          response.writeHead(202, { Location: RUN_PATH, "Cache-Control": "public, max-age=3600" });
          setTimeout(() => response.end(JSON.stringify({ run_id: RUN_ID })), 5500);
        });
        return;
      } else if ([PLAN_PATH, PLAN_APPROVAL_PATH].includes(route) && request.method === "POST") {
        request.setEncoding("utf8");
        call.body = "";
        request.on("data", (chunk) => { call.body += chunk; });
        request.on("end", () => {
          if (request.headers.origin !== origin || request.headers["x-refactra-csrf"] !== "1") {
            response.writeHead(403).end(JSON.stringify({ error: { code: "csrf_rejected" } }));
            return;
          }
          if (!request.headers.cookie?.includes("refactra_session=fixture-session")) {
            response.writeHead(401).end(JSON.stringify({ error: { code: "authentication_required" } }));
            return;
          }
          if (route === PLAN_APPROVAL_PATH) {
            const { plan_id, revision, sha256 } = JSON.parse(call.body!);
            if (plan_id !== REVISED_ARTIFACT.id || revision !== REVISED_ARTIFACT.revision || sha256 !== REVISED_ARTIFACT.sha256) {
              response.writeHead(409).end(JSON.stringify({ error: { code: "plan_approval_conflict" } }));
              return;
            }
            response.end(JSON.stringify({ run_id: RUN_ID, approved_by: 12345,
              approved_at: "2026-11-20T01:00:00Z", revision, sha256 }));
            return;
          }
          const { expected_revision, proposal } = JSON.parse(call.body!);
          if (expected_revision !== 1) {
            response.writeHead(409).end(JSON.stringify({ error: { code: "plan_revision_conflict" } }));
            return;
          }
          response.end(JSON.stringify({ content: { ...PLAN.content, ...proposal }, artifact: REVISED_ARTIFACT, approval: null,
            readiness: { blocking_question_count: 0, unsupported_check_ids: [], has_required_supported_check: true } }));
        });
        return;
      } else if (route.startsWith("/api/v1/runs/")) {
        const authenticated = request.headers.cookie?.includes("refactra_session=fixture-session");
        const status = !authenticated ? 401 : [RUN_PATH, PLAN_PATH].includes(route) ? 200 : 404;
        const body = status === 200
          ? route === PLAN_PATH ? PLAN : SAVED_RUN
          : { error: { code: status === 401 ? "authentication_required" : "not_found" } };
        response.writeHead(status, {
          "Cache-Control": "public, max-age=3600", "Set-Cookie": "unrelated=must-not-pass; Path=/",
        }).end(JSON.stringify(body));
        return;
      } else {
        response.writeHead(404);
      }
      response.end();
    });
    const apiPort = await listen(api);
    const probe = createServer();
    const webPort = await listen(probe);
    await close(probe);
    origin = `http://127.0.0.1:${webPort}`;
    const web = spawn(process.execPath, [require.resolve("next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)], {
      cwd: path.resolve(__dirname, "../../apps/web"), windowsHide: true,
      env: { ...process.env, REFACTRA_API_URL: `http://127.0.0.1:${apiPort}`, NEXT_TELEMETRY_DISABLED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let logs = "";
    web.stdout?.on("data", (chunk) => { logs += chunk; });
    web.stderr?.on("data", (chunk) => { logs += chunk; });
    try {
      await expect.poll(async () => {
        try { return (await fetch(origin, { signal: AbortSignal.timeout(1000) })).status; }
        catch { return 0; }
      }, { timeout: 15000, message: "Isolated Next.js server must start after npm run build" }).toBe(200);
      await use({ origin, calls });
    } catch (error) {
      throw new Error(`Auth proxy integration failed. Next.js output:\n${logs}`, { cause: error });
    } finally {
      if (web.exitCode === null && web.signalCode === null) {
        const exited = once(web, "exit");
        web.kill();
        await exited;
      }
      await close(api);
    }
  }, { scope: "worker" }],
});

export { expect };
