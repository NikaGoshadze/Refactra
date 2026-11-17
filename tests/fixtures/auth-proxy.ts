import { test as base, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import path from "node:path";

type Call = { url: string; method: string; headers: IncomingHttpHeaders };
type Proxy = { origin: string; calls: Call[] };
export const OAUTH_COOKIE = "refactra_oauth=fixture-attempt; Path=/; HttpOnly; SameSite=Lax";
export const SESSION_COOKIE = "refactra_session=fixture-session; Path=/; HttpOnly; SameSite=Lax";
export const CLEAR_OAUTH = "refactra_oauth=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax";
export const CLEAR_SESSION = "refactra_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax";

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
      calls.push({ url: request.url!, method: request.method!, headers: request.headers });
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
