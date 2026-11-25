import { test, expect } from "../fixtures/auth-proxy";

const metadata = { repository_id: 1001132861, full_name: "fixture/example", default_branch: "main", access: "read-only" };
const repositoryUrl = "**/api/v1/repository";

test.beforeEach(async ({ page, authProxy }) => {
  authProxy.calls.length = 0;
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({ json: { mock_enabled: true } }));
});

test("repository check stays disabled until the account is confirmed", async ({ page, authProxy }) => {
  await page.goto(authProxy.origin);
  await expect(page.getByRole("region", { name: "GitHub account" }).getByText("Not signed in", { exact: true })).toBeVisible();
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("button", { name: "Check repository access" })).toBeDisabled();
  expect(authProxy.calls.some((call) => call.url.startsWith("/api/v1/repositor"))).toBe(false);
});

test("a verified connection shows metadata and clears it after sign-out", async ({ page, context, authProxy }, testInfo) => {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await expect(panel.getByRole("button", { name: "Check repository access" })).toBeEnabled();
  // The repository comes from the API's configuration, not a name built into the page.
  await expect(panel.locator(".repository-target")).toHaveText("Configured repository");
  await panel.getByRole("button", { name: "Check repository access" }).click();
  await expect(panel.getByRole("status")).toHaveText("Read-only connection verified.");
  await expect(panel.locator(".repository-target")).toHaveText("fixture/example");
  await expect(panel.getByRole("definition").filter({ hasText: "fixture/example" })).toBeVisible();
  await expect(panel.getByText("main", { exact: true })).toBeVisible();
  expect(authProxy.calls.filter((call) => call.url === "/api/v1/repository")).toHaveLength(1);
  expect(await page.evaluate(() => document.cookie)).not.toContain("refactra_session");
  await page.screenshot({ path: testInfo.outputPath("repository-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Check repository access" })).toBeDisabled();
  await expect(panel.getByText("fixture/example", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("Read-only connection verified.")).toHaveCount(0);
});

test("a failed check can be retried and remains usable on mobile", async ({ page, context, authProxy }, testInfo) => {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  await page.setViewportSize({ width: 390, height: 844 });
  let attempts = 0;
  await page.route(repositoryUrl, (route) => ++attempts === 1
    ? route.fulfill({ status: 403, json: { error: { message: "Repository access is not allowed." } } })
    : route.fulfill({ json: metadata }));
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await panel.getByRole("button", { name: "Check repository access" }).click();
  await expect(panel.getByRole("alert")).toHaveText("Repository access is not allowed.");
  await expect(panel.getByText("fixture/example", { exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "Retry connection" }).click();
  await expect(panel.getByRole("status")).toHaveText("Read-only connection verified.");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("repository-mobile.png"), fullPage: true });
});

test("repository checks can outlast the shared client's seven-second timeout", async ({ page, context, authProxy }) => {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  await page.route(repositoryUrl, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 7500));
    await route.fulfill({ json: metadata });
  });
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  await panel.getByRole("button", { name: "Check repository access" }).click();
  await expect(panel.getByRole("status")).toHaveText("Read-only connection verified.", { timeout: 12000 });
});

test("sign-out cancels a pending check and prevents a late result from restoring metadata", async ({ page, context, authProxy }) => {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  let release!: () => void;
  let delivered!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const lateResponse = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route(repositoryUrl, async (route) => {
    await gate;
    await route.fulfill({ json: metadata });
    delivered();
  });
  await page.goto(authProxy.origin);
  const panel = page.getByRole("region", { name: "Repository connection" });
  const started = page.waitForRequest((request) => request.url().endsWith("/api/v1/repository"));
  await panel.getByRole("button", { name: "Check repository access" }).click();
  await started;
  await expect(panel.getByRole("button", { name: "Checking access…" })).toBeDisabled();
  const cancelled = page.waitForEvent("requestfailed", { predicate: (request) => request.url().endsWith("/api/v1/repository") });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await cancelled;
  release();
  await lateResponse;
  await expect(panel.getByRole("button", { name: "Check repository access" })).toBeDisabled();
  await expect(panel.getByText("fixture/example", { exact: true })).toHaveCount(0);
});

test("the analysis form explains when no analysis worker is running", async ({ page, context, authProxy }) => {
  await context.addCookies([{ name: "refactra_session", value: "fixture-session", url: authProxy.origin, httpOnly: true }]);
  await page.route("**/api/v1/capabilities", (route) => route.fulfill({
    json: { mock_enabled: true, repository_analysis_enabled: true, analysis_worker_available: false },
  }));
  await page.goto(authProxy.origin);
  await page.getByRole("region", { name: "Repository connection" })
    .getByRole("button", { name: "Check repository access" }).click();
  const analysis = page.getByRole("region", { name: "Repository analysis" });
  await expect(analysis.getByText("No analysis worker is running.", { exact: false })).toBeVisible();
  await expect(analysis.getByRole("button", { name: "Start analysis" })).toBeVisible();
});
