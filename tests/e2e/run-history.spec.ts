import { RUN_HISTORY, RUN_ID, test, expect } from "../fixtures/auth-proxy";

test.beforeEach(async ({ authProxy }) => { authProxy.calls.length = 0; });

async function signIn(context: import("@playwright/test").BrowserContext, origin: string) {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: origin, httpOnly: true }]);
}

test("signed-out visitors are sent to sign in and no runs are requested", async ({ page, authProxy }) => {
  await page.goto(`${authProxy.origin}/history`);
  await expect(page).toHaveURL(`${authProxy.origin}/sign-in?next=%2Fhistory`);
  expect(authProxy.calls.some((call) => call.url.startsWith("/api/v1/runs"))).toBe(false);
});

test("the history lists runs with their stage, PR link and details", async ({ page, context, authProxy }, testInfo) => {
  await signIn(context, authProxy.origin);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto(authProxy.origin);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "History" }).click();
  await expect(page).toHaveURL(/\/history$/);
  const published = page.getByRole("article", { name: `Run ${RUN_ID}` });
  await expect(published.getByText("fixture/example")).toBeVisible();
  await expect(published.getByText("Extract the cart calculation")).toBeVisible();
  await expect(published.getByText("Published")).toBeVisible();
  await expect(published.getByRole("link", { name: "Draft PR #7" }))
    .toHaveAttribute("href", "https://github.com/fixture/example/pull/7");
  await expect(published.getByRole("definition").filter({ hasText: "$0.0421" })).toBeVisible();
  await expect(published.getByRole("definition").filter({ hasText: "main @ aaaaaaa" })).toBeVisible();
  const failed = page.getByRole("article", { name: `Run ${RUN_HISTORY.runs[1].run_id}` });
  await expect(failed.getByText("Repository 1001132861")).toBeVisible();
  await expect(failed.getByText("Failed")).toBeVisible();
  await expect(failed.getByText("Candidate validation failed.", { exact: false })).toBeVisible();
  expect(authProxy.calls.find((call) => call.url.startsWith("/api/v1/runs"))?.url).toBe("/api/v1/runs?limit=20");
  await page.screenshot({ path: testInfo.outputPath("history-desktop.png"), fullPage: true });
});

test("search sends the query and pages load more runs", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  const requests: string[] = [];
  await page.route("**/api/v1/runs?*", (route) => {
    const url = new URL(route.request().url());
    requests.push(url.search);
    const first = !url.searchParams.has("cursor");
    return route.fulfill({ json: {
      runs: [{ ...RUN_HISTORY.runs[first ? 0 : 1] }],
      next_cursor: first ? "next-page" : null,
    } });
  });
  await page.goto(`${authProxy.origin}/history`);
  await page.getByLabel("Search tasks and repositories").fill("  cart ");
  await page.getByRole("button", { name: "Search" }).click();
  await expect.poll(() => requests.at(-1)).toBe("?limit=20&q=cart");
  await page.getByRole("button", { name: "Load more" }).click();
  await expect(page.getByRole("article")).toHaveCount(2);
  expect(requests.at(-1)).toBe("?limit=20&q=cart&cursor=next-page");
  await expect(page.getByRole("button", { name: "Load more" })).toHaveCount(0);
});

test("an empty history points to the workspace and stays usable on mobile", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v1/runs?*", (route) => route.fulfill({ json: { runs: [], next_cursor: null } }));
  await page.goto(`${authProxy.origin}/history`);
  await expect(page.getByRole("link", { name: "Start one in the workspace." })).toHaveAttribute("href", "/");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("the proxy forwards history reads with only the session cookie", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}/api/v1/runs?limit=5&q=cart`, {
    headers: { Cookie: "refactra_session=fixture-session; analytics=private", Authorization: "Bearer must-not-pass" },
  });
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(authProxy.calls[0].url).toBe("/api/v1/runs?limit=5&q=cart");
  expect(authProxy.calls[0].headers.cookie).toBe("refactra_session=fixture-session");
  expect(authProxy.calls[0].headers.authorization).toBeUndefined();
});

test("the account bar shows the remaining allowance", async ({ page, context, authProxy }) => {
  await signIn(context, authProxy.origin);
  await page.goto(`${authProxy.origin}/history`);
  await expect(page.getByRole("region", { name: "GitHub account" }))
    .toContainText("Today: 2 of 5 runs · This month: $0.40 of $3.00");
});

test("the proxy forwards usage reads with only the session cookie", async ({ authProxy, request }) => {
  const response = await request.get(`${authProxy.origin}/api/v1/usage`, {
    headers: { Cookie: "refactra_session=fixture-session; analytics=private" },
  });
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(authProxy.calls[0].headers.cookie).toBe("refactra_session=fixture-session");
});
