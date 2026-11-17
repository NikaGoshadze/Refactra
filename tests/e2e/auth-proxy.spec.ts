import { test, expect, OAUTH_COOKIE, SESSION_COOKIE, CLEAR_OAUTH, CLEAR_SESSION } from "../fixtures/auth-proxy";

test.beforeEach(async ({ authProxy: proxy }) => { proxy.calls.length = 0; });

test("login redirects reach the browser with the OAuth cookie", async ({ authProxy: proxy, request }) => {
  const response = await request.get(`${proxy.origin}/api/v1/auth/github/login`, { maxRedirects: 0 });
  expect(response.status()).toBe(302);
  expect(response.headers().location).toBe("https://github.com/login/oauth/authorize?client_id=fixture");
  expect(response.headersArray().filter((item) => item.name.toLowerCase() === "set-cookie").map((item) => item.value)).toEqual([OAUTH_COOKIE]);
  expect(proxy.calls).toHaveLength(1);
});

test("callback preserves multiple cookie headers and session status", async ({ authProxy: proxy, request }) => {
  const response = await request.get(`${proxy.origin}/api/v1/auth/github/callback?code=fixture-code&state=fixture-state`, {
    maxRedirects: 0, headers: { Cookie: "refactra_oauth=fixture-attempt; analytics=private" },
  });
  expect(response.status()).toBe(302);
  expect(response.headers().location).toBe(`${proxy.origin}/`);
  expect(response.headersArray().filter((item) => item.name.toLowerCase() === "set-cookie").map((item) => item.value)).toEqual([SESSION_COOKIE, CLEAR_OAUTH]);
  expect(proxy.calls[0].url).toBe("/api/v1/auth/github/callback?code=fixture-code&state=fixture-state");
  expect(proxy.calls[0].headers.cookie).toBe("refactra_oauth=fixture-attempt");
  const status = await request.get(`${proxy.origin}/api/v1/auth/session`);
  expect(status.status()).toBe(200);
  expect(status.headers()["cache-control"]).toBe("no-store");
  expect(await status.json()).toEqual({ authenticated: true, user: { user_id: 12345, login: "fixture-user" } });
});

test("logout preserves CSRF headers and returns an empty 204 with cookie deletion", async ({ authProxy: proxy, request }) => {
  const response = await request.post(`${proxy.origin}/api/v1/auth/logout`, {
    headers: { Origin: proxy.origin, "X-Refactra-CSRF": "1", Cookie: "refactra_session=fixture-session" },
  });
  expect(response.status()).toBe(204);
  expect(await response.body()).toHaveLength(0);
  expect(response.headersArray().filter((item) => item.name.toLowerCase() === "set-cookie").map((item) => item.value)).toEqual([CLEAR_SESSION, CLEAR_OAUTH]);
  expect(proxy.calls[0].headers.origin).toBe(proxy.origin);
  expect(proxy.calls[0].headers["x-refactra-csrf"]).toBe("1");
});

for (const origin of [undefined, "https://untrusted.example"]) {
  test(`logout does not invent or replace Origin: ${origin ?? "missing"}`, async ({ authProxy: proxy, request }) => {
    const response = await request.post(`${proxy.origin}/api/v1/auth/logout`, {
      headers: { "X-Refactra-CSRF": "1", ...(origin ? { Origin: origin } : {}) },
    });
    expect(response.status()).toBe(403);
    expect(proxy.calls[0].headers.origin).toBe(origin);
    expect(await response.json()).toEqual({ error: { code: "csrf_rejected" } });
  });
}

for (const [method, route] of [["GET", "auth/logout"], ["HEAD", "auth/logout"], ["POST", "auth/session"], ["POST", "auth/github/callback"], ["GET", "auth/private"]]) {
  test(`blocks unlisted route/method: ${method} ${route}`, async ({ authProxy: proxy, request }) => {
    const response = await request.fetch(`${proxy.origin}/api/v1/${route}`, { method });
    expect(response.status()).toBe(404);
    expect(proxy.calls).toHaveLength(0);
  });
}
