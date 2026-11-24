import { test, expect, OAUTH_COOKIE, SESSION_COOKIE, CLEAR_OAUTH, CLEAR_SESSION } from "../fixtures/auth-proxy";

test.beforeEach(async ({ authProxy: proxy }) => { proxy.calls.length = 0; });

test("login redirects reach the browser with the OAuth cookie", async ({ authProxy: proxy, request }) => {
  const response = await request.get(`${proxy.origin}/api/v1/auth/github/login`, { maxRedirects: 0 });
  expect(response.status()).toBe(302);
  expect(response.headers().location).toBe("https://github.com/login/oauth/authorize?client_id=fixture");
  expect(response.headersArray().filter((item) => item.name.toLowerCase() === "set-cookie").map((item) => item.value)).toEqual([OAUTH_COOKIE]);
  expect(response.headers()["referrer-policy"]).toBe("no-referrer");
  expect(proxy.calls).toHaveLength(1);
});

test("callback preserves multiple cookie headers and session status", async ({ authProxy: proxy, request }) => {
  const response = await request.get(`${proxy.origin}/api/v1/auth/github/callback?code=fixture-code&state=fixture-state`, {
    maxRedirects: 0, headers: { Cookie: "refactra_oauth=fixture-attempt; analytics=private" },
  });
  expect(response.status()).toBe(302);
  expect(response.headers().location).toBe(`${proxy.origin}/`);
  expect(response.headersArray().filter((item) => item.name.toLowerCase() === "set-cookie").map((item) => item.value)).toEqual([SESSION_COOKIE, CLEAR_OAUTH]);
  expect(response.headers()["referrer-policy"]).toBe("no-referrer");
  expect(proxy.calls[0].url).toBe("/api/v1/auth/github/callback?code=fixture-code&state=fixture-state");
  expect(proxy.calls[0].headers.cookie).toBe("refactra_oauth=fixture-attempt");
  const status = await request.get(`${proxy.origin}/api/v1/auth/session`);
  expect(status.status()).toBe(200);
  expect(status.headers()["cache-control"]).toBe("no-store");
  expect(await status.json()).toEqual({ authenticated: true, user: { user_id: 12345, login: "fixture-user" } });
});

test("a failed callback keeps the authorization URL out of the referrer", async ({ authProxy: proxy, request }) => {
  const response = await request.get(`${proxy.origin}/api/v1/auth/github/callback?error=access_denied&state=fixture-state`, {
    maxRedirects: 0, headers: { Cookie: "refactra_oauth=fixture-attempt" },
  });
  expect(response.status()).toBe(400);
  expect(response.headers()["referrer-policy"]).toBe("no-referrer");
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(response.headersArray().filter((item) => item.name.toLowerCase() === "set-cookie").map((item) => item.value)).toEqual([CLEAR_OAUTH]);
  expect(await response.json()).toEqual({ error: { code: "oauth_denied" } });
});

test("non-authentication routes keep the default referrer policy", async ({ authProxy: proxy, request }) => {
  const response = await request.get(`${proxy.origin}/api/v1/repositories/42`);
  expect(response.status()).toBe(401);
  expect(response.headers()["referrer-policy"]).toBeUndefined();
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

test.describe("account controls", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/v1/capabilities", (route) => route.fulfill({ json: { mock_enabled: true } }));
  });

  test("anonymous users can start GitHub sign-in", async ({ authProxy, page }, testInfo) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto(authProxy.origin);
    const account = page.getByRole("region", { name: "GitHub account" });
    await expect(account.getByText("Not signed in", { exact: true })).toBeVisible();
    await expect(account.getByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute("href", "/api/v1/auth/github/login");
    await page.screenshot({ path: testInfo.outputPath("account-anonymous.png"), fullPage: true });
    // Intercept navigation so this UI test never visits the real GitHub site.
    await page.route("**/api/v1/auth/github/login", (route) => route.fulfill({
      contentType: "text/html", body: "<h1>GitHub authorization handoff</h1>",
    }));
    await account.getByRole("link", { name: "Sign in with GitHub" }).click();
    await expect(page.getByRole("heading", { name: "GitHub authorization handoff" })).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test("signed-in users can log out through the proxy on mobile", async ({ authProxy, page, context }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true, sameSite: "Lax" }]);
    await page.goto(authProxy.origin);
    const account = page.getByRole("region", { name: "GitHub account" });
    await expect(account.getByText("Signed in as fixture-user")).toBeVisible();
    expect(await page.evaluate(() => document.cookie)).not.toContain("refactra_session");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("account-signed-in-mobile.png"), fullPage: true });
    await account.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(account.getByRole("link", { name: "Sign in with GitHub" })).toBeVisible();
    const logout = authProxy.calls.find((call) => call.url === "/api/v1/auth/logout");
    expect(logout?.headers.origin).toBe(authProxy.origin);
    expect(logout?.headers["x-refactra-csrf"]).toBe("1");
    expect((await context.cookies()).some((cookie) => cookie.name === "refactra_session")).toBe(false);
    await expect(account.getByRole("alert")).toHaveCount(0);
  });

  test("a rejected logout keeps the account visible and explains the failure", async ({ authProxy, page, context }) => {
    await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
    await page.route("**/api/v1/auth/logout", (route) => route.fulfill({ status: 403, json: { error: { message: "Logout was rejected." } } }));
    await page.goto(authProxy.origin);
    const account = page.getByRole("region", { name: "GitHub account" });
    await account.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(account.getByRole("alert")).toHaveText("Logout was rejected.");
    await expect(account.getByText("Signed in as fixture-user")).toBeVisible();
    await expect(account.getByRole("button", { name: "Sign out", exact: true })).toBeEnabled();
    await expect(account.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
  });

  test("an unavailable session is unknown until a successful retry", async ({ authProxy, page }) => {
    let attempts = 0;
    await page.route("**/api/v1/auth/session", (route) => ++attempts === 1
      ? route.fulfill({ status: 503, json: { error: { message: "Account service unavailable." } } })
      : route.continue());
    await page.goto(authProxy.origin);
    const account = page.getByRole("region", { name: "GitHub account" });
    await expect(account.getByRole("alert")).toHaveText("Account service unavailable.");
    await expect(account.getByRole("link", { name: "Sign in with GitHub" })).toHaveCount(0);
    await account.getByRole("button", { name: "Retry account" }).click();
    await expect(account.getByRole("link", { name: "Sign in with GitHub" })).toBeVisible();
    await expect(account.getByRole("alert")).toHaveCount(0);
  });
});
