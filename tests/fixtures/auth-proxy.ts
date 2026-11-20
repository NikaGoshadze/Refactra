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
      } else if (route.startsWith("/api/v1/runs/")) {
        const authenticated = request.headers.cookie?.includes("refactra_session=fixture-session");
        const status = !authenticated ? 401 : route === RUN_PATH ? 200 : 404;
        const body = status === 200
          ? { run_id: RUN_ID, status: "NEEDS_HUMAN_REVIEW", analysis_text: "Fixture analysis" }
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
